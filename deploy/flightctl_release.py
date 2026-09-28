"""Fail-closed staging, rollout, smoke, and rollback orchestration.

This module has two deliberately separate layers:

* ReleaseManager owns immutable manifest and compatibility checks.
* FileReleaseBackend records safe mutations in a local state root.

The file backend is a rehearsal backend. It never calls a network, systemd,
GPU, SSH, or process API. P1-P7 can inject a backend with the same small
method surface when the assembled release is available.
"""

from __future__ import annotations

import argparse
import copy
import functools
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEMA_VERSION = 1
HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SMOKE_FAILURES = {"unknown", "timeout", "timed-out", "failed", "failure", "lost", "unavailable"}
REQUIRED_SMOKE_GATES = frozenset({"protocol", "hash", "authentication", "local-deadline", "cleanup"})
OCCUPANCY_FIELDS = ("occupants", "workloads", "chat_occupants", "legacy_occupants")


class ReleaseFailure(RuntimeError):
    """A refusal or failed mutation that must leave admission closed."""

    def __init__(self, message: str, *, code: str = "release-refused") -> None:
        super().__init__(message)
        self.code = code


class LiveOccupantFailure(ReleaseFailure):
    """A workload or legacy occupant remained after a drain request."""

    def __init__(self, message: str = "live occupant prevents a safe release") -> None:
        super().__init__(message, code="live-occupant")


class ReleaseBackend(Protocol):
    """Minimal mutation seam used by the release manager and its fakes."""

    def close_admission(self, reason: str) -> None: ...

    def pause_producers(self) -> None: ...

    def drain_workloads(self, release_id: str) -> None: ...

    def drain_chat(self, release_id: str) -> None: ...

    def inspect_empty(self, release_id: str) -> Mapping[str, Any]: ...

    def snapshot_state(self, release_id: str) -> Mapping[str, Any]: ...

    def disable_legacy_writers(self) -> None: ...

    def install_executors(self, manifest: Mapping[str, Any]) -> None: ...

    def install_authority(self, manifest: Mapping[str, Any]) -> None: ...

    def install_clients(self, manifest: Mapping[str, Any]) -> None: ...

    def install_adapters(self, manifest: Mapping[str, Any]) -> None: ...

    def install_watcher(self, manifest: Mapping[str, Any]) -> None: ...

    def verify_agreement(self, manifest: Mapping[str, Any]) -> None: ...

    def reopen_admission(self) -> None: ...

    def smoke_probe(self, gate: str, manifest: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def commit_release(self, manifest: Mapping[str, Any]) -> None: ...

    def restore_artifacts(self, manifest: Mapping[str, Any]) -> None: ...

    def restore_state(self, snapshot: Mapping[str, Any], newer: Mapping[str, Any]) -> None: ...

    def restore_inventory(self, manifest: Mapping[str, Any]) -> None: ...

    def reconcile_hardware(self, manifest: Mapping[str, Any]) -> None: ...

    def get_state(self) -> Mapping[str, Any]: ...


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: Any) -> bytes:
    return canonical_bytes(value) + b"\n"


def _write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_json(path: Path, value: Any, *, mode: int = 0o600) -> None:
    _write_bytes(path, _json_bytes(value), mode=mode)


def _read_json(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseFailure(f"cannot read JSON document: {path.name}", code="invalid-input") from exc


def _hash_or_fail(value: Any, label: str) -> str:
    if not isinstance(value, str) or not HASH_RE.fullmatch(value):
        raise ReleaseFailure(f"{label} must be a SHA-256 hash", code="invalid-input")
    return value.lower()


def _id_or_fail(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_ID_RE.fullmatch(value):
        raise ReleaseFailure(f"{label} is not a safe release identifier", code="invalid-input")
    return value


def _safe_relative(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ReleaseFailure(f"{label} must be a relative path", code="invalid-input")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ReleaseFailure(f"{label} must stay inside its bundle", code="invalid-input")
    if candidate == Path("."):
        raise ReleaseFailure(f"{label} must name a file", code="invalid-input")
    return candidate


def _regular_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ReleaseFailure(f"{label} is not a regular file", code="invalid-input")


def _versioned_hash(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ReleaseFailure(f"{label} requires version and hash", code="invalid-input")
    version = value.get("version", value.get("compatibility", value.get("format")))
    if not isinstance(version, (str, int)) or isinstance(version, bool) or not str(version):
        raise ReleaseFailure(f"{label} requires a version", code="invalid-input")
    digest = value.get("sha256", value.get("hash"))
    return {"version": version, "sha256": _hash_or_fail(digest, f"{label} hash")}


def _copy_mode(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError as exc:
        raise ReleaseFailure(f"cannot set protected mode on {path.name}", code="io") from exc


def _make_immutable(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_symlink():
            raise ReleaseFailure("staged bundle contains a symlink", code="invalid-input")
        if path.is_dir():
            _copy_mode(path, 0o500)
        else:
            _copy_mode(path, 0o400)
    _copy_mode(root, 0o500)


def _make_owner_only_tree(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ReleaseFailure("protected bundle contains a symlink", code="io")
        _copy_mode(path, 0o700 if path.is_dir() else 0o600)


@dataclass(frozen=True)
class ReleasePaths:
    """Filesystem locations for one rehearsal or operator-configured rollout."""

    state_dir: Path
    backup_dir: Path | None = None
    inventory_path: Path | None = None
    policy_path: Path | None = None
    rollout_config_path: Path | None = None

    @property
    def staging_dir(self) -> Path:
        return self.state_dir / "staged"

    @property
    def snapshot_dir(self) -> Path:
        return self.state_dir / "snapshots"

    @classmethod
    def from_env(cls, base: Path | None = None) -> "ReleasePaths":
        base_dir = (base or Path.cwd()).resolve()

        def path_value(name: str) -> Path | None:
            raw = os.environ.get(name)
            if not raw:
                return None
            path = Path(raw)
            return path if path.is_absolute() else base_dir / path

        state_raw = os.environ.get("FLIGHTCTL_RELEASE_STATE_DIR", ".flightctl-release-state")
        state = Path(state_raw)
        if not state.is_absolute():
            state = base_dir / state
        return cls(
            state_dir=state,
            backup_dir=path_value("FLIGHTCTL_BACKUP_DIR"),
            inventory_path=path_value("FLIGHTCTL_INVENTORY"),
            policy_path=path_value("FLIGHTCTL_POLICY"),
            rollout_config_path=path_value("FLIGHTCTL_ROLLOUT_CONFIG"),
        )


class FileReleaseBackend:
    """Local mutation backend used by the real release command and scaffolding."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- 0o700 is owner-only (stricter than the rule's 0o644)
        except OSError:
            pass
        self.state_path = self.root / "state.json"
        self.trace_path = self.root / "trace.jsonl"
        if self.state_path.exists():
            state = _read_json(self.state_path)
            if not isinstance(state, dict):
                raise ReleaseFailure("release state is not an object", code="invalid-state")
            self._state: dict[str, Any] = state
        else:
            self._state = {
                "schema_version": SCHEMA_VERSION,
                "admission": "closed",
                "legacy_writers_enabled": True,
                "producers_paused": False,
                "occupants": [],
                "workloads": [],
                "chat_occupants": [],
                "legacy_occupants": [],
                "trace": [],
                "state_revision": 0,
            }
            self._save()

    def _save(self) -> None:
        self._state["state_revision"] = int(self._state.get("state_revision", 0)) + 1
        _write_json(self.state_path, self._state)

    def _step(self, name: str) -> None:
        self._state.setdefault("trace", []).append(name)
        _write_bytes(
            self.trace_path,
            ("\n".join(json.dumps(item, sort_keys=True) for item in self._state["trace"]) + "\n").encode("utf-8"),
            mode=0o600,
        )
        self._save()
        failure = self._state.get("fail_step") or os.environ.get("FLIGHTCTL_RELEASE_FAIL_STEP")
        if failure and str(failure) in {name, name.split(":", 1)[0], "all"}:
            raise ReleaseFailure(f"release step failed: {name}", code="step-failed")

    def get_state(self) -> Mapping[str, Any]:
        return copy.deepcopy(self._state)

    def close_admission(self, reason: str) -> None:
        self._state["admission"] = "closed"
        self._state["admission_reason"] = reason
        self._save()

    def pause_producers(self) -> None:
        self._step("pause")
        self._state["producers_paused"] = True
        self._save()

    def drain_workloads(self, release_id: str) -> None:
        self._step("drain_workloads")
        self._state["workloads_drained_for"] = release_id
        self._save()

    def drain_chat(self, release_id: str) -> None:
        self._step("drain_chat")
        self._state["chat_drained_for"] = release_id
        self._save()

    def inspect_empty(self, release_id: str) -> Mapping[str, Any]:
        self._step("inspect")
        occupants: list[Any] = []
        for key in OCCUPANCY_FIELDS:
            value = self._state.get(key)
            if not isinstance(value, list):
                self._state["admission"] = "closed"
                self._state["quarantined"] = True
                self._state["quarantine_reason"] = f"{key} occupancy is unknown"
                self._save()
                raise ReleaseFailure(f"{key} occupancy is unknown", code="occupancy-unknown")
            occupants.extend(value)
        if occupants:
            self._state["admission"] = "closed"
            self._state["quarantined"] = True
            self._state["quarantine_reason"] = "occupant remained during release drain"
            self._save()
            raise LiveOccupantFailure()
        self._state["empty_confirmed_for"] = release_id
        self._save()
        return {"empty": True, "occupants": []}

    def snapshot_state(self, release_id: str) -> Mapping[str, Any]:
        self._step("snapshot")
        state = copy.deepcopy(self._state)
        state.pop("trace", None)
        state["snapshot_for"] = release_id
        return state

    def disable_legacy_writers(self) -> None:
        self._step("disable_legacy")
        self._state["legacy_writers_enabled"] = False
        self._save()

    def _install(self, name: str, manifest: Mapping[str, Any]) -> None:
        self._step(name)
        installed = self._state.setdefault("installed_components", [])
        if name not in installed:
            installed.append(name)
        self._state["pending_release"] = manifest.get("release_id")
        self._state["pending_manifest_hash"] = manifest.get("manifest_sha256")
        self._save()

    def install_executors(self, manifest: Mapping[str, Any]) -> None:
        self._install("install_executors", manifest)

    def install_authority(self, manifest: Mapping[str, Any]) -> None:
        self._install("install_authority", manifest)

    def install_clients(self, manifest: Mapping[str, Any]) -> None:
        self._install("install_clients", manifest)

    def install_adapters(self, manifest: Mapping[str, Any]) -> None:
        self._install("install_adapters", manifest)

    def install_watcher(self, manifest: Mapping[str, Any]) -> None:
        self._install("install_watcher", manifest)

    def verify_agreement(self, manifest: Mapping[str, Any]) -> None:
        self._step("verify")
        if self._state.get("pending_release") != manifest.get("release_id"):
            raise ReleaseFailure("installed components do not agree on release", code="agreement-failed")
        if self._state.get("pending_manifest_hash") != manifest.get("manifest_sha256"):
            raise ReleaseFailure("installed components do not agree on manifest", code="agreement-failed")
        self._state["agreement_verified"] = True
        self._save()

    def reopen_admission(self) -> None:
        self._step("reopen")
        self._state["admission"] = "open"
        self._state["admission_reason"] = None
        self._save()

    def smoke_probe(self, gate: str, manifest: Mapping[str, Any]) -> Mapping[str, Any]:
        self._step(f"smoke:{gate}")
        smoke = self._state.get("smoke", {})
        result = smoke.get(gate, "unknown") if isinstance(smoke, Mapping) else "unknown"
        if isinstance(result, Mapping):
            return dict(result)
        return {"status": result, "gate": gate, "release_id": manifest.get("release_id")}

    def commit_release(self, manifest: Mapping[str, Any]) -> None:
        self._state["current_release"] = manifest.get("release_id")
        self._state["installed_release"] = manifest.get("release_id")
        self._state["current_manifest_hash"] = manifest.get("manifest_sha256")
        self._state["current_manifest"] = copy.deepcopy(dict(manifest))
        self._state["current_inventory_hash"] = manifest.get("inventory", {}).get("sha256")
        self._state["current_policy_hash"] = manifest.get("policy", {}).get("sha256")
        self._state["current_state_compatibility"] = copy.deepcopy(manifest.get("state_compatibility"))
        self._state["pending_release"] = None
        self._state["pending_manifest_hash"] = None
        self._state["legacy_writers_enabled"] = False
        self._save()

    def restore_artifacts(self, manifest: Mapping[str, Any]) -> None:
        self._step("restore_artifacts")
        self._state["pending_release"] = manifest.get("release_id")
        self._state["pending_manifest_hash"] = manifest.get("manifest_sha256")
        self._save()

    def restore_state(self, snapshot: Mapping[str, Any], newer: Mapping[str, Any]) -> None:
        self._step("restore_state")
        previous_trace = copy.deepcopy(self._state.get("trace", []))
        pending_release = self._state.get("pending_release")
        pending_manifest_hash = self._state.get("pending_manifest_hash")
        restored = copy.deepcopy(dict(snapshot))
        stable_keys = {
            "schema_version",
            "admission",
            "admission_reason",
            "legacy_writers_enabled",
            "producers_paused",
            "occupants",
            "workloads",
            "chat_occupants",
            "legacy_occupants",
            "current_release",
            "installed_release",
            "current_manifest_hash",
            "current_manifest",
            "current_inventory_hash",
            "current_policy_hash",
            "current_state_compatibility",
            "pending_release",
            "pending_manifest_hash",
            "trace",
            "state_revision",
        }
        for key, value in newer.items():
            if key not in stable_keys:
                restored[key] = copy.deepcopy(value)
        restored["trace"] = previous_trace
        restored["admission"] = "closed"
        restored["legacy_writers_enabled"] = False
        restored["pending_release"] = pending_release
        restored["pending_manifest_hash"] = pending_manifest_hash
        restored["preserved_newer_state_revision"] = newer.get("state_revision")
        self._state = restored
        self._save()

    def restore_inventory(self, manifest: Mapping[str, Any]) -> None:
        self._step("restore_inventory")
        self._state["current_inventory_hash"] = manifest.get("inventory", {}).get("sha256")
        self._state["current_policy_hash"] = manifest.get("policy", {}).get("sha256")
        self._save()

    def reconcile_hardware(self, manifest: Mapping[str, Any]) -> None:
        self._step("reconcile")
        self._state["reconciled_inventory_hash"] = manifest.get("inventory", {}).get("sha256")
        self._save()


@functools.lru_cache(maxsize=2)
def _contract_validator(kind: str) -> Any:
    """Load one frozen P0 schema without allowing remote reference resolution."""

    try:
        import jsonschema
        from referencing import Registry, Resource
    except ImportError as exc:
        raise ReleaseFailure("frozen contract validator is unavailable", code="contract-unavailable") from exc

    contracts_root = Path(__file__).resolve().parents[1] / "contracts"
    try:
        common = json.loads((contracts_root / "common.schema.json").read_text(encoding="utf-8"))
        inventory = json.loads((contracts_root / "inventory-v1.schema.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseFailure("frozen contract schema cannot be loaded", code="contract-unavailable") from exc
    resources = Registry().with_resources(
        [
            ("https://flightctl.local/contracts/common.schema.json", Resource.from_contents(common)),
            ("https://flightctl.local/contracts/inventory-v1.schema.json", Resource.from_contents(inventory)),
        ]
    )
    if kind == "inventory":
        schema = inventory
    elif kind == "policy":
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "https://flightctl.local/contracts/release-policy-v1.schema.json",
            "$ref": "https://flightctl.local/contracts/common.schema.json#/$defs/policy",
        }
    else:
        raise ReleaseFailure("unknown frozen contract kind", code="contract-unavailable")
    return jsonschema.Draft202012Validator(schema, registry=resources, format_checker=jsonschema.FormatChecker())


def _validate_frozen_schema(document: Mapping[str, Any], kind: str) -> None:
    try:
        errors = sorted(_contract_validator(kind).iter_errors(document), key=lambda item: list(item.path))
    except ReleaseFailure:
        raise
    except Exception as exc:
        raise ReleaseFailure(f"{kind} frozen schema validation failed", code="contract-unavailable") from exc
    if errors:
        first = errors[0]
        location = ".".join(str(part) for part in first.path) or "document"
        raise ReleaseFailure(f"{kind} frozen schema rejected {location}", code=f"{kind}-refused")


def _validate_inventory(inventory: Mapping[str, Any]) -> None:
    _validate_frozen_schema(inventory, "inventory")
    if inventory.get("schema_version") != 1 or inventory.get("stage") != "confirmed":
        raise ReleaseFailure("external inventory is not confirmed v1", code="inventory-refused")
    if not isinstance(inventory.get("revision"), int) or inventory["revision"] < 1:
        raise ReleaseFailure("external inventory has no valid revision", code="inventory-refused")
    if not isinstance(inventory.get("site_id"), str) or not inventory["site_id"]:
        raise ReleaseFailure("external inventory has no site", code="inventory-refused")
    controller = inventory.get("controller")
    if not isinstance(controller, Mapping):
        raise ReleaseFailure("external inventory has no controller", code="inventory-refused")
    if not controller.get("endpoint") or not controller.get("account") or controller.get("auth_state") != "configured":
        raise ReleaseFailure("external inventory controller is incomplete", code="inventory-refused")
    try:
        ZoneInfo(str(inventory.get("timezone")))
    except (ZoneInfoNotFoundError, TypeError):
        raise ReleaseFailure("external inventory timezone is unknown", code="inventory-refused")
    hosts = inventory.get("hosts", [])
    lanes = inventory.get("lanes", [])
    if not isinstance(hosts, list) or not isinstance(lanes, list):
        raise ReleaseFailure("external inventory hosts or lanes are invalid", code="inventory-refused")
    host_map: dict[str, Mapping[str, Any]] = {}
    device_map: dict[str, Mapping[str, Any]] = {}
    identity_ids: set[str] = set()
    for mapping in inventory.get("identity_mapping", []):
        external_id = mapping.get("external_id")
        if external_id in identity_ids:
            raise ReleaseFailure("external inventory repeats an identity mapping", code="inventory-refused")
        identity_ids.add(external_id)
        principal = mapping.get("principal", {})
        if principal.get("site_id") != inventory.get("site_id"):
            raise ReleaseFailure("external inventory identity crosses site", code="inventory-refused")
    for host in hosts:
        if not isinstance(host, Mapping) or not isinstance(host.get("host_id"), str):
            raise ReleaseFailure("external inventory has an invalid host", code="inventory-refused")
        host_id = str(host["host_id"])
        if host_id in host_map:
            raise ReleaseFailure("external inventory repeats a host", code="inventory-refused")
        host_map[host_id] = host
        count = host.get("gpu_count")
        devices = host.get("devices", [])
        if count is None:
            raise ReleaseFailure("external inventory has unknown GPU count", code="inventory-refused")
        if not isinstance(count, int) or count < 0 or not isinstance(devices, list) or count != len(devices):
            raise ReleaseFailure("external inventory GPU count is inconsistent", code="inventory-refused")
        if count == 0 and devices:
            raise ReleaseFailure("external inventory no-GPU host has devices", code="inventory-refused")
        if host.get("reachability") == "confirmed" and host.get("observed_at") is None:
            raise ReleaseFailure("external inventory confirmed host lacks observation", code="inventory-refused")
        for device in devices:
            if not isinstance(device, Mapping) or not isinstance(device.get("device_id"), str):
                raise ReleaseFailure("external inventory has an invalid device", code="inventory-refused")
            device_id = str(device["device_id"])
            if device_id in device_map:
                raise ReleaseFailure("external inventory repeats a device", code="inventory-refused")
            device_map[device_id] = device
            if any(device.get(field) is None for field in ("vendor", "model", "vram_bytes", "driver")):
                raise ReleaseFailure("external inventory has unknown device data", code="inventory-refused")
            unknown_reasons = device.get("unknown_reasons", {})
            for field in ("vendor", "model", "vram_bytes", "driver"):
                if device.get(field) is None and field not in unknown_reasons:
                    raise ReleaseFailure("external inventory unknown device data lacks a reason", code="inventory-refused")
                if device.get(field) is not None and field in unknown_reasons:
                    raise ReleaseFailure("external inventory known device data has an unknown reason", code="inventory-refused")
    lane_ids: set[str] = set()
    for lane in lanes:
        if not isinstance(lane, Mapping) or not isinstance(lane.get("lane_id"), str):
            raise ReleaseFailure("external inventory has an invalid lane", code="inventory-refused")
        lane_id = str(lane["lane_id"])
        if lane_id in lane_ids:
            raise ReleaseFailure("external inventory repeats a lane", code="inventory-refused")
        lane_ids.add(lane_id)
        host_id = lane.get("host_id")
        host = host_map.get(host_id)
        if host is None:
            raise ReleaseFailure("external inventory has a dangling lane", code="inventory-refused")
        if lane.get("enabled") and host.get("reachability") != "confirmed":
            raise ReleaseFailure("external inventory enables an unknown host", code="inventory-refused")
        for device_id in lane.get("device_ids", []):
            if device_id not in device_map or device_id not in {item.get("device_id") for item in host.get("devices", [])}:
                raise ReleaseFailure("external inventory has a dangling lane device", code="inventory-refused")
    for lane_id in inventory.get("chat_lane_order", []):
        if lane_id not in lane_ids:
            raise ReleaseFailure("external inventory has a dangling chat lane", code="inventory-refused")


def _validate_policy(policy: Mapping[str, Any]) -> None:
    _validate_frozen_schema(policy, "policy")
    if policy.get("schema_version") != 1 or not policy.get("policy_id"):
        raise ReleaseFailure("external policy is not v1", code="policy-refused")
    if not isinstance(policy.get("revision"), int) or policy["revision"] < 1:
        raise ReleaseFailure("external policy has no valid revision", code="policy-refused")
    if not isinstance(policy.get("admission"), Mapping):
        raise ReleaseFailure("external policy has no admission settings", code="policy-refused")


def _path_from_value(value: Any, base: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise ReleaseFailure("external document path is missing", code="invalid-input")
    path = Path(value)
    return path if path.is_absolute() else base / path


def _document_descriptor(
    manifest: Mapping[str, Any],
    *,
    kind: str,
    base: Path,
    configured: Path | None,
) -> dict[str, Any]:
    raw = manifest.get(kind)
    if raw is None:
        raw = manifest.get(f"{kind}_file")
    if raw is None:
        raw = manifest.get(f"{kind}_path")
    if raw is None:
        raw = manifest.get(f"confirmed_{kind}")
    if raw is None:
        raw = manifest.get(f"external_{kind}")
    metadata: dict[str, Any]
    inline: Any = None
    if isinstance(raw, Mapping):
        metadata = dict(raw)
        inline = metadata.get("document", metadata.get("data"))
        if inline is None and "schema_version" in raw:
            metadata = {key: value for key, value in raw.items() if key in {"sha256", "confirmed", "revision"}}
            inline = {key: value for key, value in raw.items() if key not in {"sha256", "confirmed", "path", "document", "data"}}
    elif isinstance(raw, str):
        metadata = {"path": raw}
    elif raw is None:
        metadata = {}
    else:
        raise ReleaseFailure(f"{kind} descriptor is invalid", code="invalid-input")
    source = configured or (_path_from_value(metadata["path"], base) if metadata.get("path") else None)
    if source is not None:
        _regular_file(source, f"{kind} document")
        raw_bytes = source.read_bytes()
        try:
            document = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseFailure(f"{kind} document is not JSON", code="invalid-input") from exc
        source_label = str(source)
    elif inline is not None:
        document = inline
        raw_bytes = _json_bytes(document)
        source_label = "inline"
    else:
        raise ReleaseFailure(f"{kind} document is not configured", code="invalid-input")
    if not isinstance(document, Mapping):
        raise ReleaseFailure(f"{kind} document is not an object", code="invalid-input")
    actual_hash = sha256_bytes(raw_bytes)
    expected_hash = metadata.get("sha256", manifest.get(f"{kind}_sha256"))
    if expected_hash is None:
        raise ReleaseFailure(f"{kind} document hash is missing", code="invalid-input")
    expected_hash = _hash_or_fail(expected_hash, f"{kind} document")
    if actual_hash != expected_hash:
        raise ReleaseFailure(f"{kind} document hash changed", code="inventory-refused" if kind == "inventory" else "policy-refused")
    expected_revision = metadata.get("revision", manifest.get(f"{kind}_revision"))
    actual_revision = document.get("revision")
    if expected_revision is None or actual_revision != expected_revision:
        raise ReleaseFailure(f"{kind} document revision changed", code="inventory-refused" if kind == "inventory" else "policy-refused")
    if kind == "inventory":
        _validate_inventory(document)
    else:
        _validate_policy(document)
    confirmed = metadata.get("confirmed")
    if type(confirmed) is not bool or not confirmed:
        raise ReleaseFailure(f"{kind} confirmation is not explicit", code="inventory-refused" if kind == "inventory" else "policy-refused")
    return {
        "revision": actual_revision,
        "sha256": actual_hash,
        "confirmed": confirmed,
        "source": source_label,
        "document": dict(document),
        "raw_bytes": raw_bytes,
    }


def _versioned_descriptor(manifest: Mapping[str, Any], name: str) -> dict[str, Any]:
    aliases = {
        "protocol": ("protocol_version", "protocol_hash"),
        "state_compatibility": ("state_compatibility_version", "state_compatibility_hash"),
    }
    value = manifest.get(name)
    if value is None:
        version_key, hash_key = aliases[name]
        if version_key in manifest or hash_key in manifest:
            value = {"version": manifest.get(version_key), "sha256": manifest.get(hash_key)}
    return _versioned_hash(value, name)


def _load_rollout_config(
    manifest: Mapping[str, Any],
    *,
    base: Path,
    configured: Path | None,
) -> dict[str, Any] | None:
    raw = manifest.get("rollout")
    if raw is None:
        raw = manifest.get("smoke")
    if raw is None and "rollout_gate_order" in manifest:
        raw = {"gate_order": manifest["rollout_gate_order"]}
    if isinstance(raw, Mapping):
        descriptor = dict(raw)
    elif raw is None:
        descriptor = {}
    else:
        raise ReleaseFailure("rollout configuration is invalid", code="invalid-input")
    source = configured or (_path_from_value(descriptor["path"], base) if descriptor.get("path") else None)
    if source is not None:
        _regular_file(source, "rollout configuration")
        raw_bytes = source.read_bytes()
        try:
            config = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseFailure("rollout configuration is not JSON", code="invalid-input") from exc
        expected = descriptor.get("sha256")
        if expected is not None and sha256_bytes(raw_bytes) != _hash_or_fail(expected, "rollout configuration"):
            raise ReleaseFailure("rollout configuration hash changed", code="invalid-input")
    else:
        config = descriptor
        raw_bytes = _json_bytes(config)
    if not isinstance(config, Mapping):
        raise ReleaseFailure("rollout configuration is not an object", code="invalid-input")
    order = config.get("smoke_gate_order", config.get("gate_order", config.get("gates")))
    if order is None:
        raise ReleaseFailure("rollout gate order is missing", code="invalid-input")
    if not isinstance(order, list) or not order or any(not isinstance(item, str) or not item for item in order):
        raise ReleaseFailure("rollout gate order is invalid", code="invalid-input")
    if len(set(order)) != len(order):
        raise ReleaseFailure("rollout gate order repeats a gate", code="invalid-input")
    missing = sorted(REQUIRED_SMOKE_GATES.difference(order))
    if missing:
        raise ReleaseFailure("rollout gate order omits required safety probes", code="invalid-input")
    return {
        "version": config.get("version", descriptor.get("version", 1)),
        "sha256": sha256_bytes(raw_bytes),
        "gate_order": list(order),
        "source": str(source) if source is not None else "manifest",
    }


def _find_manifest(release_ref: str) -> tuple[Path, dict[str, Any], bytes]:
    candidate = Path(release_ref)
    if not candidate.exists():
        root = Path(os.environ.get("FLIGHTCTL_RELEASE_INPUT_ROOT", "releases"))
        candidate = root / release_ref
    if not candidate.exists():
        raise ReleaseFailure("release input does not exist", code="invalid-input")
    if candidate.is_file():
        manifest_path = candidate
        root = candidate.parent
    elif candidate.is_dir():
        names = ("release.json", "manifest.json", "release-manifest.json")
        matches = [candidate / name for name in names if (candidate / name).is_file()]
        if len(matches) != 1:
            raise ReleaseFailure("release input must contain one release manifest", code="invalid-input")
        manifest_path = matches[0]
        root = candidate
    else:
        raise ReleaseFailure("release input is not a regular path", code="invalid-input")
    _regular_file(manifest_path, "release manifest")
    raw = manifest_path.read_bytes()
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseFailure("release manifest is not JSON", code="invalid-input") from exc
    if not isinstance(manifest, dict):
        raise ReleaseFailure("release manifest is not an object", code="invalid-input")
    return root.resolve(), manifest, raw


def _normalise_release_manifest(
    release_ref: str,
    *,
    paths: ReleasePaths,
) -> tuple[Path, dict[str, Any], bytes]:
    root, source, raw = _find_manifest(release_ref)
    if source.get("schema_version", source.get("schema")) != SCHEMA_VERSION:
        raise ReleaseFailure("release manifest schema is unsupported", code="invalid-input")
    release_id = source.get("release_id", source.get("id"))
    release_id = _id_or_fail(release_id, "release_id")
    ref_path = Path(release_ref)
    if not ref_path.exists() and release_ref != release_id:
        raise ReleaseFailure("release reference does not match release_id", code="invalid-input")
    release_version = source.get("release_version", source.get("version"))
    if not isinstance(release_version, (str, int)) or isinstance(release_version, bool) or not str(release_version):
        raise ReleaseFailure("release manifest has no release version", code="invalid-input")
    protocol = _versioned_descriptor(source, "protocol")
    state_compatibility = _versioned_descriptor(source, "state_compatibility")
    inventory = _document_descriptor(source, kind="inventory", base=root, configured=paths.inventory_path)
    policy = _document_descriptor(source, kind="policy", base=root, configured=paths.policy_path)
    artifacts_raw = source.get("artifacts")
    if not isinstance(artifacts_raw, list) or not artifacts_raw:
        raise ReleaseFailure("release manifest has no explicit artifacts", code="invalid-input")
    artifacts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in artifacts_raw:
        if not isinstance(item, Mapping):
            raise ReleaseFailure("artifact entry is invalid", code="invalid-input")
        relative = _safe_relative(item.get("path"), "artifact path")
        key = relative.as_posix()
        if key in seen:
            raise ReleaseFailure("release manifest repeats an artifact", code="invalid-input")
        seen.add(key)
        source_path = root / relative
        _regular_file(source_path, "artifact")
        expected_hash = _hash_or_fail(item.get("sha256", item.get("hash")), f"artifact {key}")
        actual_hash = sha256_file(source_path)
        if expected_hash != actual_hash:
            raise ReleaseFailure(f"artifact hash changed: {key}", code="artifact-refused")
        version = item.get("version")
        if not isinstance(version, (str, int)) or isinstance(version, bool) or not str(version):
            raise ReleaseFailure(f"artifact version missing: {key}", code="invalid-input")
        artifacts.append({"path": key, "version": version, "sha256": actual_hash, "mode": stat.S_IMODE(source_path.stat().st_mode)})
    rollout = _load_rollout_config(source, base=root, configured=paths.rollout_config_path)
    normalised: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "release_id": release_id,
        "release_version": release_version,
        "protocol": protocol,
        "state_compatibility": state_compatibility,
        "artifacts": artifacts,
        "inventory": {
            "revision": inventory["revision"],
            "sha256": inventory["sha256"],
            "confirmed": inventory["confirmed"],
        },
        "policy": {
            "revision": policy["revision"],
            "sha256": policy["sha256"],
            "confirmed": policy["confirmed"],
        },
        "rollout": rollout,
        "source_manifest_sha256": sha256_bytes(raw),
    }
    if not normalised["inventory"]["confirmed"]:
        raise ReleaseFailure("inventory confirmation is not present", code="inventory-refused")
    if not normalised["policy"]["confirmed"]:
        raise ReleaseFailure("policy confirmation is not present", code="policy-refused")
    return root, normalised, raw


def _manifest_hash(manifest: Mapping[str, Any]) -> str:
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    return sha256_bytes(canonical_bytes(unsigned))


def _staged_release_id(release_ref: str) -> str:
    candidate = Path(release_ref)
    if candidate.exists():
        _, manifest, _ = _find_manifest(release_ref)
        return _id_or_fail(manifest.get("release_id", manifest.get("id")), "release_id")
    return _id_or_fail(release_ref, "release_id")


class ReleaseManager:
    """Orchestrate one release using a real file-backed or injected backend."""

    def __init__(self, paths: ReleasePaths | None = None, backend: ReleaseBackend | None = None) -> None:
        self.paths = paths or ReleasePaths.from_env()
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)
        self.paths.staging_dir.mkdir(parents=True, exist_ok=True)
        self.paths.snapshot_dir.mkdir(parents=True, exist_ok=True)
        for directory in (self.paths.state_dir, self.paths.staging_dir, self.paths.snapshot_dir):
            try:
                os.chmod(directory, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- 0o700 is owner-only (stricter than the rule's 0o644)
            except OSError:
                pass
        self.backend = backend or FileReleaseBackend(self.paths.state_dir)

    def _fail_closed(self, reason: str) -> None:
        try:
            self.backend.close_admission(reason)
        except Exception:
            pass

    def _inspect_empty(self, release_id: str) -> None:
        observation = self.backend.inspect_empty(release_id)
        if (
            not isinstance(observation, Mapping)
            or observation.get("empty") is not True
            or not isinstance(observation.get("occupants"), list)
            or observation["occupants"]
        ):
            raise ReleaseFailure("occupancy is not confirmed empty", code="occupancy-unknown")

    def _staged_dir(self, release_id: str) -> Path:
        return self.paths.staging_dir / release_id

    def _load_staged(self, release_ref: str) -> tuple[str, Path, dict[str, Any]]:
        release_id = _staged_release_id(release_ref)
        directory = self._staged_dir(release_id)
        manifest_path = directory / "manifest.json"
        if not manifest_path.is_file():
            raise ReleaseFailure("release is not staged", code="not-staged")
        manifest = _read_json(manifest_path)
        if not isinstance(manifest, dict) or manifest.get("release_id") != release_id:
            raise ReleaseFailure("staged manifest identity is invalid", code="invalid-state")
        self._verify_staged(directory, manifest)
        return release_id, directory, manifest

    def _load_for_mutation(self, release_ref: str, operation: str) -> tuple[str, Path, dict[str, Any]]:
        try:
            return self._load_staged(release_ref)
        except Exception:
            self._fail_closed(f"{operation} staged-input refusal")
            raise

    def _verify_staged(self, directory: Path, manifest: Mapping[str, Any]) -> None:
        expected_manifest_hash = manifest.get("manifest_sha256")
        if expected_manifest_hash != _manifest_hash(manifest):
            raise ReleaseFailure("staged manifest hash does not match", code="integrity-failed")
        for artifact in manifest.get("artifacts", []):
            relative = _safe_relative(artifact.get("path"), "staged artifact path")
            path = directory / "artifacts" / relative
            _regular_file(path, "staged artifact")
            if sha256_file(path) != artifact.get("sha256"):
                raise ReleaseFailure("staged artifact hash does not match", code="integrity-failed")
        for kind in ("inventory", "policy"):
            descriptor = manifest.get(kind, {})
            path = directory / "metadata" / f"{kind}.json"
            _regular_file(path, f"staged {kind}")
            if sha256_file(path) != descriptor.get("sha256"):
                raise ReleaseFailure(f"staged {kind} hash does not match", code="integrity-failed")
            if descriptor.get("confirmed") is not True:
                raise ReleaseFailure(f"staged {kind} is not confirmed", code="integrity-failed")
            document = _read_json(path)
            if not isinstance(document, Mapping) or document.get("revision") != descriptor.get("revision"):
                raise ReleaseFailure(f"staged {kind} revision does not match", code="integrity-failed")
            if kind == "inventory":
                _validate_inventory(document)
            else:
                _validate_policy(document)

    def _write_backup(self, release_id: str, manifest: Mapping[str, Any], snapshot: Mapping[str, Any]) -> Path:
        base = self.paths.backup_dir or (self.paths.state_dir / "backup-rehearsal")
        base.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(base, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- 0o700 is owner-only (stricter than the rule's 0o644)
        except OSError:
            pass
        bundle = base / f"before-{release_id}"
        suffix = 0
        while bundle.exists():
            suffix += 1
            bundle = base / f"before-{release_id}-{suffix}"
        bundle.mkdir(mode=0o700)
        staged = self._staged_dir(release_id)
        files: list[dict[str, str]] = []
        state_path = bundle / "state.json"
        _write_json(state_path, snapshot, mode=0o600)
        files.append({"path": "state.json", "sha256": sha256_file(state_path)})
        for kind in ("inventory", "policy"):
            source = staged / "metadata" / f"{kind}.json"
            destination = bundle / f"{kind}.json"
            _write_bytes(destination, source.read_bytes(), mode=0o600)
            files.append({"path": f"{kind}.json", "sha256": sha256_file(destination)})
        input_manifest = copy.deepcopy(dict(manifest))
        input_manifest.pop("manifest_sha256", None)
        input_manifest["inventory"]["path"] = "inventory.json"
        input_manifest["policy"]["path"] = "policy.json"
        for artifact in input_manifest["artifacts"]:
            artifact["path"] = f"artifacts/{artifact['path']}"
            source = staged / "artifacts" / Path(artifact["path"]).relative_to("artifacts")
            destination = bundle / artifact["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            _write_bytes(destination, source.read_bytes(), mode=0o600)
            files.append({"path": artifact["path"], "sha256": sha256_file(destination)})
        release_input = bundle / "release.json"
        _write_json(release_input, input_manifest, mode=0o600)
        files.append({"path": "release.json", "sha256": sha256_file(release_input)})
        backup_manifest = {
            "schema_version": SCHEMA_VERSION,
            "release_id": release_id,
            "release_manifest_sha256": manifest.get("manifest_sha256"),
            "files": files,
        }
        _write_json(bundle / "backup-manifest.json", backup_manifest, mode=0o600)
        _make_owner_only_tree(bundle)
        return bundle

    def stage(self, release_ref: str) -> Mapping[str, Any]:
        root, manifest, _ = _normalise_release_manifest(release_ref, paths=self.paths)
        release_id = str(manifest["release_id"])
        target = self._staged_dir(release_id)
        if target.exists():
            existing = _read_json(target / "manifest.json")
            if isinstance(existing, Mapping) and existing.get("manifest_sha256") == _manifest_hash(manifest):
                return {"status": "already-staged", "release_id": release_id, "manifest_sha256": existing.get("manifest_sha256")}
            raise ReleaseFailure("staged release is immutable and differs", code="immutable")
        temporary = self.paths.staging_dir / f".{release_id}.tmp-{os.getpid()}-{time.time_ns()}"
        try:
            temporary.mkdir(mode=0o700)
            artifact_root = temporary / "artifacts"
            metadata_root = temporary / "metadata"
            for artifact in manifest["artifacts"]:
                relative = Path(artifact["path"])
                source = root / relative
                destination = artifact_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                _copy_mode(destination, 0o400)
            for kind in ("inventory", "policy"):
                source = _document_source_from_manifest(root, release_ref, kind, self.paths)
                destination = metadata_root / f"{kind}.json"
                _write_bytes(destination, source, mode=0o400)
            if manifest.get("rollout") and manifest["rollout"].get("source") != "manifest":
                rollout_source = _rollout_source_bytes(root, release_ref, self.paths)
                _write_bytes(metadata_root / "rollout.json", rollout_source, mode=0o400)
            manifest = copy.deepcopy(manifest)
            manifest["manifest_sha256"] = _manifest_hash(manifest)
            _write_json(temporary / "manifest.json", manifest, mode=0o400)
            _write_bytes(temporary / "IMMUTABLE", b"staged bundle; mutate by creating a new release\n", mode=0o400)
            _make_immutable(temporary)
            os.replace(temporary, target)
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary)
            raise
        return {"status": "staged", "release_id": release_id, "manifest_sha256": manifest["manifest_sha256"]}

    def _release_snapshot_path(self, release_id: str) -> Path:
        return self.paths.snapshot_dir / f"{release_id}.json"

    def _current_external_matches(self, manifest: Mapping[str, Any], snapshot: Mapping[str, Any]) -> None:
        state = self.backend.get_state()
        for kind in ("inventory", "policy"):
            expected = snapshot.get("observed", {}).get(kind, {})
            staged = manifest.get(kind, {})
            if expected and expected.get("sha256") != staged.get("sha256"):
                raise ReleaseFailure(f"rollback {kind} does not match staged release", code="rollback-refused")
            configured = self.paths.inventory_path if kind == "inventory" else self.paths.policy_path
            if configured is not None:
                _regular_file(configured, f"current {kind}")
                current_hash = sha256_file(configured)
                if current_hash != staged.get("sha256") or (expected and current_hash != expected.get("sha256")):
                    raise ReleaseFailure(f"current {kind} changed", code="rollback-refused")
            state_hash = state.get(f"current_{kind}_hash")
            if state_hash and state_hash != expected.get("sha256", state_hash):
                raise ReleaseFailure(f"recorded {kind} changed", code="rollback-refused")
        expected_hardware = snapshot.get("observed", {}).get("hardware_hash")
        current_hardware = state.get("hardware_hash", state.get("inventory_hardware_hash"))
        if expected_hardware is not None and current_hardware != expected_hardware:
            raise ReleaseFailure("current hardware observation changed", code="rollback-refused")

    def drain(self, release_ref: str) -> Mapping[str, Any]:
        release_id, _, _ = self._load_for_mutation(release_ref, "drain")
        self.backend.close_admission("drain")
        try:
            self.backend.pause_producers()
            self.backend.drain_workloads(release_id)
            self.backend.drain_chat(release_id)
            self._inspect_empty(release_id)
        except Exception as exc:
            self._fail_closed("drain failed")
            if isinstance(exc, ReleaseFailure):
                raise
            raise ReleaseFailure("drain failed", code="drain-failed") from exc
        return {"status": "drained", "release_id": release_id, "admission": "closed"}

    def activate(self, release_ref: str) -> Mapping[str, Any]:
        release_id, _, manifest = self._load_for_mutation(release_ref, "activate")
        self.backend.close_admission("activate")
        try:
            self.backend.pause_producers()
            self.backend.drain_workloads(release_id)
            self.backend.drain_chat(release_id)
            self._inspect_empty(release_id)
            previous_state = dict(self.backend.snapshot_state(release_id))
            snapshot = {
                "schema_version": SCHEMA_VERSION,
                "created_at": utc_now(),
                "source_release": release_id,
                "restore_release": previous_state.get("current_release"),
                "restore_manifest": previous_state.get("current_manifest"),
                "restore_manifest_sha256": previous_state.get("current_manifest_hash"),
                "restore_state_compatibility": previous_state.get("current_state_compatibility"),
                "observed": {
                    "inventory": copy.deepcopy(manifest.get("inventory")),
                    "policy": copy.deepcopy(manifest.get("policy")),
                    "hardware_hash": previous_state.get("hardware_hash", previous_state.get("inventory_hardware_hash")),
                },
                "state": previous_state,
            }
            _write_json(self._release_snapshot_path(release_id), snapshot, mode=0o600)
            backup = self._write_backup(release_id, manifest, snapshot)
            self.backend.disable_legacy_writers()
            self.backend.install_executors(manifest)
            self.backend.install_authority(manifest)
            self.backend.install_clients(manifest)
            self.backend.install_adapters(manifest)
            self.backend.install_watcher(manifest)
            self.backend.verify_agreement(manifest)
            self.backend.commit_release(manifest)
            self.backend.reopen_admission()
        except Exception as exc:
            self._fail_closed("activation failed")
            if isinstance(exc, ReleaseFailure):
                raise
            raise ReleaseFailure("activation failed", code="activation-failed") from exc
        return {
            "status": "active",
            "release_id": release_id,
            "manifest_sha256": manifest.get("manifest_sha256"),
            "backup": str(backup),
            "admission": "open",
        }

    def smoke(self, release_ref: str) -> Mapping[str, Any]:
        release_id, _, manifest = self._load_for_mutation(release_ref, "smoke")
        state = self.backend.get_state()
        current = state.get("current_release") or state.get("installed_release")
        if current != release_id:
            self._fail_closed("smoke release mismatch")
            raise ReleaseFailure("smoke release does not match installed release", code="smoke-refused")
        if state.get("admission") != "open":
            self._fail_closed("smoke requires an active release")
            raise ReleaseFailure("smoke requires an active release", code="smoke-refused")
        rollout = manifest.get("rollout")
        if not isinstance(rollout, Mapping) or not rollout.get("gate_order"):
            self._fail_closed("smoke gate order missing")
            raise ReleaseFailure("smoke gate order is not supplied by external configuration", code="smoke-refused")
        gate_order = rollout["gate_order"]
        if not REQUIRED_SMOKE_GATES.issubset(gate_order):
            self._fail_closed("smoke safety probes missing")
            raise ReleaseFailure("smoke gate order omits required safety probes", code="smoke-refused")
        self.backend.close_admission("smoke")
        passed: list[str] = []
        try:
            self._inspect_empty(release_id)
            for gate in gate_order:
                if gate == "hash":
                    self._verify_staged(self._staged_dir(release_id), manifest)
                result = self.backend.smoke_probe(str(gate), manifest)
                status = result.get("status") if isinstance(result, Mapping) else result
                if not isinstance(status, str) or status.lower() in SMOKE_FAILURES or status.lower() not in {"ok", "success", "passed"}:
                    raise ReleaseFailure(f"smoke gate failed: {gate}", code="smoke-failed")
                passed.append(str(gate))
            self.backend.reopen_admission()
        except Exception as exc:
            self._fail_closed("smoke failed")
            if isinstance(exc, ReleaseFailure):
                raise
            raise ReleaseFailure("smoke failed", code="smoke-failed") from exc
        return {"status": "smoke-passed", "release_id": release_id, "gates": passed, "admission": "open"}

    def rollback(self, release_ref: str) -> Mapping[str, Any]:
        release_id, _, manifest = self._load_for_mutation(release_ref, "rollback")
        state = dict(self.backend.get_state())
        current = state.get("current_release") or state.get("installed_release")
        if not current or current == release_id:
            self._fail_closed("rollback target is not an older release")
            raise ReleaseFailure("rollback target is not an older running release", code="rollback-refused")
        candidates: list[dict[str, Any]] = []
        for path in sorted(self.paths.snapshot_dir.glob("*.json")):
            try:
                candidate = _read_json(path)
            except ReleaseFailure:
                continue
            if (
                isinstance(candidate, Mapping)
                and candidate.get("source_release") == current
                and candidate.get("restore_release") == release_id
            ):
                candidates.append(dict(candidate))
        if len(candidates) != 1:
            self._fail_closed("matched rollback snapshot missing")
            raise ReleaseFailure("no unique matched rollback snapshot exists", code="rollback-refused")
        snapshot = candidates[0]
        if snapshot.get("restore_manifest_sha256") != manifest.get("manifest_sha256"):
            self._fail_closed("rollback artifact match failed")
            raise ReleaseFailure("rollback artifacts are not matched", code="rollback-refused")
        running_state_compatibility = state.get("current_state_compatibility")
        if (
            snapshot.get("restore_state_compatibility") != manifest.get("state_compatibility")
            or running_state_compatibility != manifest.get("state_compatibility")
        ):
            self._fail_closed("rollback state compatibility failed")
            raise ReleaseFailure("rollback state compatibility is incompatible", code="rollback-refused")
        try:
            self._current_external_matches(manifest, snapshot)
        except Exception:
            self._fail_closed("rollback precondition failed")
            raise
        self.backend.close_admission("rollback")
        try:
            self.backend.pause_producers()
            self.backend.drain_workloads(current)
            self.backend.drain_chat(current)
            self._inspect_empty(current)
            newer_snapshot = dict(self.backend.snapshot_state(current))
            self.backend.disable_legacy_writers()
            self.backend.restore_artifacts(manifest)
            self.backend.restore_state(snapshot.get("state", {}), newer_snapshot)
            self.backend.restore_inventory(manifest)
            self.backend.reconcile_hardware(manifest)
            self.backend.verify_agreement(manifest)
            self.backend.commit_release(manifest)
            self.backend.reopen_admission()
        except Exception as exc:
            self._fail_closed("rollback failed")
            if isinstance(exc, ReleaseFailure):
                raise
            raise ReleaseFailure("rollback failed", code="rollback-failed") from exc
        return {"status": "rolled-back", "from": current, "to": release_id, "admission": "open"}


def _document_source_from_manifest(root: Path, release_ref: str, kind: str, paths: ReleasePaths) -> bytes:
    _, source, _ = _find_manifest(release_ref)
    descriptor = source.get(kind)
    if descriptor is None:
        descriptor = source.get(f"{kind}_file", source.get(f"{kind}_path"))
    if descriptor is None:
        descriptor = source.get(f"confirmed_{kind}", source.get(f"external_{kind}"))
    configured = paths.inventory_path if kind == "inventory" else paths.policy_path
    if configured is not None:
        _regular_file(configured, f"{kind} document")
        return configured.read_bytes()
    if isinstance(descriptor, Mapping) and descriptor.get("path"):
        source_path = _path_from_value(descriptor["path"], root)
        _regular_file(source_path, f"{kind} document")
        return source_path.read_bytes()
    if isinstance(descriptor, str):
        source_path = _path_from_value(descriptor, root)
        _regular_file(source_path, f"{kind} document")
        return source_path.read_bytes()
    if isinstance(descriptor, Mapping) and "document" in descriptor:
        return _json_bytes(descriptor["document"])
    if isinstance(descriptor, Mapping) and "data" in descriptor:
        return _json_bytes(descriptor["data"])
    if isinstance(descriptor, Mapping) and "schema_version" in descriptor:
        document = {
            key: value
            for key, value in descriptor.items()
            if key not in {"sha256", "confirmed", "path", "document", "data"}
        }
        return _json_bytes(document)
    raise ReleaseFailure(f"{kind} source cannot be staged", code="invalid-input")


def _rollout_source_bytes(root: Path, release_ref: str, paths: ReleasePaths) -> bytes:
    _, source, _ = _find_manifest(release_ref)
    descriptor = source.get("rollout", source.get("smoke"))
    configured = paths.rollout_config_path
    if configured is not None:
        _regular_file(configured, "rollout configuration")
        return configured.read_bytes()
    if isinstance(descriptor, Mapping) and descriptor.get("path"):
        source_path = _path_from_value(descriptor["path"], root)
        _regular_file(source_path, "rollout configuration")
        return source_path.read_bytes()
    return _json_bytes(descriptor or {})


def restore_backup(bundle: Path, target_root: Path) -> Mapping[str, Any]:
    """Verify and restore a protected backup bundle into a fresh root."""

    bundle = bundle.resolve()
    manifest_path = bundle / "backup-manifest.json"
    if not manifest_path.is_file():
        raise ReleaseFailure("backup manifest is missing", code="backup-refused")
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, Mapping) or manifest.get("schema_version") != SCHEMA_VERSION:
        raise ReleaseFailure("backup manifest is unsupported", code="backup-refused")
    if target_root.exists() and any(target_root.iterdir()):
        raise ReleaseFailure("restore target is not fresh", code="backup-refused")
    target_root.mkdir(parents=True, exist_ok=True)
    os.chmod(target_root, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- 0o700 is owner-only (stricter than the rule's 0o644)
    restored: list[str] = []
    for item in manifest.get("files", []):
        if not isinstance(item, Mapping):
            raise ReleaseFailure("backup file entry is invalid", code="backup-refused")
        relative = _safe_relative(item.get("path"), "backup file path")
        source = bundle / relative
        _regular_file(source, "backup file")
        if sha256_file(source) != item.get("sha256"):
            raise ReleaseFailure("backup file hash does not match", code="backup-refused")
        destination = target_root / relative
        _write_bytes(destination, source.read_bytes(), mode=0o600)
        restored.append(relative.as_posix())
    _make_owner_only_tree(target_root)
    return {"status": "restored", "release_id": manifest.get("release_id"), "files": restored}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="fail-closed Flightctl release rehearsal")
    parser.add_argument("--state-dir", type=Path, help="state root; prefer FLIGHTCTL_RELEASE_STATE_DIR")
    parser.add_argument("--backup-dir", type=Path, help="owner-only backup target")
    parser.add_argument("--inventory", type=Path, help="external confirmed inventory")
    parser.add_argument("--policy", type=Path, help="external policy")
    parser.add_argument("--rollout-config", type=Path, help="external smoke gate configuration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("stage", "drain", "activate", "smoke", "rollback"):
        subparsers.add_parser(command).add_argument("release")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    env_paths = ReleasePaths.from_env()
    paths = ReleasePaths(
        state_dir=(args.state_dir or env_paths.state_dir).resolve(),
        backup_dir=(args.backup_dir or env_paths.backup_dir),
        inventory_path=(args.inventory or env_paths.inventory_path),
        policy_path=(args.policy or env_paths.policy_path),
        rollout_config_path=(args.rollout_config or env_paths.rollout_config_path),
    )
    manager = ReleaseManager(paths)
    try:
        result = getattr(manager, args.command)(args.release)
    except ReleaseFailure as exc:
        print(f"flightctl-release: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
