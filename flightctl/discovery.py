"""Bounded, side-effect-free inventory discovery proposals.

The discovery implementation deliberately stops at a proposal.  It does not
stage, confirm, grant, install, or otherwise change controller state.  The
transport and GPU probe are injected so that the same parsing and projection
code can be exercised without a network or a host GPU.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import tempfile
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


class DiscoveryError(ValueError):
    """The requested discovery cannot safely produce a proposal."""


class ProjectionError(DiscoveryError):
    """A proposal cannot be projected to a schema-shaped inventory."""


_GPU_FIELDS = ("vendor", "model", "vram_bytes", "driver")
_SHORT_ID = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_ENDPOINT = re.compile(r"^[A-Za-z0-9._-]+$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_RANGE_SUFFIX = re.compile(r"^(?P<prefix>.*?)(?P<start>\d+)(?:\.\.|-)(?P<end>\d+)$")
_RANGE_REPEATED_PREFIX = re.compile(r"^(?P<prefix>.*?)(?P<start>\d+)\.\.(?P=prefix)(?P<end>\d+)$")
_BRACKET_RANGE = re.compile(r"^(?P<prefix>[^[]*)\[(?P<start>\d+)-(?P<end>\d+)\](?P<suffix>.*)$")

_SAFE_REASON_TEXT = frozenset(
    {
        "unknown measurement",
        "vendor not reported",
        "model not reported",
        "VRAM not reported",
        "driver not reported",
        "missing VRAM measurement",
        "invalid VRAM measurement",
        "invalid VRAM unit",
        "nvidia-smi returned no output",
        "nvidia-smi returned no devices",
        "unparsable nvidia-smi row",
        "incomplete GPU observation",
        "AMD/sysfs returned no output",
        "incomplete AMD/sysfs observation",
        "GPU probe returned no mapping",
        "GPU probe returned no devices",
        "empty GPU observation",
        "GPU count does not match device records",
        "confirmed no GPU",
        "GPU probe failed",
        "host probe failed",
        "device not observed; retained for review",
        "device set changed; retained missing device records",
        "host not present in requested peer status",
    }
)


class _SystemClock:
    def utc(self) -> datetime:
        return datetime.now(timezone.utc)


def _slug(value: object, *, prefix: str = "item") -> str:
    text = str(value).strip().lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-._")
    if not text:
        text = prefix
    if not text[0].isalpha():
        text = f"{prefix}-{text}"
    return text


def _identifier(value: object, *, prefix: str = "item") -> str:
    text = str(value).strip()
    if _IDENTIFIER.fullmatch(text):
        return text
    return _slug(text, prefix=prefix)


def _short_id(value: object, *, prefix: str = "item") -> str:
    text = _slug(value, prefix=prefix)
    if len(text) > 64:
        digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:10]
        stem = text[:53].rstrip("-._") or prefix
        text = f"{stem}-{digest}"
    return text


def _safe_diagnostic(value: object, *, default: str = "host probe failed") -> str:
    """Map untrusted transport text to a small, non-sensitive vocabulary."""

    text = str(value or "").strip().lower()
    if "timeout" in text or "timed" in text or "deadline" in text or "delay" in text:
        return "probe timed out"
    if "denied" in text or "access" in text or "permission" in text or "authoriz" in text:
        return "SSH access denied"
    if "lost" in text:
        return "probe reply lost"
    if "unreach" in text or "no route" in text:
        return "host unreachable"
    if "unavailable" in text or "not configured" in text or "no transport" in text:
        return "transport unavailable"
    if "gpu" in text and "probe" in text:
        return "GPU probe failed"
    return default


def _safe_reason(value: object, *, default: str) -> str:
    text = str(value or "").strip()
    if not text:
        return default
    if text in _SAFE_REASON_TEXT:
        return text
    return _safe_diagnostic(text, default=default)


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc).replace(microsecond=0)
    return value.isoformat().replace("+00:00", "Z")


def _copy_mapping(value: object) -> dict[str, Any]:
    return deepcopy(dict(value)) if isinstance(value, Mapping) else {}


def _json_value(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _safe_positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


def _parse_memory_bytes(value: object) -> tuple[int | None, str | None]:
    """Parse the common nvidia-smi/sysfs memory forms without coercing bad data."""

    if isinstance(value, bool) or value is None:
        return None, "missing VRAM measurement"
    text = str(value).strip().replace(",", "")
    if not text:
        return None, "missing VRAM measurement"
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*([A-Za-z]+)?", text)
    if not match:
        return None, "invalid VRAM measurement"
    try:
        number = float(match.group(1))
    except ValueError:
        return None, "invalid VRAM measurement"
    if not math.isfinite(number) or number <= 0:
        return None, "invalid VRAM measurement"
    unit = (match.group(2) or "mib").lower()
    multipliers = {
        "b": 1,
        "byte": 1,
        "bytes": 1,
        "kib": 1024,
        "kb": 1000,
        "mib": 1024**2,
        "mb": 1000**2,
        "gib": 1024**3,
        "gb": 1000**3,
        "tib": 1024**4,
        "tb": 1000**4,
    }
    multiplier = multipliers.get(unit)
    if multiplier is None:
        return None, "invalid VRAM unit"
    result = number * multiplier
    if not result.is_integer() or result <= 0:
        return None, "invalid VRAM measurement"
    return int(result), None


def _parse_sysfs_bytes(value: object) -> tuple[int | None, str | None]:
    """Sysfs ``vram_bytes`` is already bytes unless it explicitly has a unit."""

    if isinstance(value, str) and re.fullmatch(r"\s*\+?\d+\s*", value):
        number = int(value.strip())
        return (number, None) if number > 0 else (None, "invalid VRAM measurement")
    return _parse_memory_bytes(value)


def _device(
    device_id: object,
    *,
    vendor: object = None,
    model: object = None,
    vram_bytes: object = None,
    driver: object = None,
    reasons: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Build a device while making every null field explainable."""

    source_reasons = dict(reasons or {})
    values: dict[str, Any] = {
        "vendor": vendor.lower().strip() if isinstance(vendor, str) and vendor.strip() else None,
        "model": model.strip() if isinstance(model, str) and model.strip() else None,
        "vram_bytes": _safe_positive_int(vram_bytes),
        "driver": driver.strip() if isinstance(driver, str) and driver.strip() else None,
    }
    for field, reason in list(source_reasons.items()):
        if field not in _GPU_FIELDS:
            source_reasons.pop(field, None)
        elif values[field] is not None:
            source_reasons.pop(field, None)
        else:
            source_reasons[field] = _safe_reason(reason, default="unknown measurement")
    default_reasons = {
        "vendor": "vendor not reported",
        "model": "model not reported",
        "vram_bytes": "VRAM not reported",
        "driver": "driver not reported",
    }
    for field in _GPU_FIELDS:
        if values[field] is None:
            source_reasons.setdefault(field, default_reasons[field])
        else:
            source_reasons.pop(field, None)
    return {"device_id": _short_id(device_id, prefix="gpu"), **values, "unknown_reasons": source_reasons}


def _unknown_gpu(reason: str) -> dict[str, Any]:
    message = _safe_reason(reason, default="GPU observation is unknown")
    return {
        "vendor": None,
        "model": None,
        "vram_bytes": None,
        "driver": None,
        "count": None,
        "reason": message,
        "devices": [],
    }


def parse_nvidia_smi(raw: str) -> dict[str, Any]:
    """Parse nvidia-smi CSV output, preserving partial fields as unknown.

    The parser accepts both the three-column query form
    ``name,memory.total,driver_version`` and the four-column test form with a
    vendor column.  An empty, malformed, or negative measurement is never
    interpreted as zero GPUs.
    """

    if not isinstance(raw, str) or not raw.strip():
        return _unknown_gpu("nvidia-smi returned no output")
    rows = list(csv.reader(raw.strip().splitlines()))
    devices: list[dict[str, Any]] = []
    errors: list[str] = []
    for index, row in enumerate(rows):
        row = [part.strip() for part in row]
        if not row or all(not part for part in row):
            continue
        # A header is harmless when the command was run without noheader.
        if row[0].lower() in {"name", "gpu_name", "model"}:
            continue
        if len(row) < 3:
            errors.append("unparsable nvidia-smi row")
            continue
        model = row[0] or None
        vram, vram_error = _parse_memory_bytes(row[1])
        driver = row[2] or None
        vendor = (row[3] if len(row) >= 4 else "nvidia") or None
        reasons: dict[str, str] = {}
        if model is None:
            reasons["model"] = "model not reported"
        if vram_error:
            reasons["vram_bytes"] = vram_error
        if driver is None:
            reasons["driver"] = "driver not reported"
        if vendor is None:
            reasons["vendor"] = "vendor not reported"
        devices.append(_device(f"gpu{index}", vendor=vendor, model=model, vram_bytes=vram, driver=driver, reasons=reasons))
    if not devices:
        return _unknown_gpu(errors[0] if errors else "nvidia-smi returned no devices")
    reason = errors[0] if errors else None
    if any(device["unknown_reasons"] for device in devices):
        reason = reason or "incomplete GPU observation"
    complete = not errors and all(not device["unknown_reasons"] for device in devices)
    first = devices[0]
    return {
        "vendor": first["vendor"],
        "model": first["model"],
        "vram_bytes": first["vram_bytes"],
        "driver": first["driver"],
        "count": len(devices) if complete else None,
        "reason": reason,
        "devices": devices,
    }


def _sysfs_groups(raw: str) -> list[dict[str, str]]:
    groups: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in str(raw).splitlines():
        stripped = line.strip()
        if not stripped:
            if current:
                groups.append(current)
                current = {}
            continue
        if "=" not in stripped:
            continue
        key, value = (part.strip() for part in stripped.split("=", 1))
        key = key.rsplit(".", 1)[-1]
        if key in {"vendor", "model", "vram_bytes", "driver"}:
            current[key] = value
    if current:
        groups.append(current)
    return groups


def parse_amd_sysfs(raw: str) -> dict[str, Any]:
    """Parse the small key/value representation used for AMD sysfs probes."""

    groups = _sysfs_groups(raw)
    if not groups:
        return _unknown_gpu("AMD/sysfs returned no output")
    devices: list[dict[str, Any]] = []
    for index, fields in enumerate(groups):
        vram, vram_error = _parse_sysfs_bytes(fields.get("vram_bytes"))
        reasons: dict[str, str] = {}
        for field in ("vendor", "model", "driver"):
            if not fields.get(field):
                reasons[field] = f"{field} not reported"
        if vram_error:
            reasons["vram_bytes"] = vram_error
        devices.append(
            _device(
                f"gpu{index}",
                vendor=fields.get("vendor"),
                model=fields.get("model"),
                vram_bytes=vram,
                driver=fields.get("driver"),
                reasons=reasons,
            )
        )
    complete = all(not item["unknown_reasons"] for item in devices)
    first = devices[0]
    return {
        "vendor": first["vendor"],
        "model": first["model"],
        "vram_bytes": first["vram_bytes"],
        "driver": first["driver"],
        "count": len(devices) if complete else None,
        "reason": None if complete else "incomplete AMD/sysfs observation",
        "devices": devices,
    }


def _normalise_gpu_result(result: object) -> dict[str, Any]:
    """Normalise a fake or transport GPU result without importing fake code."""

    if isinstance(result, str):
        return parse_nvidia_smi(result)
    if not isinstance(result, Mapping):
        return _unknown_gpu("GPU probe returned no mapping")
    data: Mapping[str, Any] = result
    for key in ("gpu", "gpu_probe", "probe", "response"):
        nested = data.get(key)
        if isinstance(nested, Mapping) and (key != "response" or any(k in nested for k in ("devices", "count", "raw", "nvidia_smi", "sysfs"))):
            data = nested
            break
    if isinstance(data.get("nvidia_smi"), str):
        return parse_nvidia_smi(data["nvidia_smi"])
    if isinstance(data.get("nvidia_smi_output"), str):
        return parse_nvidia_smi(data["nvidia_smi_output"])
    if isinstance(data.get("amd_sysfs"), str) or isinstance(data.get("sysfs"), str):
        return parse_amd_sysfs(str(data.get("amd_sysfs", data.get("sysfs", ""))))
    if isinstance(data.get("sysfs_output"), str):
        return parse_amd_sysfs(data["sysfs_output"])
    if isinstance(data.get("stdout"), str):
        family = str(data.get("family", "nvidia")).lower()
        return parse_amd_sysfs(data["stdout"]) if family in {"amd", "sysfs"} else parse_nvidia_smi(data["stdout"])
    if isinstance(data.get("raw"), str):
        family = str(data.get("family", "nvidia")).lower()
        return parse_amd_sysfs(data["raw"]) if family in {"amd", "sysfs"} else parse_nvidia_smi(data["raw"])
    status = str(data.get("status", "")).lower()
    if status in {"missing", "unavailable", "timeout", "denied", "unknown", "failure", "failed"}:
        return _unknown_gpu(_safe_reason(data.get("reason", data.get("error", f"GPU probe {status}")), default="GPU probe failed"))
    if data.get("no_gpu") is True or status in {"none", "no-gpu", "no_gpu"}:
        return {"vendor": None, "model": None, "vram_bytes": None, "driver": None, "count": 0, "reason": "confirmed no GPU", "devices": []}

    raw_devices = data.get("devices")
    devices: list[dict[str, Any]] = []
    if isinstance(raw_devices, Sequence) and not isinstance(raw_devices, (str, bytes, bytearray)):
        for index, item in enumerate(raw_devices):
            if isinstance(item, Mapping):
                reasons = item.get("unknown_reasons")
                devices.append(
                    _device(
                        item.get("device_id", item.get("id", f"gpu{index}")),
                        vendor=item.get("vendor"),
                        model=item.get("model"),
                        vram_bytes=item.get("vram_bytes"),
                        driver=item.get("driver"),
                        reasons=reasons if isinstance(reasons, Mapping) else None,
                    )
                )
    count_value = data.get("count")
    count = count_value if isinstance(count_value, int) and not isinstance(count_value, bool) and count_value >= 0 else None
    explicit_reason = data.get("reason")
    reason = _safe_reason(explicit_reason, default="GPU probe failed") if explicit_reason else None
    if count == 0 and not devices:
        if reason and re.search(r"\bno[-_ ]gpu\b", reason.lower()):
            return {"vendor": None, "model": None, "vram_bytes": None, "driver": None, "count": 0, "reason": "confirmed no GPU", "devices": []}
        return _unknown_gpu(reason or "empty GPU observation")
    if not devices and any(key in data for key in _GPU_FIELDS):
        devices = [
            _device(
                "gpu0",
                vendor=data.get("vendor"),
                model=data.get("model"),
                vram_bytes=data.get("vram_bytes"),
                driver=data.get("driver"),
            )
        ]
    if not devices:
        return _unknown_gpu(reason or "GPU probe returned no devices")
    complete = count is not None and count == len(devices) and all(not item["unknown_reasons"] for item in devices)
    if count is not None and count != len(devices):
        reason = reason or "GPU count does not match device records"
    if any(item["unknown_reasons"] for item in devices):
        reason = reason or "incomplete GPU observation"
    if not complete:
        count = None
    first = devices[0]
    return {
        "vendor": first["vendor"],
        "model": first["model"],
        "vram_bytes": first["vram_bytes"],
        "driver": first["driver"],
        "count": count,
        "reason": reason,
        "devices": devices,
    }


def parse_tailnet_status(raw: object) -> list[dict[str, Any]]:
    """Extract bounded, stable SSH targets from tailscale status JSON."""

    value: Any = raw
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise DiscoveryError("tailnet status is not valid JSON") from exc
    if isinstance(value, Mapping):
        if isinstance(value.get("response"), (Mapping, str)):
            return parse_tailnet_status(value["response"])
        if isinstance(value.get("tailnet_status"), (Mapping, str)):
            return parse_tailnet_status(value["tailnet_status"])
        if isinstance(value.get("stdout"), str):
            return parse_tailnet_status(value["stdout"])
        peers = value.get("Peer", value.get("peer", value.get("peers")))
    else:
        peers = value
    if peers is None:
        return []
    entries: list[tuple[object, Mapping[str, Any]]] = []
    if isinstance(peers, Mapping):
        entries = [(key, item) for key, item in peers.items() if isinstance(item, Mapping)]
    elif isinstance(peers, Sequence) and not isinstance(peers, (str, bytes, bytearray)):
        entries = [(index, item) for index, item in enumerate(peers) if isinstance(item, Mapping)]
    else:
        raise DiscoveryError("tailnet peers are not a mapping or list")
    result: list[dict[str, Any]] = []
    for key, peer in entries:
        hostname = peer.get("HostName", peer.get("hostname", peer.get("host_id")))
        dns_name = peer.get("DNSName", peer.get("dns_name", peer.get("endpoint")))
        ips = peer.get("TailscaleIPs", peer.get("addresses", []))
        endpoint = dns_name or hostname
        if not endpoint and isinstance(ips, Sequence) and not isinstance(ips, (str, bytes, bytearray)):
            endpoint = next((item for item in ips if isinstance(item, str) and item.strip()), None)
        if not endpoint:
            endpoint = f"peer-{key}"
        endpoint = str(endpoint).strip().rstrip(".")
        # The inventory contract intentionally uses a host-like SSH endpoint.
        if ":" in endpoint and endpoint.count(":") == 1 and endpoint.rsplit(":", 1)[1].isdigit():
            endpoint = endpoint.rsplit(":", 1)[0]
        if not _ENDPOINT.fullmatch(endpoint):
            endpoint = _short_id(endpoint, prefix="host")
        host_id = _short_id(hostname or endpoint, prefix="host")
        result.append({"host_id": host_id, "ssh_endpoint": endpoint, "ssh_user": peer.get("User", peer.get("ssh_user")), "online": peer.get("Online", peer.get("online", True)), "source": "tailnet"})
    return sorted(result, key=lambda item: (item["host_id"], item["ssh_endpoint"]))


def project_proposal(proposal: Mapping[str, Any]) -> dict[str, Any]:
    """Project exactly the frozen proposal fields into a draft inventory."""

    if not isinstance(proposal, Mapping):
        raise ProjectionError("discovery proposal must be a mapping")
    top_level = ("schema_version", "site_id", "revision", "controller", "timezone", "identity_mapping", "chat_lane_order")
    projected: dict[str, Any] = {key: deepcopy(proposal[key]) for key in top_level if key in proposal}
    projected["stage"] = "draft"
    projected["hosts"] = []
    for host in proposal.get("hosts", []):
        if not isinstance(host, Mapping):
            raise ProjectionError("proposal host must be a mapping")
        projected["hosts"].append({key: deepcopy(host[key]) for key in ("host_id", "ssh_endpoint", "ssh_user", "reachability", "observed_at", "observation_error", "gpu_count", "gpu_count_reason", "devices") if key in host})
    projected["lanes"] = []
    for lane in proposal.get("lanes", []):
        if not isinstance(lane, Mapping):
            raise ProjectionError("proposal lane must be a mapping")
        projected["lanes"].append({key: deepcopy(lane[key]) for key in ("lane_id", "host_id", "device_ids", "class", "enabled", "policy") if key in lane})
    required = {"schema_version", "site_id", "revision", "stage", "controller", "timezone", "identity_mapping", "chat_lane_order", "hosts", "lanes"}
    missing = sorted(required - set(projected))
    if missing:
        raise ProjectionError(f"proposal lacks projection fields: {', '.join(missing)}")
    return projected


class DiscoveryHandler:
    """Construct proposals using injected clock, transport, and GPU probe."""

    def __init__(
        self,
        transport: Any = None,
        clock: Any = None,
        gpu_probe: Any = None,
        *,
        probe: Any = None,
        current_inventory: Mapping[str, Any] | str | Path | None = None,
        current_inventory_path: str | Path | None = None,
        current_path: str | Path | None = None,
        repo_root: str | Path | None = None,
        timeout_s: float = 5.0,
        max_hosts: int = 64,
    ) -> None:
        if hasattr(transport, "utc") and hasattr(clock, "request"):
            transport, clock = clock, transport
        self.transport = transport
        self.clock = clock or _SystemClock()
        self.gpu_probe = gpu_probe if gpu_probe is not None else probe
        self.current_inventory = current_inventory
        self.current_inventory_path = current_inventory_path or current_path
        self.repo_root = Path(repo_root).resolve() if repo_root is not None else Path(__file__).resolve().parents[1]
        self.timeout_s = self._finite_timeout(timeout_s)
        self.max_hosts = self._positive_limit(max_hosts, "max_hosts")

    @staticmethod
    def _finite_timeout(value: object) -> float:
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError) as exc:
            raise DiscoveryError("timeout_s must be finite and positive") from exc
        if not math.isfinite(number) or number <= 0:
            raise DiscoveryError("timeout_s must be finite and positive")
        return number

    @staticmethod
    def _positive_limit(value: object, name: str) -> int:
        if isinstance(value, bool):
            raise DiscoveryError(f"{name} must be positive")
        try:
            number = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError) as exc:
            raise DiscoveryError(f"{name} must be positive") from exc
        if number < 1:
            raise DiscoveryError(f"{name} must be positive")
        return number

    def propose(self, options: Mapping[str, Any] | None = None) -> dict[str, Any]:
        opts = dict(options or {})
        current, current_path = self._load_current(opts)
        now = _iso_utc(self.clock.utc())
        timeout_s = self._finite_timeout(opts.get("timeout_s", opts.get("timeout", self.timeout_s)))
        max_hosts = self._positive_limit(opts.get("max_hosts", opts.get("host_limit", self.max_hosts)), "max_hosts")
        specs, authoritative = self._target_specs(opts, current, timeout_s, max_hosts)
        current_hosts = {str(item.get("host_id")): item for item in current.get("hosts", []) if isinstance(item, Mapping) and item.get("host_id")}
        matched_current: set[str] = set()
        observed_hosts: list[dict[str, Any]] = []
        observations: list[dict[str, Any]] = []
        host_meta: dict[str, dict[str, Any]] = {}
        used_device_ids = {
            str(device.get("device_id"))
            for item in current.get("hosts", [])
            if isinstance(item, Mapping)
            for device in item.get("devices", [])
            if isinstance(device, Mapping) and device.get("device_id")
        }
        for spec in specs[:max_hosts]:
            current_host = self._match_current(spec, current_hosts)
            if current_host is not None:
                matched_current.add(str(current_host["host_id"]))
                host_id = str(current_host["host_id"])
                merged_spec = {
                    **spec,
                    "host_id": host_id,
                    "ssh_endpoint": spec.get("ssh_endpoint") or current_host.get("ssh_endpoint"),
                }
                if not spec.get("_ssh_user_explicit") and current_host.get("ssh_user"):
                    merged_spec["ssh_user"] = current_host["ssh_user"]
                spec = merged_spec
            else:
                host_id = str(spec["host_id"])
            local_used_device_ids = set(used_device_ids)
            if current_host is not None:
                local_used_device_ids.difference_update(
                    str(device.get("device_id"))
                    for device in current_host.get("devices", [])
                    if isinstance(device, Mapping) and device.get("device_id")
                )
            host, host_observations, meta = self._probe_host(spec, current_host, now, timeout_s, local_used_device_ids)
            if spec.get("source") == "tailnet":
                host_observations.insert(0, self._observation("peer", host_id, now, "confirmed", {"endpoint": spec.get("ssh_endpoint")}, None, "tailnet"))
            used_device_ids.update(
                str(device.get("device_id"))
                for device in host.get("devices", [])
                if isinstance(device, Mapping) and device.get("device_id")
            )
            observed_hosts.append(host)
            observations.extend(host_observations)
            host_meta[host_id] = meta

        # An authoritative peer listing makes omissions meaningful.  Explicit
        # operator targets are deliberately not treated as a global inventory.
        retained_hosts: list[dict[str, Any]] = []
        for host_id, old_host in current_hosts.items():
            if host_id in matched_current:
                continue
            if authoritative:
                missing, missing_obs = self._missing_host(old_host, now, "host not present in requested peer status")
                retained_hosts.append(missing)
                observations.extend(missing_obs)
                host_meta[host_id] = {"missing": True, "hardware_changed": True, "probe": None}
            else:
                retained_hosts.append(self._retain_host(old_host))
                host_meta[host_id] = {"missing": False, "hardware_changed": False, "probe": None, "unprobed": True}
        all_hosts = observed_hosts + retained_hosts
        all_hosts.sort(key=lambda item: item["host_id"])

        config = self._configuration(opts, current, now)
        lanes, new_lane_ids = self._build_lanes(current, all_hosts, host_meta, config, now)
        chat_order = self._chat_order(current, config, lanes, new_lane_ids)
        proposal: dict[str, Any] = {
            "schema_version": 1,
            "site_id": config["site_id"],
            "revision": int(current.get("revision", 0) or 0) + 1,
            "stage": "draft",
            "controller": config["controller"],
            "timezone": config["timezone"],
            "identity_mapping": config["identity_mapping"],
            "chat_lane_order": chat_order,
            "policy": config["policy"],
            "generated_at": now,
            "current_revision": current.get("revision"),
            "hosts": all_hosts,
            "lanes": lanes,
            "observations": observations,
            "diff": [],
            "status": "proposed",
        }
        proposal["observations"] = self._sorted_observations(proposal["observations"])
        proposal["diff"] = self._diff(current, proposal)
        if proposal["diff"]:
            proposal["status"] = "needs_review"
        return proposal

    def discover(self, options: Mapping[str, Any] | None = None) -> dict[str, Any]:
        opts = dict(options or {})
        output = self._option_value(opts, "output", "proposal", "output_path", "proposal_path")
        if output is None or (isinstance(output, str) and not output.strip()):
            raise DiscoveryError("proposal output path is required")
        proposal = self.propose(opts)
        self.write_proposal(proposal, output, current_path=self._current_path_for_alias(opts))
        return proposal

    handle = discover
    run = discover
    __call__ = discover

    def write_proposal(self, proposal: Mapping[str, Any], output: str | Path, *, current_path: str | Path | None = None) -> Path:
        if output is None or (isinstance(output, str) and not output.strip()):
            raise DiscoveryError("proposal output path is required")
        try:
            output_path = Path(output).expanduser()
        except TypeError as exc:
            raise DiscoveryError("proposal output path is required") from exc
        if not str(output_path).strip():
            raise DiscoveryError("proposal output path is required")
        resolved = output_path.resolve(strict=False)
        repo = self.repo_root.resolve()
        if resolved == repo or repo in resolved.parents:
            raise DiscoveryError("proposal output must be outside the product checkout")
        if output_path.is_symlink():
            raise DiscoveryError("proposal output symlink is not allowed")
        parent = resolved.parent
        if not parent.exists() or not parent.is_dir():
            raise DiscoveryError("proposal output directory does not exist")
        if resolved.exists() and resolved.is_dir():
            raise DiscoveryError("proposal output must be a file")
        if not os.access(parent, os.W_OK):
            raise DiscoveryError("proposal output directory is not writable")
        if current_path is not None:
            current_resolved = Path(current_path).expanduser().resolve(strict=False)
            if resolved == current_resolved:
                raise DiscoveryError("proposal output aliases the current inventory")
        encoded = json.dumps(proposal, sort_keys=True, indent=2, ensure_ascii=True) + "\n"
        fd, temporary_name = tempfile.mkstemp(prefix=".flightctl-proposal-", dir=str(parent), text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, resolved)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise
        return resolved

    def _load_current(self, options: Mapping[str, Any]) -> tuple[dict[str, Any], Path | None]:
        marker = object()
        source: object = marker
        source_key = None
        for key in ("current", "current_inventory", "current_path", "inventory"):
            if key in options:
                source = options[key]
                source_key = key
                break
        if source is marker:
            source = self.current_inventory
        if source is None and self.current_inventory_path is not None:
            source = self.current_inventory_path
        if source is None:
            return {}, None
        if isinstance(source, Mapping):
            return deepcopy(dict(source)), None
        path = Path(source).expanduser()
        if not path.exists() or not path.is_file():
            raise DiscoveryError(f"current inventory is not readable: {path}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DiscoveryError(f"current inventory is not valid JSON: {path}") from exc
        if not isinstance(value, Mapping):
            raise DiscoveryError("current inventory must be a JSON object")
        return deepcopy(dict(value)), path.resolve()

    def _current_path_for_alias(self, options: Mapping[str, Any]) -> Path | None:
        for key in ("current", "current_inventory", "current_path", "inventory"):
            value = options.get(key)
            if isinstance(value, (str, Path)):
                return Path(value).expanduser()
        if isinstance(self.current_inventory, (str, Path)):
            return Path(self.current_inventory).expanduser()
        if self.current_inventory_path is not None:
            return Path(self.current_inventory_path).expanduser()
        return None

    @staticmethod
    def _option_value(options: Mapping[str, Any], *keys: str) -> object:
        for key in keys:
            if key in options:
                return options[key]
        return None

    def _target_specs(self, options: Mapping[str, Any], current: Mapping[str, Any], timeout_s: float, max_hosts: int) -> tuple[list[dict[str, Any]], bool]:
        explicit_keys = ("hosts", "host", "operator_hosts", "ranges", "host_ranges", "operator_ranges", "range", "host_range")
        explicit = any(key in options and options[key] not in (None, [], "") for key in explicit_keys)
        specs: list[dict[str, Any]] = []
        authoritative = False

        tailnet_value = self._option_value(options, "tailnet_status", "peer_status", "peers")
        if tailnet_value is not None:
            specs.extend(parse_tailnet_status(tailnet_value))
            authoritative = True
        elif bool(options.get("discover_tailnet", False)) or (not explicit and not current.get("hosts")):
            result = self._request("tailscale", {"kind": "tailnet_status"}, timeout_s)
            ok, payload, _reason = self._transport_payload(result)
            if ok:
                try:
                    specs.extend(parse_tailnet_status(payload))
                    authoritative = True
                except DiscoveryError:
                    authoritative = False

        for key in ("hosts", "host", "operator_hosts"):
            if key in options:
                value = options[key]
                values = [value] if isinstance(value, (str, Mapping)) else list(value) if isinstance(value, Sequence) else None
                if values is None:
                    specs.extend(self._normalise_host_specs(value, options.get("ssh_user")))
                else:
                    for item in values:
                        if isinstance(item, str) and (".." in item or _BRACKET_RANGE.fullmatch(item)):
                            specs.extend(self._expand_ranges(item, options.get("ssh_user"), max_hosts))
                        else:
                            specs.extend(self._normalise_host_specs(item, options.get("ssh_user")))
        for key in ("ranges", "host_ranges", "operator_ranges", "range", "host_range"):
            if key in options:
                specs.extend(self._expand_ranges(options[key], options.get("ssh_user"), max_hosts))

        if not specs and not explicit and not authoritative:
            for item in current.get("hosts", []):
                if isinstance(item, Mapping):
                    specs.append({"host_id": item.get("host_id"), "ssh_endpoint": item.get("ssh_endpoint"), "ssh_user": item.get("ssh_user"), "source": "current"})
        deduped: dict[tuple[str, str], dict[str, Any]] = {}
        for spec in specs:
            normal = self._normalise_host_spec(spec, options.get("ssh_user"))
            key = (str(normal["host_id"]), str(normal["ssh_endpoint"]))
            if key not in deduped:
                deduped[key] = normal
        ordered = sorted(deduped.values(), key=lambda item: (str(item["host_id"]), str(item["ssh_endpoint"])))
        return ordered[:max_hosts], authoritative

    def _normalise_host_specs(self, value: object, default_user: object) -> list[dict[str, Any]]:
        if isinstance(value, (str, Mapping)):
            return [self._normalise_host_spec(value, default_user)]
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            return [self._normalise_host_spec(item, default_user) for item in value]
        raise DiscoveryError("hosts must be a host or a finite host list")

    def _normalise_host_spec(self, value: object, default_user: object) -> dict[str, Any]:
        explicit_user = False
        if isinstance(value, str):
            user = default_user
            endpoint = value.strip()
            if "@" in endpoint and not endpoint.startswith("@"):
                possible_user, possible_endpoint = endpoint.split("@", 1)
                if possible_user and possible_endpoint:
                    user, endpoint = possible_user, possible_endpoint
                    explicit_user = True
            if user is not None and str(user).strip():
                explicit_user = explicit_user or default_user is not None
            data: Mapping[str, Any] = {"ssh_endpoint": endpoint, "ssh_user": user, "_ssh_user_explicit": explicit_user}
        elif isinstance(value, Mapping):
            data = value
            marker = data.get("_ssh_user_explicit")
            if marker is not None:
                explicit_user = bool(marker)
            else:
                explicit_user = any(
                    key in data and data.get(key) is not None and str(data.get(key)).strip()
                    for key in ("ssh_user", "user", "username")
                )
                if not explicit_user and default_user is not None and str(default_user).strip():
                    explicit_user = True
        else:
            raise DiscoveryError("host entry must be a string or mapping")
        endpoint = data.get("ssh_endpoint", data.get("endpoint", data.get("host", data.get("address", data.get("name", data.get("host_id"))))))
        if endpoint is None or not str(endpoint).strip():
            raise DiscoveryError("host entry lacks an SSH endpoint")
        endpoint_text = str(endpoint).strip()
        if endpoint_text.startswith("ssh://"):
            endpoint_text = endpoint_text[6:]
        if "@" in endpoint_text:
            endpoint_text = endpoint_text.rsplit("@", 1)[1]
        if endpoint_text.count(":") == 1 and endpoint_text.rsplit(":", 1)[1].isdigit():
            endpoint_text = endpoint_text.rsplit(":", 1)[0]
        endpoint_text = endpoint_text.rstrip(".")
        if not _ENDPOINT.fullmatch(endpoint_text):
            raise DiscoveryError(f"unsupported SSH endpoint: {endpoint_text}")
        host_id = data.get("host_id", data.get("id", data.get("name", endpoint_text)))
        user = data.get("ssh_user")
        if user is None or not str(user).strip():
            for key in ("user", "username"):
                candidate = data.get(key)
                if candidate is not None and str(candidate).strip():
                    user = candidate
                    break
        if user is None or not str(user).strip():
            user = default_user
        if user is None or not str(user).strip():
            user = "runner"
        user_text = _short_id(user, prefix="user")
        return {"host_id": _short_id(host_id, prefix="host"), "ssh_endpoint": endpoint_text, "ssh_user": user_text, "online": data.get("online", True), "source": data.get("source", "operator"), "_ssh_user_explicit": explicit_user}

    def _expand_ranges(self, value: object, default_user: object, max_hosts: int) -> list[dict[str, Any]]:
        items = [value] if isinstance(value, (str, Mapping)) else list(value) if isinstance(value, Sequence) else None
        if items is None:
            raise DiscoveryError("host ranges must be finite")
        result: list[dict[str, Any]] = []
        for item in items:
            if isinstance(item, Mapping):
                start, end = item.get("start"), item.get("end")
                prefix = item.get("prefix")
                width = item.get("width")
                if start is None or end is None:
                    raise DiscoveryError("host range mapping requires start and end")
                if prefix is not None:
                    start_text, end_text = f"{prefix}{start}", f"{prefix}{end}"
                else:
                    start_text, end_text = str(start), str(end)
                range_match = _RANGE_REPEATED_PREFIX.fullmatch(f"{start_text}..{end_text}") or _RANGE_SUFFIX.fullmatch(f"{start_text}..{end_text}")
                if range_match is None:
                    try:
                        start_num, end_num = int(start), int(end)
                    except (TypeError, ValueError) as exc:
                        raise DiscoveryError("host range bounds must be numeric") from exc
                    prefix_text = str(prefix or "host-")
                    width_num = int(width or 0)
                    remaining = max_hosts - len(result)
                    values = []
                    for number in range(start_num, end_num + 1):
                        if len(values) >= remaining:
                            break
                        values.append(f"{prefix_text}{number:0{width_num}d}" if width_num else f"{prefix_text}{number}")
                    result.extend(self._normalise_host_specs(values, default_user))
                    continue
                item = f"{start_text}..{end_text}"
            if not isinstance(item, str):
                raise DiscoveryError("host range must be a string or mapping")
            match = _BRACKET_RANGE.fullmatch(item) or _RANGE_REPEATED_PREFIX.fullmatch(item) or _RANGE_SUFFIX.fullmatch(item)
            if not match:
                raise DiscoveryError(f"unsupported host range: {item}")
            group = match.groupdict()
            prefix = group.get("prefix", "")
            suffix = group.get("suffix", "") or ""
            start_num, end_num = int(group["start"]), int(group["end"])
            if end_num < start_num:
                raise DiscoveryError("host range end precedes start")
            width = max(len(group["start"]), len(group["end"]))
            for number in range(start_num, end_num + 1):
                if len(result) >= max_hosts:
                    break
                name = f"{prefix}{number:0{width}d}{suffix}" if width > 1 else f"{prefix}{number}{suffix}"
                result.extend(self._normalise_host_specs(name, default_user))
        return result[:max_hosts]

    def _request(self, endpoint: str, message: Mapping[str, Any], timeout_s: float) -> object:
        if self.transport is None:
            return {"status": "unavailable", "error": "transport is not configured"}
        try:
            return self.transport.request(endpoint, message, timeout_s)
        except Exception:  # an injected transport failure is an observation, not a crash
            return {"status": "failure"}

    @staticmethod
    def _transport_payload(result: object) -> tuple[bool, object, str | None]:
        if isinstance(result, str):
            return True, result, None
        if not isinstance(result, Mapping):
            return False, {}, "transport returned no mapping"
        status = str(result.get("status", "ok")).lower()
        if status not in {"ok", "success", "confirmed", ""}:
            return False, {}, status
        if result.get("ok") is False:
            return False, {}, "failure"
        payload: object = result.get("response", result.get("data", result))
        if isinstance(payload, Mapping):
            nested_status = str(payload.get("ssh_probe", "")).lower()
            if nested_status in {"timeout", "denied", "lost", "unreachable", "unknown", "failure"}:
                return False, {}, nested_status
        return True, payload, None

    def _probe_host(self, spec: Mapping[str, Any], current_host: Mapping[str, Any] | None, now: str, timeout_s: float, used_device_ids: set[str]) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
        host_id = str(spec["host_id"])
        endpoint = str(spec["ssh_endpoint"])
        user = str(spec.get("ssh_user") or (current_host or {}).get("ssh_user") or "runner")
        base = {"host_id": host_id, "ssh_endpoint": endpoint, "ssh_user": user}
        observations: list[dict[str, Any]] = []
        result = self._request(endpoint, {"kind": "discovery", "ssh_user": user, "commands": ["nvidia-smi", "amd-sysfs"]}, timeout_s)
        ok, payload, reason = self._transport_payload(result)
        if not ok:
            failure = self._failure_reason(reason)
            host = self._unknown_host(base, failure, current_host)
            observations.append(self._observation("ssh", host_id, now, "unknown", None, failure, "ssh"))
            return host, observations, {"missing": False, "hardware_changed": True, "probe": _unknown_gpu(failure), "failed": True}
        observations.append(self._observation("ssh", host_id, now, "confirmed", {"endpoint": endpoint}, None, "ssh"))
        gpu_payload: object | None = self._gpu_payload_from_transport(payload)
        if gpu_payload is None and self.gpu_probe is not None:
            try:
                gpu_payload = self.gpu_probe.inspect(host_id)
            except Exception:
                gpu_payload = {"status": "unknown", "reason": "GPU probe failed"}
        gpu = _normalise_gpu_result(gpu_payload if gpu_payload is not None else {})
        old = current_host
        host, meta = self._merge_host(base, old, gpu, now, used_device_ids)
        gpu_status = "confirmed" if gpu["count"] is not None else "unknown"
        observations.append(self._observation("gpu", host_id, now, gpu_status, {"count": gpu["count"]}, gpu.get("reason"), self._gpu_source(payload, gpu_payload)))
        for device in host["devices"]:
            status = "confirmed" if not device["unknown_reasons"] else "unknown"
            observations.append(self._observation("device", host_id, now, status, {key: device[key] for key in ("device_id", "vendor", "model", "vram_bytes")}, next(iter(device["unknown_reasons"].values()), None), "nvidia-smi" if device.get("vendor") == "nvidia" else "sysfs"))
            if device.get("driver") is not None:
                observations.append(self._observation("driver", host_id, now, "confirmed", {"device_id": device["device_id"], "driver": device["driver"]}, None, "nvidia-smi" if device.get("vendor") == "nvidia" else "sysfs"))
        meta["failed"] = False
        return host, observations, meta

    @staticmethod
    def _gpu_payload_from_transport(payload: object) -> object | None:
        if isinstance(payload, str):
            return payload
        if not isinstance(payload, Mapping):
            return None
        for key in ("gpu", "gpu_probe", "nvidia_smi", "nvidia_smi_output", "amd_sysfs", "sysfs", "sysfs_output", "stdout", "raw", "devices", "count", "no_gpu"):
            if key in payload:
                if key in {"nvidia_smi", "nvidia_smi_output", "amd_sysfs", "sysfs", "sysfs_output", "stdout", "raw"} and isinstance(payload.get(key), str):
                    family = payload.get("family", "amd" if key in {"amd_sysfs", "sysfs", "sysfs_output"} else "nvidia")
                    return {key: payload[key], "family": family}
                return payload
        return None

    @staticmethod
    def _gpu_source(payload: object, gpu_payload: object) -> str:
        if isinstance(gpu_payload, Mapping) and any(key in gpu_payload for key in ("amd_sysfs", "sysfs")):
            return "sysfs"
        if isinstance(gpu_payload, Mapping) and gpu_payload.get("family") in {"amd", "sysfs"}:
            return "sysfs"
        if isinstance(payload, Mapping) and payload.get("family") in {"amd", "sysfs"}:
            return "sysfs"
        return "nvidia-smi"

    @staticmethod
    def _failure_reason(reason: str | None) -> str:
        return _safe_diagnostic(reason, default="host probe failed")

    def _merge_host(self, base: Mapping[str, Any], old: Mapping[str, Any] | None, gpu: Mapping[str, Any], now: str, used_device_ids: set[str]) -> tuple[dict[str, Any], dict[str, Any]]:
        old_devices = [item for item in (old or {}).get("devices", []) if isinstance(item, Mapping)]
        devices = self._assign_device_ids(str(base["host_id"]), gpu.get("devices", []), old_devices, used_device_ids)
        old_ids = {str(item.get("device_id")): item for item in old_devices if item.get("device_id")}
        new_ids = {str(item["device_id"]) for item in devices}
        missing_ids = sorted(set(old_ids) - new_ids)
        hardware_changed = False
        if missing_ids:
            hardware_changed = True
            for device_id in missing_ids:
                devices.append(self._unknown_device(device_id, "device not observed; retained for review"))
        devices.sort(key=lambda item: item["device_id"])
        count = gpu.get("count")
        reason = gpu.get("reason")
        if missing_ids:
            count = None
            reason = "device set changed; retained missing device records"
        if old is not None and self._hardware_signature(old.get("devices", [])) != self._hardware_signature(devices):
            hardware_changed = True
        if old is not None and old.get("gpu_count") != count:
            hardware_changed = True
        admissible = count is not None and (count == 0 or all(not item["unknown_reasons"] for item in devices))
        host = {
            "host_id": str(base["host_id"]),
            "ssh_endpoint": str(base["ssh_endpoint"]),
            "ssh_user": str(base.get("ssh_user") or "runner"),
            "reachability": "confirmed",
            "observed_at": now,
            "observation_error": None,
            "gpu_count": count,
            "gpu_count_reason": reason,
            "devices": devices,
            "admissible": bool(admissible),
        }
        return host, {"missing": False, "hardware_changed": hardware_changed, "probe": gpu, "failed": False}

    @staticmethod
    def _hardware_signature(devices: object) -> list[tuple[Any, ...]]:
        result: list[tuple[Any, ...]] = []
        if isinstance(devices, Sequence) and not isinstance(devices, (str, bytes, bytearray)):
            for device in devices:
                if isinstance(device, Mapping):
                    result.append(tuple(device.get(field) for field in ("device_id", "vendor", "model", "vram_bytes", "driver")))
        return sorted(result, key=_json_value)

    @staticmethod
    def _device_fingerprint(device: Mapping[str, Any]) -> str:
        return _json_value({field: device.get(field) for field in _GPU_FIELDS})

    @staticmethod
    def _fresh_device_id(host_id: str, fingerprint: str, occurrence: int, used_ids: set[str]) -> str:
        digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]
        stem = f"{host_id}-gpu-{digest}-{occurrence}"
        candidate = _short_id(stem, prefix="gpu")
        if candidate not in used_ids:
            return candidate
        for salt in range(1, 256):
            candidate = _short_id(f"{stem}-{salt}", prefix="gpu")
            if candidate not in used_ids:
                return candidate
        raise DiscoveryError("unable to allocate a stable device id")

    @staticmethod
    def _assign_device_ids(host_id: str, observed: object, old_devices: Sequence[Mapping[str, Any]], used_ids: set[str]) -> list[dict[str, Any]]:
        items = [item for item in observed if isinstance(item, Mapping)] if isinstance(observed, Sequence) and not isinstance(observed, (str, bytes, bytearray)) else []
        old_by_fingerprint: dict[str, list[str]] = {}
        for item in old_devices:
            fingerprint = DiscoveryHandler._device_fingerprint(item)
            old_by_fingerprint.setdefault(fingerprint, []).append(str(item.get("device_id")))
        for matching in old_by_fingerprint.values():
            matching.sort()
        items = sorted(items, key=DiscoveryHandler._device_fingerprint)
        assigned: list[dict[str, Any]] = []
        local_used = set(used_ids)
        old_ids_in_order = sorted(str(item.get("device_id")) for item in old_devices if item.get("device_id"))
        occurrences: dict[str, int] = {}
        for index, item in enumerate(items):
            fingerprint = DiscoveryHandler._device_fingerprint(item)
            occurrence = occurrences.get(fingerprint, 0)
            occurrences[fingerprint] = occurrence + 1
            candidate = None
            matching = old_by_fingerprint.get(fingerprint, [])
            while matching:
                possible = matching.pop(0)
                if possible not in local_used:
                    candidate = possible
                    break
            if candidate is None and index < len(old_ids_in_order) and old_ids_in_order[index] not in local_used:
                candidate = old_ids_in_order[index]
            if candidate is None:
                candidate = DiscoveryHandler._fresh_device_id(host_id, fingerprint, occurrence, local_used)
            local_used.add(candidate)
            assigned.append(_device(candidate, vendor=item.get("vendor"), model=item.get("model"), vram_bytes=item.get("vram_bytes"), driver=item.get("driver"), reasons=item.get("unknown_reasons") if isinstance(item.get("unknown_reasons"), Mapping) else None))
        return assigned

    @staticmethod
    def _unknown_device(device_id: str, reason: str) -> dict[str, Any]:
        return _device(device_id, reasons={field: reason for field in _GPU_FIELDS})

    @staticmethod
    def _unknown_host(base: Mapping[str, Any], reason: str, old_host: Mapping[str, Any] | None = None) -> dict[str, Any]:
        devices = [
            DiscoveryHandler._unknown_device(str(item["device_id"]), reason)
            for item in (old_host or {}).get("devices", [])
            if isinstance(item, Mapping) and item.get("device_id")
        ]
        return {
            "host_id": base["host_id"],
            "ssh_endpoint": base["ssh_endpoint"],
            "ssh_user": base.get("ssh_user") or "runner",
            "reachability": "unknown",
            "observed_at": None,
            "observation_error": reason,
            "gpu_count": None,
            "gpu_count_reason": reason,
            "devices": devices,
            "admissible": False,
        }

    @staticmethod
    def _retain_host(old_host: Mapping[str, Any]) -> dict[str, Any]:
        host = {key: deepcopy(old_host.get(key)) for key in ("host_id", "ssh_endpoint", "ssh_user", "reachability", "observed_at", "observation_error", "gpu_count", "gpu_count_reason", "devices")}
        host["admissible"] = bool(host.get("reachability") == "confirmed" and host.get("gpu_count") is not None and all(not item.get("unknown_reasons") for item in host.get("devices", []) if isinstance(item, Mapping)))
        return host

    def _missing_host(self, old_host: Mapping[str, Any], now: str, reason: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        devices: list[dict[str, Any]] = []
        for item in old_host.get("devices", []):
            if isinstance(item, Mapping) and item.get("device_id"):
                devices.append(self._unknown_device(str(item["device_id"]), reason))
        host = {
            "host_id": old_host["host_id"],
            "ssh_endpoint": old_host.get("ssh_endpoint", old_host["host_id"]),
            "ssh_user": old_host.get("ssh_user", "runner"),
            "reachability": "unknown",
            "observed_at": None,
            "observation_error": reason,
            "gpu_count": None,
            "gpu_count_reason": reason,
            "devices": devices,
            "admissible": False,
        }
        return host, [self._observation("host-missing", str(old_host["host_id"]), now, "unknown", None, reason, "tailnet")]

    @staticmethod
    def _match_current(spec: Mapping[str, Any], current_hosts: Mapping[str, Mapping[str, Any]]) -> Mapping[str, Any] | None:
        host_id = str(spec.get("host_id"))
        if host_id in current_hosts:
            return current_hosts[host_id]
        endpoint = str(spec.get("ssh_endpoint"))
        for host in current_hosts.values():
            if str(host.get("ssh_endpoint")) == endpoint:
                return host
        return None

    def _configuration(self, options: Mapping[str, Any], current: Mapping[str, Any], now: str) -> dict[str, Any]:
        site_id = _short_id(options.get("site_id", current.get("site_id", "site-a")), prefix="site")
        controller = _copy_mapping(current.get("controller")) or _copy_mapping(options.get("controller"))
        if not controller:
            controller = {"controller_id": "controller", "endpoint": None, "account": None, "paths": [], "auth_state": "unresolved"}
        policy = _copy_mapping(current.get("policy")) or _copy_mapping(options.get("policy"))
        if not policy:
            policy = self._default_policy(now)
        identity = deepcopy(current.get("identity_mapping", options.get("identity_mapping", [])))
        chat_order = deepcopy(current.get("chat_lane_order", options.get("chat_lane_order", [])))
        timezone_name = str(current.get("timezone", options.get("timezone", "Etc/UTC")))
        return {"site_id": site_id, "controller": controller, "policy": policy, "identity_mapping": identity if isinstance(identity, list) else [], "chat_lane_order": chat_order if isinstance(chat_order, list) else [], "timezone": timezone_name}

    @staticmethod
    def _default_policy(now: str) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "policy_id": "default",
            "revision": 1,
            "updated_at": now,
            "purpose_rules": ["purpose-required"],
            "content_rules": ["acceptable-use"],
            "admission": {"max_clock_skew_s": 30, "heartbeat_s": 60, "queue_refresh_s": 60, "queue_expiry_s": 600, "booking_horizon_s": 1209600, "minimum_booking_s": 900, "agent_max_s": 14400, "operator_max_s": 43200},
            "security_hooks": {"supported_schemes": ["ssh-sk"], "unsupported_supplied_action": "deny"},
        }

    def _build_lanes(self, current: Mapping[str, Any], hosts: Sequence[Mapping[str, Any]], host_meta: Mapping[str, Mapping[str, Any]], config: Mapping[str, Any], now: str) -> tuple[list[dict[str, Any]], list[str]]:
        host_map = {str(host["host_id"]): host for host in hosts}
        old_lanes = [item for item in current.get("lanes", []) if isinstance(item, Mapping) and item.get("lane_id")]
        lanes: list[dict[str, Any]] = []
        used_lane_ids: set[str] = set()
        assigned_devices: set[str] = set()
        old_device_ids_by_host: dict[str, set[str]] = {}
        first_gpu_lane_by_host: dict[str, str] = {}
        for old in old_lanes:
            old_host_id = str(old.get("host_id"))
            old_device_ids_by_host.setdefault(old_host_id, set()).update(str(item) for item in old.get("device_ids", []) if item is not None)
            if old.get("class") == "gpu":
                first_gpu_lane_by_host.setdefault(old_host_id, str(old["lane_id"]))
        for old in sorted(old_lanes, key=lambda item: str(item["lane_id"])):
            lane = {key: deepcopy(old.get(key)) for key in ("lane_id", "host_id", "device_ids", "class", "enabled", "policy")}
            lane["device_ids"] = list(dict.fromkeys(str(item) for item in lane.get("device_ids", []) if item is not None))
            host_id = str(lane.get("host_id"))
            host = host_map.get(host_id)
            meta = host_meta.get(host_id, {})
            current_enabled = bool(old.get("enabled", False))
            changed = bool(meta.get("hardware_changed"))
            if host is None or host.get("reachability") != "confirmed" or not host.get("admissible"):
                lane["enabled"] = False
                lane["action"] = "retain" if meta.get("missing") else "review"
            else:
                available = {str(item["device_id"]): item for item in host.get("devices", []) if isinstance(item, Mapping) and item.get("device_id")}
                old_ids = list(lane["device_ids"])
                missing_refs = [item for item in old_ids if item not in available]
                if missing_refs:
                    changed = True
                if lane.get("class") == "gpu":
                    old_host_device_ids = old_device_ids_by_host.get(host_id, set())
                    additions = set(available) - old_host_device_ids if not missing_refs else set()
                    if not old_host_device_ids and str(lane["lane_id"]) == first_gpu_lane_by_host.get(host_id):
                        additions = set(available)
                    if str(lane["lane_id"]) == first_gpu_lane_by_host.get(host_id):
                        for device_id in sorted(additions):
                            if device_id not in assigned_devices and device_id not in lane["device_ids"]:
                                lane["device_ids"].append(device_id)
                                changed = True
                lane["device_ids"] = sorted(set(lane["device_ids"]))
                lane["enabled"] = current_enabled and not changed
                lane["action"] = "review" if changed else "retain"
            assigned_devices.update(lane["device_ids"])
            used_lane_ids.add(str(lane["lane_id"]))
            lanes.append(lane)

        new_lane_ids: list[str] = []
        for host in sorted(hosts, key=lambda item: str(item["host_id"])):
            host_id = str(host["host_id"])
            if host.get("reachability") != "confirmed" or not host.get("admissible") or not isinstance(host.get("gpu_count"), int) or host.get("gpu_count", 0) <= 0:
                continue
            devices = [str(item["device_id"]) for item in host.get("devices", []) if isinstance(item, Mapping) and item.get("device_id") and not item.get("unknown_reasons")]
            if not devices:
                continue
            if any(str(lane.get("host_id")) == host_id for lane in lanes):
                continue
            lane_id = self._new_lane_id(host_id, devices, used_lane_ids)
            lane = {"lane_id": lane_id, "host_id": host_id, "device_ids": sorted(devices), "class": "gpu", "enabled": True, "policy": self._new_lane_policy(config["site_id"], now), "action": "add"}
            lanes.append(lane)
            used_lane_ids.add(lane_id)
            new_lane_ids.append(lane_id)
        lanes.sort(key=lambda item: str(item["lane_id"]))
        return lanes, sorted(new_lane_ids)

    @staticmethod
    def _new_lane_id(host_id: str, devices: Sequence[str], used: set[str]) -> str:
        candidates = []
        if devices:
            candidates.append(_short_id(f"lane-{devices[0]}", prefix="lane"))
        candidates.append(_short_id(f"lane-{host_id}-gpu", prefix="lane"))
        for number in range(1001):
            candidates.append(_short_id(f"lane-{host_id}-gpu-{number}", prefix="lane"))
        for candidate in candidates:
            if candidate not in used:
                return candidate
        raise DiscoveryError("unable to allocate a stable lane id")

    @staticmethod
    def _new_lane_policy(site_id: str, now: str) -> dict[str, Any]:
        return {
            "site_id": site_id,
            "capability_class": "gpu",
            "capabilities": ["compute"],
            "isolation": ["cgroup"],
            "sanitisation": ["gpu-reset"],
            "assurance": {"required": [], "offered": ["observed"], "verified": [], "evidence_refs": [], "unknown": False},
            "observed_at": now,
            "provenance": ["discovery"],
        }

    @staticmethod
    def _chat_order(current: Mapping[str, Any], config: Mapping[str, Any], lanes: Sequence[Mapping[str, Any]], new_lane_ids: Sequence[str]) -> list[str]:
        existing = current.get("chat_lane_order", config.get("chat_lane_order", []))
        order = [str(item) for item in existing if item is not None]
        known = {str(lane["lane_id"]) for lane in lanes}
        order.extend(item for item in sorted(new_lane_ids) if item not in order)
        return [item for item in order if item in known]

    @staticmethod
    def _observation(kind: str, host_id: str, observed_at: str, status: str, value: object, reason: object, source: str) -> dict[str, Any]:
        suffix = _short_id(host_id, prefix="host")
        if kind in {"device", "driver"} and isinstance(value, Mapping) and value.get("device_id"):
            suffix = _short_id(f"{suffix}-{value['device_id']}", prefix="obs")
        observation_id = _identifier(f"obs-{kind}-{suffix}", prefix="obs")
        safe_reason = _safe_reason(reason, default="observation unavailable") if reason else None
        return {"observation_id": observation_id, "host_id": host_id, "kind": kind, "status": status, "observed_at": observed_at, "value": deepcopy(value), "reason": safe_reason, "source": source if source in {"operator", "tailnet", "ssh", "nvidia-smi", "sysfs"} else "operator"}

    @staticmethod
    def _sorted_observations(observations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        unique: dict[tuple[str, str], Mapping[str, Any]] = {}
        for item in observations:
            unique[(str(item.get("kind")), str(item.get("observation_id")))] = item
        return [deepcopy(item) for _key, item in sorted(unique.items(), key=lambda pair: pair[0])]

    def _diff(self, current: Mapping[str, Any], proposal: Mapping[str, Any]) -> list[dict[str, Any]]:
        before_hosts = {str(item.get("host_id")): item for item in current.get("hosts", []) if isinstance(item, Mapping) and item.get("host_id")}
        after_hosts = {str(item.get("host_id")): item for item in proposal.get("hosts", []) if isinstance(item, Mapping) and item.get("host_id")}
        before_lanes = {str(item.get("lane_id")): item for item in current.get("lanes", []) if isinstance(item, Mapping) and item.get("lane_id")}
        after_lanes = {str(item.get("lane_id")): item for item in proposal.get("lanes", []) if isinstance(item, Mapping) and item.get("lane_id")}
        entries: list[dict[str, Any]] = []
        for host_id in sorted(set(before_hosts) | set(after_hosts)):
            old, new = before_hosts.get(host_id), after_hosts.get(host_id)
            if old is None:
                entries.append(self._diff_entry("host", host_id, "added", None, {"reachability": new.get("reachability")}, True))
                entries.extend(self._device_diffs({}, new))
                continue
            elif new is None:
                entries.append(self._diff_entry("host", host_id, "unknown", {"reachability": old.get("reachability")}, None, True))
                entries.extend(self._device_diffs(old, {}))
                continue
            elif old.get("reachability") != new.get("reachability") and new.get("reachability") != "confirmed":
                entries.append(self._diff_entry("host", host_id, "unknown", {"reachability": old.get("reachability")}, {"reachability": new.get("reachability")}, True))
            elif self._host_without_devices(old) != self._host_without_devices(new):
                entries.append(self._diff_entry("host", host_id, "changed", self._host_without_devices(old), self._host_without_devices(new), True))
            entries.extend(self._device_diffs(old, new))
        for lane_id in sorted(set(before_lanes) | set(after_lanes)):
            old, new = before_lanes.get(lane_id), after_lanes.get(lane_id)
            if old is None:
                entries.append(self._diff_entry("lane", lane_id, "added", None, {"host_id": new.get("host_id"), "device_ids": new.get("device_ids")}, True))
            elif new is None:
                entries.append(self._diff_entry("lane", lane_id, "retained", {"enabled": old.get("enabled")}, None, True))
            elif {key: old.get(key) for key in ("host_id", "device_ids", "class", "enabled", "policy")} != {key: new.get(key) for key in ("host_id", "device_ids", "class", "enabled", "policy")}:
                change = "retained" if not new.get("enabled") and old.get("enabled") else "changed"
                entries.append(self._diff_entry("lane", lane_id, change, {"enabled": old.get("enabled"), "device_ids": old.get("device_ids")}, {"enabled": new.get("enabled"), "device_ids": new.get("device_ids")}, True))
        return sorted(entries, key=lambda item: item["sort_key"])

    @staticmethod
    def _host_without_devices(host: Mapping[str, Any]) -> dict[str, Any]:
        return {key: host.get(key) for key in ("ssh_endpoint", "ssh_user", "reachability", "observation_error", "gpu_count", "gpu_count_reason")}

    def _device_diffs(self, old: Mapping[str, Any], new: Mapping[str, Any]) -> list[dict[str, Any]]:
        old_devices = {str(item.get("device_id")): item for item in old.get("devices", []) if isinstance(item, Mapping) and item.get("device_id")}
        new_devices = {str(item.get("device_id")): item for item in new.get("devices", []) if isinstance(item, Mapping) and item.get("device_id")}
        result: list[dict[str, Any]] = []
        for device_id in sorted(set(old_devices) | set(new_devices)):
            before, after = old_devices.get(device_id), new_devices.get(device_id)
            if before is None:
                result.append(self._diff_entry("device", device_id, "added", None, {key: after.get(key) for key in _GPU_FIELDS}, True))
                continue
            if after is None:
                result.append(self._diff_entry("device", device_id, "retained", {key: before.get(key) for key in _GPU_FIELDS}, None, True))
                continue
            if any(after.get(field) is None and before.get(field) is not None for field in _GPU_FIELDS):
                result.append(self._diff_entry("unknown", device_id, "unknown", {key: before.get(key) for key in _GPU_FIELDS}, {key: after.get(key) for key in _GPU_FIELDS}, True))
                continue
            if any(before.get(field) != after.get(field) for field in ("vendor", "model", "vram_bytes")):
                result.append(self._diff_entry("gpu", device_id, "changed", {key: before.get(key) for key in ("vendor", "model", "vram_bytes")}, {key: after.get(key) for key in ("vendor", "model", "vram_bytes")}, True))
            if before.get("driver") != after.get("driver"):
                result.append(self._diff_entry("driver", device_id, "changed", before.get("driver"), after.get("driver"), True))
        return result

    @staticmethod
    def _diff_entry(kind: str, identifier: str, change: str, before: object, after: object, review_required: bool) -> dict[str, Any]:
        return {"sort_key": f"{kind}/{identifier}", "kind": kind, "id": identifier, "change": change, "before": deepcopy(before), "after": deepcopy(after), "review_required": review_required}


def discover(
    options: Mapping[str, Any],
    *,
    transport: Any = None,
    clock: Any = None,
    gpu_probe: Any = None,
    current_inventory: Mapping[str, Any] | str | Path | None = None,
) -> dict[str, Any]:
    """Convenience entry point used by the P3 adapter without importing it."""

    return DiscoveryHandler(transport, clock, gpu_probe, current_inventory=current_inventory).discover(options)


Discovery = DiscoveryHandler
proposal_to_inventory = project_proposal
parse_tailscale_status = parse_tailnet_status


__all__ = [
    "DiscoveryError",
    "ProjectionError",
    "DiscoveryHandler",
    "Discovery",
    "discover",
    "parse_amd_sysfs",
    "parse_nvidia_smi",
    "parse_tailnet_status",
    "parse_tailscale_status",
    "project_proposal",
    "proposal_to_inventory",
]
