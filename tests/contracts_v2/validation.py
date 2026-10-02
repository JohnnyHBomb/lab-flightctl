"""Schema loading and semantic checks for contract set v2.

Contract-level helpers only: these validate documents and express rules JSON Schema cannot
(cross-field, cross-document). They are not runtime code; slices implement the behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path
import contextlib
from typing import Any, Mapping

import jsonschema
from jsonschema import FormatChecker
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[2]
V1_DIR = ROOT / "contracts"
V2_DIR = ROOT / "contracts" / "v2"
V2_SCHEMA_FILES = tuple(sorted(V2_DIR.glob("*.schema.json")))
BASE = "https://flightctl.local/contracts/"


class ContractError(ValueError):
    """A v2 schema or semantic contract violation."""


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _registry() -> Registry:
    resources = []
    for path in sorted(V1_DIR.glob("*.schema.json")):
        schema = load(path)
        resources.append((schema["$id"], Resource.from_contents(schema)))
    for path in V2_SCHEMA_FILES:
        schema = load(path)
        if schema["$id"] != f"{BASE}v2/{path.name}":
            raise ContractError(f"{path.name}: $id must be {BASE}v2/{path.name}")
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


_REGISTRY: Registry | None = None


def registry() -> Registry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _registry()
    return _REGISTRY


def schema_path(name: str) -> Path:
    path = V2_DIR / (name if name.endswith(".schema.json") else f"{name}.schema.json")
    if not path.exists():
        raise FileNotFoundError(path)
    return path


def validator(name: str, definition: str | None = None) -> Any:
    schema = load(schema_path(name))
    if definition is not None:
        schema = {"$schema": schema["$schema"], "$ref": f"{schema['$id']}#/$defs/{definition}"}
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema, registry=registry(), format_checker=FormatChecker())


def errors(instance: Any, name: str, definition: str | None = None) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in validator(name, definition).iter_errors(instance)]


def assert_valid(instance: Any, name: str, definition: str | None = None) -> None:
    found = errors(instance, name, definition)
    if found:
        raise ContractError(f"{name}{'#' + definition if definition else ''}: {found[:3]}")


def assert_invalid(instance: Any, name: str, definition: str | None = None) -> None:
    if not errors(instance, name, definition):
        raise ContractError(f"{name}: expected invalid instance was accepted: {json.dumps(instance)[:200]}")


def examples(name: str) -> dict[str, list[Any]]:
    return load(schema_path(name)).get("x-examples", {"valid": [], "invalid": []})


# ---------------------------------------------------------------- semantic rules

MUTATING_PORTS = frozenset({"workload_runner", "waker", "inhibitor", "notifier", "model_cache", "session_gateway", "release_backend"})
LANE_SCOPED_MUTATING = ("workload_runner", "inhibitor", "waker", "model_cache", "session_gateway", "notifier", "release_backend")
LANE_CRITICAL_PORTS = ("command_runner", "executor_transport", "workload_runner", "occupancy_probe", "inhibitor", "waker")
SHADOW_MUST_BE_DRYRUN = ("workload_runner", "inhibitor", "waker")
SHADOW_MUST_BE_REAL = ("command_runner", "executor_transport", "occupancy_probe")
FEATURE_PORTS = {
    "holder_leases": ("clock", "command_runner", "executor_transport", "occupancy_probe", "inventory_probe", "peer_identity", "inhibitor"),
    "wake": ("waker",),
    "jobs": ("workload_runner", "model_cache"),
    "endpoints": ("workload_runner", "health_probe", "model_cache"),
    "sessions": ("session_gateway",),
    "model_cache": ("model_cache",),
    "notifications": ("notifier",),
    "fido2_approvals": ("signer",),
    "release_backend": ("release_backend",),
    "shadow_observer": ("legacy_observer",),
}


def required_ports(features: Mapping[str, bool]) -> set[str]:
    return {port for feature, on in features.items() if on for port in FEATURE_PORTS[feature]}


def adapters_semantics(config: Mapping[str, Any]) -> list[str]:
    """Fail-closed rules for the site adapters file (ADAPTERS.md section 3, round 2: B3)."""
    problems: list[str] = []
    profile = config["profile"]
    ports: Mapping[str, str] = config["ports"]
    allow_fake = set(config.get("allow_fake", []))
    required = required_ports(config["features"])
    for port, impl in sorted(ports.items()):
        if profile == "live" and impl == "fake" and port not in allow_fake:
            problems.append(f"live profile refuses fake port {port} (not in allow_fake)")
        if profile == "live" and port in required and impl != "real":
            problems.append(f"live profile: port {port} is required by an enabled feature and is {impl}")
        if profile == "sim" and impl in {"real", "dryrun", "record"}:
            problems.append(f"sim profile refuses {impl} port {port}")
        if profile == "shadow" and port in MUTATING_PORTS and impl in {"real", "record"}:
            problems.append(f"shadow profile: mutating port {port} is {impl} (shadow writes nothing)")
        if impl == "dryrun" and port not in MUTATING_PORTS:
            problems.append(f"port {port} is read-only and has no dryrun twin; use real or fake")
    for item in allow_fake:
        if item in LANE_CRITICAL_PORTS:
            problems.append(f"allow_fake may not list lane-critical port {item}")
        elif item in required:
            problems.append(f"allow_fake may not list {item}: an enabled feature requires it")
    for lane, entry in sorted(config.get("lanes", {}).items()):
        mode = entry["mode"]
        overrides: Mapping[str, str] = entry.get("ports", {})
        effective = {port: overrides.get(port, ports.get(port, "fake")) for port in set(ports) | set(LANE_CRITICAL_PORTS) | set(LANE_SCOPED_MUTATING)}
        wake_needed = entry.get("wake_needed", True)
        shadow_real = set(entry.get("shadow_real", []))
        if shadow_real and mode != "shadow":
            problems.append(f"lane {lane}: shadow_real is only valid on a shadow lane")
        if mode == "live":
            if profile != "live":
                problems.append(f"lane {lane} is live but the site profile is {profile}")
            # round 3 (Sol 6 B3): EFFECTIVE ports per live lane, lane overrides included
            for port in sorted(set(LANE_CRITICAL_PORTS) | required):
                if port == "waker" and not wake_needed:
                    continue
                if effective[port] != "real":
                    problems.append(f"lane {lane} is live but {port} is {effective[port]}")
        if mode == "shadow":
            if profile == "sim":
                problems.append(f"lane {lane} is shadow but the site profile is sim")
            if shadow_real and not entry.get("legacy_lane"):
                problems.append(f"lane {lane}: shadow_real needs legacy_lane (the legacy fence must stand)")
            for port in SHADOW_MUST_BE_DRYRUN:
                if port == "waker" and not wake_needed:
                    continue
                want = "real" if port in shadow_real else "dryrun"
                if effective[port] != want:
                    problems.append(f"lane {lane} is shadow but mutating port {port} is {effective[port]} (must be {want})")
            for port in LANE_SCOPED_MUTATING:
                if port in shadow_real:
                    continue
                if effective[port] in {"real", "record"}:
                    problems.append(f"lane {lane} is shadow but mutating port {port} is {effective[port]} (shadow writes nothing)")
            for port in SHADOW_MUST_BE_REAL:
                if effective[port] != "real":
                    problems.append(f"lane {lane} is shadow but {port} is {effective[port]} (shadow needs real reads)")
        if mode == "off" and overrides:
            problems.append(f"lane {lane} is off but carries port overrides")
    return problems


def lease_ceiling(policy: Mapping[str, Any], lane_max_lease_s: int | None, quota_max_lease_s: int | None, cls: str) -> int:
    """N3 oracle (policy.lease description): an explicit quota max_lease_s replaces the class default."""
    lease = policy["lease"]
    base = quota_max_lease_s if quota_max_lease_s is not None else lease["class_ceiling_s"][cls]
    caps = [base, lease["absolute_max_s"]]
    if lane_max_lease_s is not None:
        caps.append(lane_max_lease_s)
    return min(caps)


def quota_eval_semantics(result: Mapping[str, Any]) -> list[str]:
    problems = []
    if result["decision"] == "deny" and not result.get("limit"):
        problems.append("a quota denial must name the limit")
    if result["decision"] == "allow" and result.get("limit"):
        problems.append("an allow names no limit")
    return problems


def inventory_semantics(inventory: Mapping[str, Any]) -> list[str]:
    """Cross-reference rules for inventory v2 (v1 rules carried forward plus G07 card binding)."""
    problems: list[str] = []
    hosts = {h["host_id"]: h for h in inventory["hosts"]}
    if len(hosts) != len(inventory["hosts"]):
        problems.append("duplicate host_id")
    devices: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for host in inventory["hosts"]:
        for device in host["devices"]:
            if device["device_id"] in devices:
                problems.append(f"duplicate device_id {device['device_id']}")
            devices[device["device_id"]] = (host["host_id"], device)
            for key in ("uuid", "pci_bus_id", "numa_node"):
                if device[key] is None and key not in device["unknown_reasons"]:
                    problems.append(f"{device['device_id']}: {key} is null without an unknown reason")
    lane_ids = [lane["lane_id"] for lane in inventory["lanes"]]
    if len(lane_ids) != len(set(lane_ids)):
        problems.append("duplicate lane_id")
    claimed: dict[str, str] = {}
    for lane in inventory["lanes"]:
        if lane["host_id"] not in hosts:
            problems.append(f"lane {lane['lane_id']}: dangling host {lane['host_id']}")
            continue
        for device_id in lane["device_ids"]:
            owner = devices.get(device_id)
            if owner is None or owner[0] != lane["host_id"]:
                problems.append(f"lane {lane['lane_id']}: device {device_id} is not on host {lane['host_id']}")
                continue
            if device_id in claimed:
                problems.append(f"device {device_id} is in lanes {claimed[device_id]} and {lane['lane_id']}")
            claimed[device_id] = lane["lane_id"]
            if lane["enabled"] and owner[1]["uuid"] is None:
                problems.append(f"enabled lane {lane['lane_id']}: device {device_id} has no uuid (the occupancy probe cannot bind it)")
        if lane["enabled"] and inventory["stage"] == "confirmed":
            reach = hosts[lane["host_id"]]["reachability"]
            if reach not in ("confirmed", "asleep"):
                problems.append(f"enabled lane {lane['lane_id']} on host with reachability {reach}")
    for lane_id in inventory["endpoint_lane_order"]:
        if lane_id not in lane_ids:
            problems.append(f"endpoint_lane_order names unknown lane {lane_id}")
    if inventory["stage"] == "confirmed" and inventory["controller"]["auth_state"] != "configured":
        problems.append("a confirmed inventory needs a configured controller")
    return problems


def _num(text: str) -> int | float | None:
    text = text.strip()
    if text.startswith("[") or text in {"", "N/A"}:
        return None
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return None


def occupancy_from_capture(gpus_csv: str, procs_csv: str, *, returncode: int, lane_id: str, host_id: str, observed_at: str,
                           lane_uuids: list[str], process_noise_mib: int = 512, lane_noise_mib: int = 1024,
                           attributed_pids: frozenset[int] = frozenset()) -> dict[str, Any]:
    """Contract ORACLE for the occupancy observation (gpu-probe.schema.json emptiness_rule).

    Parses the two real nvidia-smi query outputs (x-real-commands) exactly as the real twin must. It exists so
    that golden captures from real cards fix the rule; slice A3's production parser must agree with it."""
    thresholds = {"process_noise_mib": process_noise_mib, "lane_noise_mib": lane_noise_mib}
    unknown = {"kind": "gpu-occupancy", "host_id": host_id, "lane_id": lane_id, "observed_at": observed_at, "status": "unknown", "expected_uuids": list(lane_uuids),
               "gpus": [], "processes": [], "tenants": [], "noise": [], "lane_memory_used_mib": None, "thresholds": thresholds,
               "unexplained_mib": None, "empty": False}
    if returncode != 0:
        return unknown | {"reason": f"nvidia-smi exited {returncode}"}
    gpus = []
    for line in gpus_csv.strip().splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) != 10:
            return unknown | {"reason": "unparsable gpu row"}
        uuid = cells[0]
        if uuid not in lane_uuids:
            continue
        used, total = _num(cells[1]), _num(cells[2])
        if not isinstance(used, int) or not isinstance(total, int):
            return unknown | {"reason": f"memory.used unknown on {uuid}"}
        slow = None if cells[7].startswith("[") else (cells[7] == "Active" or cells[8] == "Active")
        gpus.append({"uuid": uuid, "memory_used_mib": used, "memory_total_mib": total, "utilization_pct": _num(cells[3]),
                     "temperature_c": _num(cells[4]), "power_draw_w": _num(cells[5]), "power_limit_w": _num(cells[6]),
                     "thermal_slowdown": slow, "ecc_uncorrected": _num(cells[9])})
    if sorted(g["uuid"] for g in gpus) != sorted(lane_uuids):
        return unknown | {"reason": "a lane card is missing from nvidia-smi output"}
    processes, tenants, noise = [], [], []
    for line in procs_csv.strip().splitlines():
        head = line.split(", ", 2)
        if len(head) < 3:
            return unknown | {"reason": "unparsable process row"}
        uuid, pid_text = head[0].strip(), head[1].strip()
        name, _, mem_text = head[2].rpartition(", ")
        if not pid_text.isdigit() or not name:
            return unknown | {"reason": "unparsable process row"}
        if uuid not in lane_uuids:
            continue
        mem = _num(mem_text)
        mem = mem if isinstance(mem, int) else None
        pid = int(pid_text)
        if pid in attributed_pids:
            kind = "lease"
        elif mem is not None and mem < process_noise_mib:
            kind = "noise"
        else:
            kind = "external"
        row = {"gpu_uuid": uuid, "pid": pid, "process_name": name[:4096], "used_memory_mib": mem, "attribution": kind}
        processes.append(row)
        (tenants if kind == "external" else noise if kind == "noise" else []).append(row)
    lane_used = sum(g["memory_used_mib"] for g in gpus)
    for g in gpus:  # the two queries are separate samples: inconsistent numbers are unknown, never empty
        if sum(p["used_memory_mib"] or 0 for p in processes if p["gpu_uuid"] == g["uuid"]) > g["memory_used_mib"]:
            return unknown | {"reason": f"inconsistent sample on {g['uuid']}: process memory exceeds memory.used"}
    explained = sum((p["used_memory_mib"] or 0) for p in processes if p["attribution"] in {"lease", "noise"})
    unexplained = lane_used - explained
    return {"kind": "gpu-occupancy", "host_id": host_id, "lane_id": lane_id, "observed_at": observed_at, "status": "ok", "reason": None, "expected_uuids": list(lane_uuids),
            "gpus": gpus, "processes": processes, "tenants": tenants, "noise": noise, "lane_memory_used_mib": lane_used,
            "thresholds": thresholds, "unexplained_mib": unexplained, "empty": not tenants and not any(p["attribution"] == "lease" for p in processes) and unexplained < lane_noise_mib}


def occupancy_semantics(obs: Mapping[str, Any]) -> list[str]:
    """B2: 'empty' must agree with tenants AND aggregate memory; unknown memory is never noise."""
    problems = []
    if obs["status"] != "ok":
        return [] if obs["empty"] is False else ["unknown status cannot be empty"]
    # round 4 (Sol 6 r3 B2): the observed cards must be exactly the lane's expected cards
    expected_ids = list(obs.get("expected_uuids") or [])
    observed_ids = [g["uuid"] for g in obs["gpus"]]
    if not expected_ids:
        problems.append("expected_uuids is missing: emptiness cannot be judged without the lane's card identities")
    if sorted(observed_ids) != sorted(expected_ids):
        problems.append(f"observed cards {sorted(observed_ids)} are not exactly the lane's cards {sorted(expected_ids)}")
    for p in obs["processes"]:
        if p["gpu_uuid"] not in expected_ids:
            problems.append(f"pid {p['pid']} is on card {p['gpu_uuid']}, which is not a lane card")
    # round 7 self-probe: known process memory on a card cannot exceed that card's memory.used (inconsistent sample)
    for g in obs["gpus"]:
        listed = sum(p["used_memory_mib"] or 0 for p in obs["processes"] if p["gpu_uuid"] == g["uuid"])
        if listed > g["memory_used_mib"]:
            problems.append(f"card {g['uuid']}: processes report {listed} MiB but memory.used is {g['memory_used_mib']} MiB")
    lane_used = sum(g["memory_used_mib"] for g in obs["gpus"])
    if obs["lane_memory_used_mib"] != lane_used:
        problems.append("lane_memory_used_mib is not the sum of the lane's cards")
    explained = sum((p["used_memory_mib"] or 0) for p in obs["processes"] if p["attribution"] in {"lease", "noise"})
    if obs["unexplained_mib"] != lane_used - explained:
        problems.append("unexplained_mib does not equal lane memory minus attributed and noise memory")
    noise_floor = obs["thresholds"]["process_noise_mib"]
    for p in obs["processes"]:
        mem, kind = p["used_memory_mib"], p["attribution"]
        if kind == "unattributed":
            problems.append(f"pid {p['pid']} is unattributed: every process must be lease, noise or external")
        if kind == "noise" and (mem is None or mem >= noise_floor):
            problems.append(f"pid {p['pid']} is classed as noise but its memory is {mem} (unknown or >= {noise_floor} MiB)")
        if kind == "external" and mem is not None and mem < noise_floor:
            problems.append(f"pid {p['pid']} is external but below the noise floor: classify it as noise")
    # round 3 (Sol 6 B2 counterexample): tenants and noise are exactly the external / noise processes
    if [p for p in obs["processes"] if p["attribution"] == "external"] != list(obs["tenants"]):
        problems.append("tenants must equal the external processes (partition broken)")
    if [p for p in obs["processes"] if p["attribution"] == "noise"] != list(obs["noise"]):
        problems.append("noise must equal the noise processes (partition broken)")
    if obs["empty"] and any(p["used_memory_mib"] is None for p in obs["processes"]):
        problems.append("a process with unknown memory can never leave the lane empty")
    expected = not obs["tenants"] and not any(p["attribution"] == "lease" for p in obs["processes"]) and obs["unexplained_mib"] < obs["thresholds"]["lane_noise_mib"]
    if obs["empty"] != expected:
        problems.append(f"empty={obs['empty']} contradicts tenants/unexplained memory (expected {expected})")
    return problems


def host_ceiling_bound(*, send_t: float, rtt_s: float, in_s: float, approved_max_end_t: float) -> dict[str, Any]:
    """N1 oracle (round 3). Times are on the AUTHORITY's monotonic clock. The host anchored max-end no later than
    send_t + rtt_s (it received the message before replying), so its ceiling is at most send_t + rtt_s + in_s.
    If that can exceed the approval, the authority must send a shorten-only 'ceiling' message."""
    bound = send_t + rtt_s + in_s
    return {"host_max_end_upper_bound": bound, "late_by_s": max(0.0, bound - approved_max_end_t), "must_shorten": bound > approved_max_end_t}


def ceiling_confirmed(*, reply_received_t: float, max_end_remaining_s: float | None, approved_max_end_t: float) -> bool:
    """N1 oracle (round 4). The host built its reply before the authority received it, so the host ceiling is at most
    reply_received_t + max_end_remaining_s (authority monotonic clock; no clock comparison). A grant is allowed only
    when that bound is at or before the approval. No reply (lost message) = not confirmed."""
    if max_end_remaining_s is None:
        return False
    return reply_received_t + max_end_remaining_s <= approved_max_end_t


FENCED_STATES = frozenset({"reserved", "staging", "starting", "running"})


def fence_evidence_ok(reply: Mapping[str, Any], expect: Mapping[str, Any]) -> bool:
    """Round 8 (Sol 6 r7): the reported state alone is not evidence. A reply counts only if it carries the executor's
    PERSISTED fence for this lease: exactly one fence on the expected lane (expect['lane_id'] on expect['host_id']),
    whose identity equals the echoed identity (lease, generation, token hash, run, unit), whose state is a fenced
    state equal to observed_state, and which was not invalidated by a reboot. Where the lane requires an inhibitor
    (expect['inhibitor_required'], default True: the authority sets awake.hold_inhibitor on every lane of a host whose
    inventory power mode is 'sleeps'), the fence must say inhibitor_held and the reply's inhibitor must be held under the
    lane/generation unit name flightctl-awake-<lane>-g<generation>.service."""
    ident = reply.get("echoed_identity") or {}
    lane_id = expect.get("lane_id")
    if lane_id is None or (ident.get("lane") or {}).get("lane_id") != lane_id:
        return False
    on_lane = [f for f in reply.get("fences") or [] if f["identity"]["lane"] == ident["lane"]]
    if len(on_lane) != 1 or on_lane[0]["identity"] != ident:
        return False
    fence = on_lane[0]
    if fence["state"] not in FENCED_STATES or fence["state"] != reply["observed_state"] or fence["rebooted_since_reserve"]:
        return False
    if expect.get("inhibitor_required", True):
        inhibitor = reply.get("inhibitor")
        unit = f"flightctl-awake-{lane_id}-g{ident['generation']}.service"
        if not (fence["inhibitor_held"] is True and inhibitor is not None and inhibitor["held"] is True and inhibitor["unit"] == unit):
            return False
    return True


def grant_after_reserve(replies: list[Mapping[str, Any]], approved_max_end_t: float, expect: Mapping[str, Any] | None = None) -> str:
    """N1 oracle (rounds 4-8). Each entry is {'received_t': float, 'reply': <executor reply in the WIRE shape of
    executor.schema.json>} or {'lost': True}; expect = {'lease_id', 'generation', 'host_id', 'lane_id', 'request_ids':
    set, 'inhibitor_required': bool (default True)}. A reply counts only if it validates against the executor reply
    schema, is ok=True, definite=True and dry_run=False, its controller_request_id is one this authority sent for this
    reserve, its nested echoed_identity and host_boot name the same lease_id, generation and host, AND (round 8) it
    carries the matching persisted fence evidence (fence_evidence_ok). Round 7: a 'reserve' reply with observed_state
    'reserved' must come first; only after it may a 'ceiling' reply in a fenced state confirm a shortened ceiling.
    Any other kind, a dry-run, or a free/unknown state never confirms."""
    if expect is None:
        return "pending ceiling-unconfirmed"
    reserved = False
    for entry in replies:
        if entry.get("lost"):
            continue
        reply = entry.get("reply")
        if not isinstance(reply, Mapping) or errors(reply, "executor", "reply"):
            continue
        ident = reply.get("echoed_identity") or {}
        if not (reply["ok"] is True and reply["definite"] is True and reply["dry_run"] is False
                and reply["controller_request_id"] in expect["request_ids"]
                and ident.get("lease_id") == expect["lease_id"]
                and ident.get("generation") == expect["generation"]
                and (ident.get("lane") or {}).get("host_id") == expect["host_id"]
                and reply.get("host_boot", {}).get("host_id") == expect["host_id"]
                and fence_evidence_ok(reply, expect)):
            continue
        if reply["kind"] == "reserve" and reply["observed_state"] == "reserved":
            reserved = True
        elif not (reply["kind"] == "ceiling" and reserved and reply["observed_state"] in FENCED_STATES):
            continue
        if ceiling_confirmed(reply_received_t=entry["received_t"], max_end_remaining_s=reply["max_end_remaining_s"], approved_max_end_t=approved_max_end_t):
            return "grant"
    return "pending ceiling-unconfirmed"


def executor_reply(*, lease_id: str, generation: int, host_id: str, controller_request_id: str, remaining: float | None,
                   ok: bool = True, definite: bool = True, kind: str = "reserve", observed_state: str | None = None,
                   dry_run: bool = False, fenced: bool = True, inhibitor_held: bool = True) -> dict[str, Any]:
    """Build an executor reply in the exact WIRE shape of executor.schema.json (nested echoed_identity, host_boot).
    By default an ok reply in a fence state carries its persisted fence and a held inhibitor (round 8); fenced=False
    builds Sol 6 r7's reply (observed_state reserved, fences [], inhibitor null)."""
    import copy

    reserve = copy.deepcopy(examples("executor")["valid"][0])
    ident = reserve["identity"]
    ident.update(lease_id=lease_id, generation=generation)
    ident["lane"]["host_id"] = host_id
    ident["unit"] = f"flightctl-{ident['lane']['lane_id']}-g{generation}.service"
    state = observed_state or ("reserved" if ok else "free")
    fence_states = {"reserved", "staging", "starting", "running", "stopping", "quarantined", "closed"}
    with_fence = fenced and ok and state in fence_states
    fences = [{"identity": copy.deepcopy(ident), "state": state, "local_deadlines": [], "inhibitor_held": inhibitor_held,
               "rebooted_since_reserve": False}] if with_fence else []
    inhibitor = ({"held": inhibitor_held, "unit": f"flightctl-awake-{ident['lane']['lane_id']}-g{generation}.service", "what": "idle"}
                 if with_fence else None)
    reply = {"schema_version": 2, "kind": kind, "controller_request_id": controller_request_id, "echoed_identity": ident,
             "ok": ok, "definite": definite, "observed_state": state,
             "host_boot": {"host_id": host_id, "boot_id": "e8a72066-3f84-4fbb-bfd5-9e055bee90c0", "observed_at": "2026-10-02T10:00:01Z"},
             "host_utc": "2026-10-02T10:00:01Z", "fences": fences, "unit": None, "occupancy": None, "inhibitor": inhibitor, "stage": None,
             "log_lines": [], "next_cursor": None, "error": None if ok else {"code": "unknown", "message": "reserve not applied", "layer": "executor", "cause": None},
             "dry_run": dry_run, "max_end_remaining_s": remaining, "output": None}
    return reply


def parse_utc(value: str):
    """Parse a contract utc_time ('...Z', optional fraction) to an aware datetime. Never compare timestamps as text."""
    from datetime import datetime, timezone

    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"not a UTC timestamp: {value!r}")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    return parsed.astimezone(timezone.utc)


def auth_decision(peer_ids: list[Mapping[str, str]], accounts: list[Mapping[str, Any]], token: Mapping[str, Any] | None,
                  now: str) -> tuple[str, str | None]:
    """B6 oracle (round 5). The request carries no principal: it is derived from the peer's external ids, or selected
    by a token accepted from an allowed peer. Expiry compares PARSED aware datetimes (now >= expires_at denies);
    revoked tokens deny; a rejected token never falls back."""
    by_id = {a["principal_id"]: a for a in accounts}
    if token is not None:
        owner = by_id.get(token["principal_id"])
        try:
            expired = parse_utc(now) >= parse_utc(token["expires_at"])
        except (ValueError, TypeError, KeyError):
            return "deny", None  # unparsable or missing expiry fails closed (round 7 self-probe)
        if owner is None or not owner["enabled"] or token.get("revoked_at") or expired:
            return "deny", None
        allowed = owner["allowed_peers"] + owner["external_ids"]
        if not any(p in allowed for p in peer_ids):
            return "deny", None
        return "allow", owner["principal_id"]
    matches = [a for a in accounts if any(p in a["external_ids"] for p in peer_ids)]
    if len(matches) != 1 or not matches[0]["enabled"]:
        return "deny", None
    return "allow", matches[0]["principal_id"]




def work_admission(active_parents: Mapping[tuple[str, str], str], principal_id: str, host_id: str, kind: str,
                   lease_id: str, principal_kind: str) -> tuple[str, str | None]:
    """B5 oracle (round 4). kind: 'parent' (a lease) or 'child' (session/job naming lease_id).
    active_parents maps (principal_id, host_id) -> parent lease_id."""
    if principal_kind != "human":
        return "allow", None
    current = active_parents.get((principal_id, host_id))
    if kind == "parent":
        return ("deny", "account_busy") if current is not None else ("allow", None)
    return ("allow", None) if current == lease_id else ("deny", "account_busy")


def admit_parent(db_path: str, principal_id: str, host_id: str, lease_id: str) -> bool:
    """B5 reference for the authority side: one active parent per (principal, host), enforced by a UNIQUE index inside
    the admission transaction (BEGIN IMMEDIATE). Returns True if this lease became the parent."""
    import sqlite3

    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS parent(principal_id TEXT, host_id TEXT, lease_id TEXT, active INTEGER)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_parent ON parent(principal_id, host_id) WHERE active = 1")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("INSERT INTO parent VALUES (?, ?, ?, 1)", (principal_id, host_id, lease_id))
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            return False
        conn.execute("COMMIT")
        return True
    finally:
        conn.close()


FRIEND_ACCOUNT_RE = r"^fc-[a-z0-9][a-z0-9_-]{0,28}$"
# round 8 (Sol 6 r7): claim-clear is a SEPARATE root-only program (mode 0700 root:root) reachable only through the
# operator's own sudoers rule, which must re-authenticate (no NOPASSWD). The executor's helper has no claim-clear.
CLEAR_PATH = "/usr/local/libexec/flightctl-claim-clear"


def _claim_paths(state_dir: str, account: str) -> tuple[str, str, str]:
    import os
    import re

    if not re.fullmatch(FRIEND_ACCOUNT_RE, account) or account == "fc-svc":
        raise ValueError(f"account {account!r} is not an allowlisted friend account")
    return (os.path.join(state_dir, f"{account}.parent"), os.path.join(state_dir, f"{account}.quarantined"),
            os.path.join(state_dir, f".{account}.lock"))


@contextlib.contextmanager
def _account_lock(lock_path: str, enabled: bool = True):
    """Round 8: one exclusive flock per account serialises admission+start, release/clear and the reconcile marker
    write, so a quarantine can never land between a child's admission and its start. `enabled=False` exists ONLY so a
    race-injection test hook (which runs while the lock is held) can model a path that bypasses it."""
    import fcntl
    import os

    if not enabled:
        yield
        return
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _quarantine(marker: str, status: str) -> None:
    try:
        with open(marker, "x", encoding="utf-8") as handle:
            handle.write(status)
    except FileExistsError:
        pass


def _unlink_if_ours(state_dir: str, account: str, path: str, our_ino: int) -> bool:
    """Tombstone technique (rounds 6 and 8): rename the claim path to a unique tombstone and delete it only if the
    tombstone is the inode we expect; otherwise put it back and refuse. If the put-back finds a NEW claim at the path,
    nothing is deleted: the tombstone stays for the operator and the account is quarantined (fail closed)."""
    import os
    import uuid

    _, marker, _ = _claim_paths(state_dir, account)
    tomb = os.path.join(state_dir, f".{account}.tomb.{uuid.uuid4().hex}")
    try:
        os.rename(path, tomb)
    except FileNotFoundError:
        return False
    if os.lstat(tomb).st_ino != our_ino:
        try:
            os.link(tomb, path)
            os.unlink(tomb)
        except FileExistsError:
            _quarantine(marker, "conflict")
        return False
    os.unlink(tomb)
    return True


def helper_claim(state_dir: str, account: str, lease_id: str, boot_id: str = "boot-a", *, start=None,
                 _after_link=None, _before_rollback=None, _lock: bool = True) -> bool:
    """B5 reference for the helper's admission (rounds 5-8). session-open and friend unit-start carry --parent-lease.
    ONE RULE (round 8): while the account is quarantined NO claim of any kind is admitted (no new parent, no child of
    the old parent); otherwise the request is admitted only if it claims the parent (no claim yet) or names the claimed
    parent (a child). The claim is a complete temp file published with link(); `start` (the actual session-open or
    systemd-run) runs while the account lock is still held, so a quarantine cannot land between admission and start.
    Defence in depth (round 8, Sol 6 r7): if a marker appears after the link, the rollback deletes the claim path only
    if it still names our own inode (tombstone check); if the path was cleared and replaced meanwhile, we refuse
    without touching the new claim."""
    import json
    import os
    import tempfile

    path, marker, lock = _claim_paths(state_dir, account)
    with _account_lock(lock, _lock):
        if os.path.lexists(marker):
            return False
        fd, tmp = tempfile.mkstemp(prefix=f".{account}.", dir=state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"lease_id": lease_id, "boot_id": boot_id}, handle)
            our_ino = os.lstat(tmp).st_ino
            try:
                os.link(tmp, path)
                linked = True
            except FileExistsError:
                linked = False
            if linked:
                if _after_link is not None:
                    _after_link()
                if os.path.lexists(marker):  # quarantined after our link: roll back only our own inode
                    if _before_rollback is not None:
                        _before_rollback()
                    _unlink_if_ours(state_dir, account, path, our_ino)
                    return False
                try:
                    if os.lstat(path).st_ino != our_ino:
                        return False  # cleared and replaced since our link: not ours any more
                except FileNotFoundError:
                    return False
            else:
                try:
                    fdc = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                except OSError:
                    return False
                with os.fdopen(fdc, encoding="utf-8") as handle:
                    if json.load(handle)["lease_id"] != lease_id or os.path.lexists(marker):
                        return False
            if start is not None:
                start()
            return True
        finally:
            os.unlink(tmp)


CLOSE_PROOF_KEYS = ("user_slice_empty", "occupancy_empty", "key_removed")


def _remove_claim(state_dir: str, account: str, lease_id: str, race_hook) -> bool:
    import json
    import os

    path, _, _ = _claim_paths(state_dir, account)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return True  # already gone (e.g. a retried clear after a crash)
    except OSError:
        return False  # e.g. a symlink planted at the claim path: refuse, never follow
    try:
        with os.fdopen(os.dup(fd), encoding="utf-8") as handle:
            if json.load(handle)["lease_id"] != lease_id:
                return False
        if race_hook is not None:
            race_hook()
        tomb_ok = _unlink_if_ours(state_dir, account, path, os.fstat(fd).st_ino)
        if not tomb_ok and os.path.lexists(path):
            return False
        return True
    finally:
        os.close(fd)


def helper_release(state_dir: str, account: str, lease_id: str, close_proof: Mapping[str, bool], _race_hook=None,
                   *, _lock: bool = True) -> bool:
    """B5 claim-release (rounds 6-8), run through the executor's helper: requires the close proof and the same lease,
    and is race-safe (inode-checked tombstone). It never lifts a quarantine: a quarantined account needs claim-clear."""
    import os

    path, marker, lock = _claim_paths(state_dir, account)
    if not all(close_proof.get(k) is True for k in CLOSE_PROOF_KEYS):
        return False
    with _account_lock(lock, _lock):
        if os.path.lexists(marker):
            return False
        return _remove_claim(state_dir, account, lease_id, _race_hook)


def clear_authorized(invocation: Mapping[str, Any], operator_account: str, operator_uid: int) -> list[str]:
    """Round 8 (Sol 6 r7): how claim-clear verifies operator approval. It is a separate program at CLEAR_PATH, mode
    0700 root:root, so only root can execute it; the executor's helper never execs it and has no claim-clear
    subcommand. It runs only when sudo started it for the operator: real and effective UID 0, and SUDO_USER/SUDO_UID
    (set by sudo itself, after env_reset) equal to helper.json operator_account and that account's UID. The operator's
    sudoers rule must re-authenticate (no NOPASSWD); `operator_sudo_audit` checks it on the installed host. Input =
    {'program': argv0 realpath, 'ruid', 'euid', 'sudo_user', 'sudo_uid'}."""
    problems = []
    if invocation.get("program") != CLEAR_PATH:
        problems.append("claim-clear must run as its own root-only program, never through the executor's helper")
    if invocation.get("ruid") != 0 or invocation.get("euid") != 0:
        problems.append("claim-clear must run as root (real and effective UID 0)")
    if invocation.get("sudo_user") != operator_account or invocation.get("sudo_uid") != operator_uid:
        problems.append("claim-clear must be started by sudo for the configured operator account")
    return problems


def helper_clear(state_dir: str, account: str, lease_id: str, close_proof: Mapping[str, bool],
                 invocation: Mapping[str, Any], operator_account: str, operator_uid: int, *, _lock: bool = True) -> bool:
    """B5 claim-clear (round 8): the only way a quarantine is lifted. Operator authorization (clear_authorized), the
    close proof and the claimed lease are all required; the claim is removed first (tombstone-checked) and the marker
    last, so a crash in between leaves the account blocked."""
    import os

    path, marker, lock = _claim_paths(state_dir, account)
    if clear_authorized(invocation, operator_account, operator_uid):
        return False
    if not all(close_proof.get(k) is True for k in CLOSE_PROOF_KEYS):
        return False
    with _account_lock(lock, _lock):
        if not os.path.lexists(marker):
            return False  # nothing to clear: an unquarantined claim is released with claim-release
        if not _remove_claim(state_dir, account, lease_id, None):
            return False
        os.unlink(marker)
        return True


def helper_reconcile(state_dir: str, active_parents: Mapping[str, str], current_boot_id: str, *, _lock: bool = True) -> dict[str, str]:
    """B5 reconcile (rounds 6-8): NEVER removes a claim. A claim the authority lists is 'kept'; one it omits is
    'orphaned-quarantined' and one naming another parent is 'conflict'. Both write the account's quarantine marker
    under the account lock (admission effect: helper_claim refuses everything) and raise an operator alert; only
    claim-clear lifts it."""
    import json
    import os

    outcome = {}
    for name in sorted(os.listdir(state_dir)):
        if not name.endswith(".parent") or name.startswith("."):
            continue
        account = name[: -len(".parent")]
        path, marker, lock = _claim_paths(state_dir, account)
        with _account_lock(lock, _lock):
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            except OSError:
                continue
            with os.fdopen(fd, encoding="utf-8") as handle:
                claim = json.load(handle)
            want = active_parents.get(account)
            if want == claim["lease_id"] and not os.path.lexists(marker):
                outcome[account] = "kept"
                continue
            status = "kept-quarantined" if want == claim["lease_id"] else ("orphaned-quarantined" if want is None else "conflict")
            _quarantine(marker, status)
            outcome[account] = status
    return outcome


class SplitStore:
    """Round 5 reference for D-token-4 crash consistency (main authority DB + never-backed-up replay store).

    ORDER for a grant: (1) commit the replay row (request_id, token, deadline) in the replay store; (2) commit the
    lease + token-free idempotency record (with request_fingerprint and replay_deadline) in the main DB. The main DB
    is the source of truth.
    RECOVERY at startup: delete replay rows whose request_id has no committed idempotency record (a crash between 1
    and 2 left an orphan that was never granted).
    SCOPE (round 9, Sol 6 r8): idempotency and replay rows are keyed by (authenticated principal_id, request_id), as
    rpc-envelope states; another principal reusing the same request_id (even with the same payload) is a different
    request and gets a fresh decision, never this principal's lease or token.
    REPLAY: round 8 (Sol 6 r7) checks the request fingerprint FIRST: the same request_id with a different fingerprint
    is 409 conflict at any time and never sees the stored response. A matching retry returns the grant with its token
    if the replay row exists and is within its deadline; at/after the deadline it gets the null-token replay; inside
    the window with the row missing (restored main DB, lost replay store) it is 409 replay_unavailable: no token is
    invented and NO second grant is made; the orphaned lease is closed through the holder-lost path."""

    def __init__(self, main_path: str, replay_path: str):
        import sqlite3

        self.main = sqlite3.connect(main_path, isolation_level=None)
        self.replay = sqlite3.connect(replay_path, isolation_level=None)
        for conn in (self.main, self.replay):
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA secure_delete=ON")
        self.main.execute("CREATE TABLE IF NOT EXISTS idem(principal_id TEXT NOT NULL, request_id TEXT NOT NULL, request_fingerprint TEXT NOT NULL, lease_id TEXT, lane TEXT, replay_deadline REAL, PRIMARY KEY (principal_id, request_id))")
        self.main.execute("CREATE TABLE IF NOT EXISTS lease(lease_id TEXT PRIMARY KEY, lane TEXT, token_sha256 TEXT, state TEXT)")
        self.main.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active ON lease(lane) WHERE state IN ('active', 'holder-lost')")
        self.replay.execute("CREATE TABLE IF NOT EXISTS replay(principal_id TEXT NOT NULL, request_id TEXT NOT NULL, token TEXT, deadline REAL, PRIMARY KEY (principal_id, request_id))")

    def grant(self, request_id: str, lane: str, lease_id: str, token: str, deadline: float, *, principal: str, fingerprint: str, now: float,
              crash_between: bool = False) -> dict:
        import hashlib

        known = self.replay_or_record(request_id, principal=principal, fingerprint=fingerprint, now=now)  # round 7: the real retry time
        if known is not None:
            return known
        self.replay.execute("INSERT INTO replay VALUES (?, ?, ?, ?)", (principal, request_id, token, deadline))  # step 1
        if crash_between:
            raise RuntimeError("crash between the two commits")
        self.main.execute("BEGIN IMMEDIATE")
        try:
            self.main.execute("INSERT INTO lease VALUES (?, ?, ?, 'active')", (lease_id, lane, hashlib.sha256(token.encode()).hexdigest()))
            self.main.execute("INSERT INTO idem VALUES (?, ?, ?, ?, ?, ?)", (principal, request_id, fingerprint, lease_id, lane, deadline))
        except Exception:
            self.main.execute("ROLLBACK")
            self.replay.execute("DELETE FROM replay WHERE principal_id = ? AND request_id = ?", (principal, request_id))
            return {"status": 409, "code": "busy"}
        self.main.execute("COMMIT")
        return {"status": 200, "lease_id": lease_id, "token": token}

    def replay_or_record(self, request_id: str, *, principal: str, fingerprint: str, now: float) -> dict | None:
        """Round 6 (Sol 6 r5): the token-free replay deadline lives in the MAIN record, so a missing replay row is
        interpreted by the main DB alone. Round 8 (Sol 6 r7): a fingerprint mismatch is 409 conflict before anything
        else, with no lease id and no token."""
        rec = self.main.execute("SELECT request_fingerprint, lease_id, replay_deadline FROM idem WHERE principal_id = ? AND request_id = ?", (principal, request_id)).fetchone()
        if rec is None:
            return None
        stored_fp, lease_id, deadline = rec
        if stored_fp != fingerprint:
            return {"status": 409, "code": "conflict"}
        if now >= deadline:
            return {"status": 200, "lease_id": lease_id, "token": None}
        row = self.replay.execute("SELECT token FROM replay WHERE principal_id = ? AND request_id = ?", (principal, request_id)).fetchone()
        if row is not None:
            return {"status": 200, "lease_id": lease_id, "token": row[0]}
        self.main.execute("UPDATE lease SET state = 'holder-lost' WHERE lease_id = ? AND state = 'active'", (lease_id,))
        return {"status": 409, "code": "replay_unavailable", "lease_id": lease_id}

    def scrub(self, now: float) -> int:
        """Routine end-of-window scrub of raw tokens (secure_delete + WAL truncate)."""
        cur = self.replay.execute("DELETE FROM replay WHERE deadline <= ?", (now,))
        self.replay.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return cur.rowcount

    def recover(self) -> int:
        ids = {tuple(r) for r in self.main.execute("SELECT principal_id, request_id FROM idem")}
        orphans = [tuple(r) for r in self.replay.execute("SELECT principal_id, request_id FROM replay") if tuple(r) not in ids]
        for pid, rid in orphans:
            self.replay.execute("DELETE FROM replay WHERE principal_id = ? AND request_id = ?", (pid, rid))
        return len(orphans)

    def active_leases(self, lane: str) -> int:
        return self.main.execute("SELECT count(*) FROM lease WHERE lane = ? AND state IN ('active', 'holder-lost')", (lane,)).fetchone()[0]

    def close_after_proof(self, lease_id: str) -> None:
        """The holder-lost lease frees the lane only through the normal stop + emptiness proof."""
        self.main.execute("UPDATE lease SET state = 'closed' WHERE lease_id = ?", (lease_id,))

    def close(self) -> None:
        self.main.close()
        self.replay.close()




def contained_open(root_fd: int, relpath: str, expect_sha256: str | None = None) -> bytes:
    """Reference for the output containment rule (executor 'output' kind): reject absolute paths, '.', '..' and empty
    components, then walk component by component from a directory fd with O_NOFOLLOW, refuse symlinks and non-regular
    files, verify the expected hash on the open fd. Raises PermissionError (escape) or ValueError (changed)."""
    import hashlib
    import os
    import stat

    if not relpath or relpath.startswith("/") or "\x00" in relpath:
        raise PermissionError("relpath must be relative")
    parts = relpath.split("/")
    if any(p in {"", ".", ".."} for p in parts):
        raise PermissionError("relpath must be relative and contained")
    fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = nxt
        leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=fd)
    except OSError as exc:
        raise PermissionError(f"refused: {exc.strerror}") from exc
    finally:
        os.close(fd)
    try:
        if not stat.S_ISREG(os.fstat(leaf).st_mode):
            raise PermissionError("not a regular file")
        with os.fdopen(leaf, "rb", closefd=False) as handle:
            data = handle.read()
    finally:
        os.close(leaf)
    if expect_sha256 is not None and hashlib.sha256(data).hexdigest() != expect_sha256:
        raise ValueError("output changed between list and get")
    return data


def stage_outputs(staging_fd: int, store_fd: int, job_uid: int) -> dict[str, Any]:
    """Round 4 reference for trusted output staging (run after the job unit stopped and its cgroup is empty).
    Accepts only regular files with st_nlink == 1 and st_uid == job_uid (checked with fstat on the O_NOFOLLOW fd) and
    COPIES their bytes into the executor-owned store; everything else is rejected with a reason."""
    import hashlib
    import os
    import stat

    accepted: dict[str, str] = {}
    rejected: dict[str, str] = {}

    def walk(src_fd: int, dst_fd: int, prefix: str) -> None:
        for name in sorted(os.listdir(src_fd)):
            rel = f"{prefix}{name}"
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=src_fd)
            except OSError as exc:
                rejected[rel] = f"open refused: {exc.strerror}"
                continue
            try:
                st = os.fstat(fd)
                if stat.S_ISDIR(st.st_mode) and st.st_uid == job_uid:
                    os.mkdir(name, 0o750, dir_fd=dst_fd)
                    sub = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dst_fd)
                    try:
                        walk(fd, sub, rel + "/")
                    finally:
                        os.close(sub)
                    continue
                if not stat.S_ISREG(st.st_mode):
                    rejected[rel] = "not a regular file"
                elif st.st_nlink != 1:
                    rejected[rel] = f"hard link (st_nlink={st.st_nlink})"
                elif st.st_uid != job_uid:
                    rejected[rel] = "not owned by the job account"
                else:
                    with os.fdopen(fd, "rb", closefd=False) as src:
                        data = src.read()
                    out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640, dir_fd=dst_fd)
                    with os.fdopen(out, "wb") as dst:
                        dst.write(data)
                    accepted[rel] = hashlib.sha256(data).hexdigest()
            finally:
                os.close(fd)

    walk(staging_fd, store_fd, "")
    return {"accepted": accepted, "rejected": rejected}




HELPER_PATH = "/usr/local/libexec/flightctl-helper"
HELPER_CONFIG = "/etc/flightctl/helper.json"
# secure_path may name only these (the C7h install audit also stats each: root-owned, not group/other-writable)
SECURE_PATH_ALLOWED = frozenset({"/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"})


def sudo_l_grants(text: str, user: str) -> list[dict[str, Any]]:
    """Parse the EFFECTIVE grants from `sudo -l -U <user>` output (format observed on sudo 1.9.17p2, 2 Oct 2026):
    header 'User <u> may run the following commands on <h>:', grants indented 4 spaces starting with '(',
    continuation lines indented 8 spaces joined with one space, optional 'TAG: ' prefixes, comma-separated commands.
    Group (%grp) and alias grants appear here already expanded, which is why the audit uses this output."""
    import re

    lines = text.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if re.match(rf"^User {re.escape(user)} may run the following commands on \S+:$", l))
    except StopIteration:
        return []
    entries: list[str] = []
    for line in lines[start + 1:]:
        if not line.strip():
            break
        if line.startswith("        ") and entries:
            entries[-1] += " " + line.strip()
        elif line.startswith("    ("):
            entries.append(line.strip())
        else:
            break
    grants = []
    for entry in entries:
        m = re.match(r"^\(([^)]*)\)\s*(.*)$", entry)
        if not m:
            grants.append({"runas": None, "tags": [], "commands": [entry], "raw": entry})
            continue
        rest = m.group(2)
        tags = []
        while True:
            t = re.match(r"^([A-Z_]+):\s*(.*)$", rest)
            if not t:
                break
            tags.append(t.group(1))
            rest = t.group(2)
        grants.append({"runas": m.group(1), "tags": tags, "commands": [c.strip() for c in rest.split(", ") if c.strip()], "raw": entry})
    return grants


def sudoers_audit(sudo_l_text: str, caller: str) -> list[str]:
    """C7h EFFECTIVE-privilege audit (round 4): run on `sudo -l -U <caller>` output from the installed host.
    The caller must have exactly one effective grant: (root) NOPASSWD: <helper>, no other command, no SETENV,
    and its matching Defaults must not weaken the environment."""
    problems = []
    grants = sudo_l_grants(sudo_l_text, caller)
    if len(grants) != 1:
        problems.append(f"{caller} has {len(grants)} effective sudo grants; exactly one (the helper) is allowed")
    for g in grants:
        if g["runas"] != "root":
            problems.append(f"grant runs as ({g['runas']}), not (root): {g['raw']}")
        if "NOPASSWD" not in g["tags"] or "SETENV" in g["tags"]:
            problems.append(f"grant tags {g['tags']} must be NOPASSWD without SETENV: {g['raw']}")
        if g["commands"] != [HELPER_PATH]:
            problems.append(f"grant allows {g['commands']}, not exactly [{HELPER_PATH}]")
    defaults = sudo_l_text.split("may run the following commands")[0]
    import re

    if "!env_reset" in defaults:
        problems.append("Defaults weaken the environment (!env_reset)")
    # round 5 (Sol 6 r4): the Defaults C7h names must be PRESENT, not merely not negated
    entries = {e.strip().split("=", 1)[0] for e in re.split(r",\s*|\n\s*", defaults) if e.strip()}
    for required in ("env_reset", "!setenv", "secure_path"):
        if required not in entries:
            problems.append(f"required Defaults entry {required} is missing for {caller}")
    # round 6 (Sol 6 r5): secure_path must be a non-empty list of standard root-owned system directories
    m = re.search(r"secure_path=([^,\n]*)", defaults)
    if m is not None:
        raw = m.group(1).strip()
        if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
            raw = raw[1:-1]
        parts = re.split(r"\\?:", raw)  # sudo -l escapes ':' as '\:'; both spellings separate entries
        dirs = [d for d in parts if d]
        if not dirs:
            problems.append("secure_path is empty")
        elif len(dirs) != len(parts):
            # round 7 (Sol 6 r6): an empty component (leading, trailing or doubled separator) means the current directory
            problems.append("secure_path has an empty component (current directory in PATH)")
        for d in dirs:
            if d not in SECURE_PATH_ALLOWED:
                problems.append(f"secure_path entry {d!r} is not a standard root-owned system directory")
    if re.search(r"(?<!!)\bsetenv\b", defaults):
        problems.append("Defaults weaken the environment (setenv)")
    if re.search(r"env_keep\s*\+?=\s*\"?[^\"\n]*\b(LD_|PYTHON)", defaults):
        problems.append("Defaults weaken the environment (env_keep of loader/interpreter variables)")
    return problems


def sudoers_file_audit(text: str, caller: str, caller_groups: list[str]) -> list[str]:
    """Static check of an installed sudoers.d file (complements, never replaces, the effective audit): any rule whose
    user list can include the caller (its name, a %group it belongs to, ALL, or a User_Alias containing either) may
    grant only the helper."""
    import re

    aliases: dict[str, set[str]] = {}
    for line in text.splitlines():
        m = re.match(r"^\s*User_Alias\s+(\w+)\s*=\s*(.+)$", line)
        if m:
            aliases[m.group(1)] = {x.strip() for x in m.group(2).split(",")}
    me = {caller, "ALL"} | {f"%{g}" for g in caller_groups}
    problems = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Defaults") and (s.startswith("Defaults ") or s.startswith("Defaults\t") or s.startswith(f"Defaults:{caller}")):
            if "!env_reset" in s or re.search(r"(?<!!)\bsetenv\b", s):
                problems.append(f"Defaults weaken the environment for {caller}: {s}")
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("Defaults") or s.startswith("User_Alias"):
            continue
        users, _, rest = s.partition(" ")
        members = set()
        for u in users.split(","):
            members |= aliases.get(u, {u})
        if not members & me:
            continue
        cmds = rest.split(":", 1)[-1] if ":" in rest else rest.split(")", 1)[-1]
        commands = [c.strip() for c in cmds.split(",")]
        if commands != [HELPER_PATH] or "SETENV" in rest:
            problems.append(f"rule can apply to {caller} and grants more than the helper: {s}")
    return problems




SUDO_BUILTINS = frozenset({"sudoedit", "list"})


def sudo_path_matches(spec: str, program: str) -> bool:
    """Does a sudoers command PATH spec reach `program`? Rules from sudoers(5) (1.9.17p2, read 2 Oct 2026): 'ALL';
    a '^...$' POSIX ERE (1.9.10+); a directory ending in '/' reaches files directly inside it (not sub-directories);
    otherwise shell-style wildcards (*, ?, [...]) that never match '/'. An unparsable regex is treated as reaching
    (fail closed)."""
    import fnmatch
    import posixpath
    import re

    if spec == "ALL":
        return True
    if spec.startswith("^") and spec.endswith("$"):
        try:
            return re.fullmatch(spec[4:] if spec.startswith("^(?i)") else spec, program, re.I if spec.startswith("^(?i)") else 0) is not None
        except re.error:
            return True
    if spec.endswith("/"):
        spec, program = spec[:-1], posixpath.dirname(program)
    want, have = spec.split("/"), program.split("/")
    return len(want) == len(have) and all(fnmatch.fnmatchcase(h, w) for h, w in zip(have, want))


def _command_reaches(command: str, program: str, cmnd_aliases: Mapping[str, list[str]] | None = None, _depth: int = 0) -> bool:
    """One Cmnd from a grant: 'path [args]' (arguments never limit reach here: the clear program is dangerous with any
    argument, so an argument-bearing grant counts), '!path' (a negation grants nothing, and is NOT allowed to cancel a
    route: fail closed), a built-in, or an alias NAME. A known Cmnd_Alias is expanded; an unknown alias name is treated
    as reaching (fail closed)."""
    import re

    c = command.strip()
    if not c or c.startswith("!"):
        return False
    path = c.split()[0]
    if path in SUDO_BUILTINS:
        return False
    if path == "ALL" or path.startswith("/") or path.startswith("^"):
        return sudo_path_matches(path, program)
    if re.fullmatch(r"[A-Z][A-Z0-9_]*", path):
        if cmnd_aliases is not None and path in cmnd_aliases and _depth < 16:
            return any(_command_reaches(x, program, cmnd_aliases, _depth + 1) for x in cmnd_aliases[path])
        return True
    return True  # anything unrecognised: fail closed


def _grant_reaches(grant: Mapping[str, Any], program: str, cmnd_aliases: Mapping[str, list[str]] | None = None) -> bool:
    if grant["runas"] is None:
        return True  # unparsable grant line: fail closed
    return any(_command_reaches(c, program, cmnd_aliases) for c in grant["commands"])


def parse_sudoers_rules(text: str) -> tuple[dict[str, set[str]], dict[str, list[str]], list[dict[str, Any]]]:
    """Static sudoers parse for the C7h install audit: User_Alias and Cmnd_Alias definitions (including 'A = x : B = y'
    on one line) and user specs 'users hosts = (runas) TAGS: cmnd, ...'. Continuation lines ending in '\\' are joined."""
    import re

    joined = re.sub(r"\\\n\s*", " ", text)
    users: dict[str, set[str]] = {}
    cmnds: dict[str, list[str]] = {}
    rules = []
    for line in joined.splitlines():
        s = line.split("#", 1)[0].strip() if not line.strip().startswith("#") else ""
        if not s or s.startswith("Defaults"):
            continue
        m = re.match(r"^(User_Alias|Cmnd_Alias|Cmd_Alias|Host_Alias|Runas_Alias)\s+(.*)$", s)
        if m:
            for part in m.group(2).split(" : "):
                name, _, value = part.partition("=")
                items = [x.strip() for x in value.split(",") if x.strip()]
                if m.group(1) == "User_Alias":
                    users[name.strip()] = set(items)
                elif m.group(1) in ("Cmnd_Alias", "Cmd_Alias"):
                    cmnds[name.strip()] = items
            continue
        who, _, rest = s.partition(" ")
        if "=" not in rest:
            continue
        spec = rest.split("=", 1)[1].strip()
        runas = None
        rm = re.match(r"^\(([^)]*)\)\s*(.*)$", spec)
        if rm:
            runas, spec = rm.group(1), rm.group(2)
        tags = []
        while True:
            t = re.match(r"^([A-Z_]+):\s*(.*)$", spec)
            if not t:
                break
            tags.append(t.group(1))
            spec = t.group(2)
        rules.append({"users": [u.strip() for u in who.split(",")], "runas": runas if runas is not None else "root", "tags": tags,
                      "commands": [c.strip() for c in spec.split(",") if c.strip()], "raw": s})
    return users, cmnds, rules


def _static_routes(sudoers_text: str, account: str, groups: list[str] | tuple[str, ...], program: str) -> list[dict[str, Any]]:
    user_aliases, cmnd_aliases, rules = parse_sudoers_rules(sudoers_text)
    me = {account, "ALL"} | {f"%{g}" for g in groups}

    def members(u: str, depth: int = 0) -> set[str]:
        if u in user_aliases and depth < 16:
            out: set[str] = set()
            for x in user_aliases[u]:
                out |= members(x, depth + 1)
            return out
        return {u}

    found = []
    for rule in rules:
        names: set[str] = set()
        for u in rule["users"]:
            names |= members(u)
        if names & me and _grant_reaches(rule, program, cmnd_aliases):
            found.append(rule)
    return found


def _defaults_lines(sudo_l_text: str) -> tuple[list[str], list[str]]:
    """Split `sudo -l -U <u>` Defaults output into the 'Matching Defaults entries' parameters and the
    'Runas and Command-specific defaults' lines (format measured on sudo 1.9.17p2: each line '    Defaults!<cmnds> <params>'
    or '    Defaults><runas> <params>')."""
    import re

    lines = sudo_l_text.splitlines()
    matching: list[str] = []
    specific: list[str] = []
    mode = None
    for line in lines:
        if line.startswith("Matching Defaults entries for "):
            mode = "m"
            continue
        if line.startswith("Runas and Command-specific defaults for "):
            mode = "s"
            continue
        if not line.strip() or not line.startswith("    "):
            mode = None if not line.startswith("    ") else mode
            continue
        if mode == "m":
            matching += [e.strip() for e in re.split(r",\s*", line.strip()) if e.strip()]
        elif mode == "s":
            specific.append(line.strip())
    return matching, specific


def _param_value(params: list[str], name: str):
    value = None
    for p in params:
        if p == f"!{name}":
            value = False
        elif p == name:
            value = True
        elif p.startswith(f"{name}="):
            value = p.split("=", 1)[1].strip().strip('"')
    return value


def clear_fresh_auth_audit(sudo_l_text: str, cmnd_aliases: Mapping[str, list[str]] | None = None) -> list[str]:
    """Round 9 (Sol 6 r8): sudo caches credentials (timestamp_timeout, default 5 minutes), so a password rule alone does
    not re-authenticate. The EFFECTIVE timestamp_timeout for CLEAR_PATH must be 0 ('always prompt', sudoers(5)) and
    authentication must not be disabled. Precedence follows sudoers(5): matching (global/host/user) Defaults first,
    then runas-specific (Defaults>root/ALL), then command-specific Defaults!<cmnd> 'applied later, once the command's
    path is known'; later entries override earlier ones. A Defaults! line naming an unresolvable alias that touches
    these options fails the audit (fail closed)."""
    import re

    matching, specific = _defaults_lines(sudo_l_text)
    problems = []
    timeout = _param_value(matching, "timestamp_timeout")
    auth = _param_value(matching, "authenticate")
    staged = {">": [], "!": []}
    for line in specific:
        m = re.match(r"^Defaults([!>])(\S+)\s+(.*)$", line)
        if not m:
            problems.append(f"unparsable command-specific Defaults line: {line}")
            continue
        kind, targets, params = m.group(1), m.group(2), [p.strip() for p in re.split(r",\s*", m.group(3)) if p.strip()]
        staged[kind].append((targets.split(","), params, line))
    for kind in (">", "!"):
        for targets, params, line in staged[kind]:
            touches = any(p.lstrip("!").split("=", 1)[0] in ("timestamp_timeout", "authenticate") for p in params)
            if kind == ">":
                applies = any(t in ("root", "ALL") for t in targets)
            else:
                unresolved = [t for t in targets if re.fullmatch(r"[A-Z][A-Z0-9_]*", t) and t != "ALL" and not (cmnd_aliases and t in cmnd_aliases)]
                if unresolved and touches:
                    problems.append(f"cannot resolve {unresolved} in {line}; state the claim-clear path literally")
                applies = any(_command_reaches(t, CLEAR_PATH, cmnd_aliases) for t in targets if t not in unresolved)
            if applies:
                t = _param_value(params, "timestamp_timeout")
                timeout = t if t is not None else timeout
                a = _param_value(params, "authenticate")
                auth = a if a is not None else auth
    if auth is False:
        problems.append("authentication is disabled (!authenticate) for claim-clear")
    try:
        ok = timeout is not None and float(timeout) == 0.0
    except (TypeError, ValueError):
        ok = False
    if not ok:
        problems.append(f"effective timestamp_timeout for {CLEAR_PATH} is {timeout if timeout is not None else 'the default (5)'}; it must be 0 so every invocation prompts")
    return problems


def operator_sudo_audit(sudo_l_text: str, operator: str, sudoers_text: str | None = None, groups: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Rounds 8-9 (Sol 6 r7, r8): `sudo -l -U <operator>` on the installed host (plus, when given, the installed sudoers
    text for Cmnd_Alias expansion). The operator must reach CLEAR_PATH as root; EVERY grant that reaches it (exact,
    ALL, wildcard, directory, regex, argument-bearing or alias) must re-authenticate: no NOPASSWD, no SETENV; and the
    effective timestamp_timeout for the program must be 0 (clear_fresh_auth_audit)."""
    cmnd_aliases = parse_sudoers_rules(sudoers_text)[1] if sudoers_text else None
    grants = [g for g in sudo_l_grants(sudo_l_text, operator) if _grant_reaches(g, CLEAR_PATH, cmnd_aliases)]
    if sudoers_text:
        grants += _static_routes(sudoers_text, operator, groups, CLEAR_PATH)
    problems = []
    if not grants:
        problems.append(f"{operator} has no sudo route to {CLEAR_PATH}")
    for g in grants:
        if g["runas"] not in ("root", "ALL", "ALL : ALL", "root : root"):
            problems.append(f"grant does not run as root: {g['raw']}")
        if "NOPASSWD" in g["tags"] or "SETENV" in g["tags"]:
            problems.append(f"route to claim-clear must re-authenticate (no NOPASSWD/SETENV): {g['raw']}")
    problems += clear_fresh_auth_audit(sudo_l_text, cmnd_aliases)
    return problems


def no_clear_route_audit(sudo_l_text: str, account: str, sudoers_text: str | None = None, groups: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Rounds 8-9: the executor and every friend account must have no sudo route to CLEAR_PATH, counting wildcard,
    directory, regex, argument-bearing, ALL and alias grants (unknown aliases fail closed)."""
    cmnd_aliases = parse_sudoers_rules(sudoers_text)[1] if sudoers_text else None
    found = [g["raw"] for g in sudo_l_grants(sudo_l_text, account) if _grant_reaches(g, CLEAR_PATH, cmnd_aliases)]
    if sudoers_text:
        found += [r["raw"] for r in _static_routes(sudoers_text, account, groups, CLEAR_PATH)]
    return [f"{account} can reach claim-clear: {raw}" for raw in found]


def helper_config_semantics(cfg: Mapping[str, Any]) -> list[str]:
    """Round 8: cross-field rules for helper.json that JSON Schema cannot state."""
    problems = []
    op = cfg.get("operator_account")
    if op == cfg.get("caller_account"):
        problems.append("operator_account must not be the executor (caller_account)")
    if op in cfg.get("friend_accounts", []):
        problems.append("operator_account must not be a friend account")
    return problems


# Round 8 (Sol 6 r7): a mention of the withdrawn shared account is allowed ONLY inside one of these withdrawal phrases,
# matched over the occurrence itself (whitespace-normalised), never by a same-line keyword elsewhere on the line.
WITHDRAWAL_PHRASES = (
    r"the shared `?fc-svc`? account is (?:withdrawn|gone)",
    r"\bno (?:shared )?service account is (?:involved|needed any more)",
)


def schema_prose(schema: Any) -> str:
    """The prose of a schema file (every 'description' and every x-* text except x-examples), which is what a reader
    takes as instructions; structural keywords such as a `not: {pattern: ^fc-svc$}` rejection are not prose."""
    out: list[str] = []

    def walk(node: Any, key: str | None = None) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "x-examples":
                    continue
                if isinstance(v, str) and (k == "description" or k.startswith("x-") or (key or "").startswith("x-")):
                    out.append(v)
                else:
                    walk(v, k)
        elif isinstance(node, list):
            for v in node:
                walk(v, key)

    walk(schema)
    return "\n".join(out)


def service_account_offenders(text: str) -> list[str]:
    """Return every 'service account' / fc-svc / service_account occurrence that is not itself inside a withdrawal
    phrase. Unlike a same-line keyword exemption, an affirmative instruction stays an offender even if the word
    'withdrawn' appears elsewhere on the line or in the sentence."""
    import re

    flat = re.sub(r"\s+", " ", text)
    covered: list[tuple[int, int]] = []
    for phrase in WITHDRAWAL_PHRASES:
        covered += [m.span() for m in re.finditer(phrase, flat, re.I)]
    offenders = []
    for m in re.finditer(r"service account|fc-svc|service_account", flat, re.I):
        if not any(a <= m.start() and m.end() <= b for a, b in covered):
            offenders.append(flat[max(0, m.start() - 60): m.end() + 40])
    return offenders


def helper_install_audit(stat_chain: list[Mapping[str, Any]]) -> list[str]:
    """C7h install audit: every path from / to the helper and to its config (and the keys dir) must be root-owned and
    not group/other-writable, so the caller cannot replace the helper or its trusted config. Input = os.stat of each
    component as {path, uid, mode}; the gauge collects it on the real host."""
    problems = []
    for entry in stat_chain:
        if entry["uid"] != 0:
            problems.append(f"{entry['path']} is not owned by root")
        if entry["mode"] & 0o022:
            problems.append(f"{entry['path']} is group- or other-writable")
    return problems


def executor_semantics(message: Mapping[str, Any]) -> list[str]:
    """Cross-field executor-v2 rules JSON Schema cannot express."""
    problems = []
    ident = message.get("identity") or message.get("echoed_identity") or {}
    unit = ident.get("unit")
    lane = (ident.get("lane") or {}).get("lane_id")
    gen = ident.get("generation")
    if unit is not None and lane is not None and gen is not None and unit != f"flightctl-{lane}-g{gen}.service":
        problems.append("unit name must be flightctl-<lane_id>-g<generation>.service (assigned before reserve)")
    if "deadlines" in message:
        kinds = [d["kind"] for d in message["deadlines"]]
        if len(kinds) != len(set(kinds)):
            problems.append("duplicate deadline kind")
    return problems
