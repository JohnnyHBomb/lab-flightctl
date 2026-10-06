"""OccupancyProbe real twin (A3): who is on a lane's cards now, from real nvidia-smi output over a CommandRunner.

Produces gpu-probe.schema.json#/$defs/occupancy_observation exactly as the contract oracle occupancy_from_capture
does. Noise is decided by identity (owner uid of /proc/<pid>, argv[0] of /proc/<pid>/cmdline, the per-card context
type from `nvidia-smi -q -d PIDS`), never by size. Anything that fails, times out or cannot be parsed is unknown,
never empty. Every process is noise or external: attribution to a lease is the executor's job.
"""

import math as _math
import re as _re
import time as _time
from collections.abc import Mapping as _Mapping
from datetime import timezone as _timezone

GPU_QUERY = ("--query-gpu=uuid,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw,enforced.power.limit,"
             "clocks_event_reasons.hw_thermal_slowdown,clocks_event_reasons.sw_thermal_slowdown,ecc.errors.uncorrected.volatile.total")
APPS_QUERY = "--query-compute-apps=gpu_uuid,pid,process_name,used_memory"
INVENTORY_QUERY = "--query-gpu=index,uuid,pci.bus_id,name,memory.total,driver_version"
_FORMAT = "--format=csv,noheader,nounits"
_SHORT = _re.compile(r"[a-z][a-z0-9._-]{0,63}")
_UUID = _re.compile(r"GPU-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_PCI = _re.compile(r"[0-9A-Fa-f]{8}:[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}\.[0-7]")
_DIGITS = _re.compile(r"[0-9]+")
_DYNAMIC_USER_UIDS = range(61184, 65520)  # systemd DynamicUser range: never a noise identity
_BOUNDS = {"memory_used_mib": (True, 0, None), "memory_total_mib": (True, 1, None), "utilization_pct": (True, 0, 100),  # gpu_sample
           "temperature_c": (True, 0, 130), "power_draw_w": (False, 0, None), "power_limit_w": (False, 0, None),
           "ecc_uncorrected": (True, 0, None)}


class _Unknown(Exception):
    """Ends a call with status unknown; the message is the reason."""


def _require(ok: bool, exc: type, message: str) -> None:
    if not ok:
        raise exc(message)


def _short(value, name: str) -> None:
    _require(isinstance(value, str), TypeError, f"{name} must be a str")
    _require(bool(_SHORT.fullmatch(value)), ValueError, f"{name} must match ^[a-z][a-z0-9._-]{{0,63}}$")


def _int(value, name: str, low: int) -> None:
    _require(isinstance(value, int) and not isinstance(value, bool), TypeError, f"{name} must be an int")
    _require(value >= low, ValueError, f"{name} must be >= {low}")


def _num(text: str):
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


def _parse_gpus(gpus_csv: str, lane: list) -> list:
    """The oracle's gpu row parsing: one sample per lane card, else _Unknown."""
    gpus = []
    for line in gpus_csv.strip().splitlines():
        cells = [c.strip() for c in line.split(",")]
        _require(len(cells) == 10, _Unknown, "unparsable gpu row")
        if cells[0] not in lane:
            continue
        used, total = _num(cells[1]), _num(cells[2])
        _require(isinstance(used, int) and isinstance(total, int), _Unknown, f"memory.used unknown on {cells[0]}")
        slow = None if cells[7].startswith("[") else (cells[7] == "Active" or cells[8] == "Active")
        sample = {"uuid": cells[0], "memory_used_mib": used, "memory_total_mib": total, "utilization_pct": _num(cells[3]),
                  "temperature_c": _num(cells[4]), "power_draw_w": _num(cells[5]), "power_limit_w": _num(cells[6]),
                  "thermal_slowdown": slow, "ecc_uncorrected": _num(cells[9])}
        for field, (integer, low, high) in _BOUNDS.items():  # Amendment 5: values nvidia-smi does not print are unknown
            v = sample[field]
            _require(v is None or ((not integer or isinstance(v, int)) and _math.isfinite(v) and v >= low and (high is None or v <= high)),
                     _Unknown, f"out-of-range {field}={v} on {cells[0]}")
        gpus.append(sample)
    _require(sorted(g["uuid"] for g in gpus) == sorted(lane), _Unknown, "a lane card is missing from nvidia-smi output")
    return gpus


def _digits_int(text: str):
    if not _DIGITS.fullmatch(text):
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _parse_inventory(inventory_csv: str) -> list:
    devices, uuids, buses = [], set(), set()
    for line in inventory_csv.splitlines():
        if not line.strip():
            continue
        cells = [cell.strip() for cell in line.split(",")]
        _require(len(cells) == 6, _Unknown, f"unparsable inventory row: expected 6 cells, got {len(cells)}")
        index, uuid, bus, name, memory, driver = cells
        index_value, memory_value, card = _digits_int(index), _digits_int(memory), f"for card index {index[:16]!r}"
        _require(index_value is not None, _Unknown, f"invalid inventory index {card}")
        _require(bool(_UUID.fullmatch(uuid)), _Unknown, f"invalid inventory uuid {card}")
        _require(bool(_PCI.fullmatch(bus)), _Unknown, f"invalid inventory pci bus id {card}")
        _require(bool(name), _Unknown, f"empty inventory name {card}")
        _require(memory_value is not None and memory_value >= 1, _Unknown, f"invalid inventory memory total {card}")
        _require(bool(driver), _Unknown, f"empty inventory driver version {card}")
        parts = bus.split(":")
        normalized = f"{parts[0][-4:].lower()}:{parts[1].lower()}:{parts[2].lower()}"
        _require(uuid not in uuids, _Unknown, "duplicate uuid in the inventory query")
        _require(normalized not in buses, _Unknown, "duplicate pci bus id in the inventory query")
        uuids.add(uuid)
        buses.add(normalized)
        devices.append({"index": index_value, "uuid": uuid, "pci_bus_id": normalized, "name": name,
                        "memory_total_mib": memory_value, "driver_version": driver, "numa_node": None, "device_minor": None})
    _require(bool(devices), _Unknown, "the inventory query listed no card")
    return devices


def _parse_numa(text: str | None):
    if text is None:
        return None
    node = _digits_int(text.strip())
    return node if node is not None and node <= 1023 else None  # -1 and ids past MAX_NUMNODES (1024) are null


def _parse_minor(text: str | None):
    if text is None:
        return None
    _, separator, value = text.partition(":")
    minor = _digits_int(value.strip())
    return minor if separator and minor is not None and minor <= 255 else None


def _parse_procs(procs_csv: str, lane: list, gpus: list) -> list:
    """The oracle's process row parsing: lane rows (uuid, pid, name, mem), else _Unknown."""
    rows = []
    for line in procs_csv.strip().splitlines():
        head = line.split(", ", 2)
        _require(len(head) >= 3, _Unknown, "unparsable process row")
        uuid, pid_text = head[0].strip(), head[1].strip()
        name, _, mem_text = head[2].rpartition(", ")
        _require(bool(_DIGITS.fullmatch(pid_text)) and int(pid_text) >= 1 and bool(name), _Unknown, "unparsable process row")
        if uuid in lane:
            mem = _num(mem_text)
            _require(not isinstance(mem, int) or mem >= 0, _Unknown, f"out-of-range used_memory {mem} on {uuid}")
            rows.append((uuid, int(pid_text), name, mem if isinstance(mem, int) else None))
    for g in gpus:  # the two queries are separate samples: inconsistent numbers are unknown, never empty
        listed = sum(mem or 0 for uuid, _, _, mem in rows if uuid == g["uuid"])
        _require(listed <= g["memory_used_mib"], _Unknown, f"inconsistent sample on {g['uuid']}: process memory exceeds memory.used")
    return rows


def _context_types(text: str) -> dict:
    """pid -> context type on this card from `nvidia-smi -q -d PIDS -i <uuid>`; a type other than C, G, C+G is None."""
    types, pid = {}, None
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if key == "Process ID":
            pid = int(value) if _DIGITS.fullmatch(value) else None
        elif key == "Type" and pid is not None:
            types[pid], pid = (value if value in ("C", "G", "C+G") else None), None
    return types


def _noise_ok(row: dict, allowlist: list) -> bool:
    uid = row["uid"]
    if row["context_type"] not in ("G", "C+G") or not isinstance(uid, int) or uid == 0 or uid in _DYNAMIC_USER_UIDS:
        return False
    argv0 = row["argv0"]  # Amendment 5: equality, or the entry then a space (a command line rewritten into one string)
    return argv0 is not None and any(e["argv0"] and (argv0 == e["argv0"] or argv0.startswith(e["argv0"] + " ")) and e["uid"] == uid
                                     for e in allowlist)


class NvidiaOccupancyProbe:
    """OccupancyProbe over a CommandRunner: nvidia-smi queries, per-card context types and /proc identities."""

    def __init__(self, runner, *, clock, nvidia_smi="nvidia-smi", local_host_id=None):
        _require(callable(getattr(runner, "run", None)), TypeError, "runner must have a callable run")
        _require(callable(getattr(clock, "utc", None)), TypeError, "clock must have a callable utc")
        _require(isinstance(nvidia_smi, str), TypeError, "nvidia_smi must be a str")
        _require(bool(nvidia_smi) and "\0" not in nvidia_smi, ValueError, "nvidia_smi must be non-empty without NUL")
        if local_host_id is not None:
            _short(local_host_id, "local_host_id")
        self._runner, self._clock, self._nvidia_smi, self._local = runner, clock, nvidia_smi, local_host_id

    def occupancy(self, host_id, lane_id, uuids, *, noise_allowlist, noise_cap_mib, lane_noise_mib, timeout_s):
        _short(host_id, "host_id")
        _short(lane_id, "lane_id")
        _require(isinstance(uuids, (list, tuple)) and all(isinstance(u, str) for u in uuids), TypeError, "uuids must be a list or tuple of str")
        _require(bool(uuids) and all(_UUID.fullmatch(u) for u in uuids) and len(set(uuids)) == len(uuids), ValueError,
                 "uuids must be one or more distinct GPU UUIDs")
        _require(isinstance(noise_allowlist, (list, tuple)) and all(isinstance(e, _Mapping) for e in noise_allowlist), TypeError,
                 "noise_allowlist must be a list or tuple of mappings")
        for entry in noise_allowlist:
            _require(set(entry) == {"argv0", "uid"}, ValueError, "a noise_allowlist entry has exactly the keys argv0 and uid")
            _require(isinstance(entry["argv0"], str), TypeError, "argv0 must be a str")
            _require(1 <= len(entry["argv0"]) <= 4096, ValueError, "argv0 must be 1-4096 characters")
            _int(entry["uid"], "uid", 1)
        _require(len({(e["argv0"], e["uid"]) for e in noise_allowlist}) == len(noise_allowlist), ValueError, "noise_allowlist entries must be distinct")
        _int(noise_cap_mib, "noise_cap_mib", 0)
        _int(lane_noise_mib, "lane_noise_mib", 0)
        _require(isinstance(timeout_s, (int, float)) and not isinstance(timeout_s, bool), TypeError, "timeout_s must be an int or float")
        _require(_math.isfinite(timeout_s) and timeout_s > 0, ValueError, "timeout_s must be finite and > 0")
        deadline = _time.monotonic() + timeout_s
        lane = list(uuids)
        allow = [{"argv0": e["argv0"], "uid": e["uid"]} for e in noise_allowlist]
        thresholds = {"lane_noise_mib": lane_noise_mib, "noise_cap_mib": noise_cap_mib, "noise_allowlist": allow}
        obs = {"kind": "gpu-occupancy", "host_id": host_id, "lane_id": lane_id,
               "observed_at": self._clock.utc().astimezone(_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "status": "unknown", "reason": None, "expected_uuids": list(lane), "gpus": [], "processes": [], "tenants": [],
               "noise": [], "lane_memory_used_mib": None, "thresholds": thresholds, "unexplained_mib": None, "empty": False}
        try:
            return obs | self._observe(host_id if host_id != self._local else None, lane, allow, noise_cap_mib, lane_noise_mib, deadline)
        except _Unknown as exc:
            return obs | {"reason": (str(exc) or "unknown")[:1024]}

    def _run(self, step: str, argv: list, host, deadline: float, strict: bool = True):
        left = deadline - _time.monotonic()
        _require(left > 0, _Unknown, f"timeout: no time left to run the {step}")
        result = self._runner.run(argv, timeout_s=left, host_id=host)
        error = result.get("error")
        timed_out = result.get("timed_out") or (isinstance(error, _Mapping) and error.get("code") == "timeout")
        _require(not timed_out, _Unknown, f"timeout: the {step} did not finish in time")
        failed = result.get("returncode") != 0 or error is not None
        if strict and failed:  # operator message: the step, its exit code, then stderr or the runner's error message
            detail = (result.get("stderr") or "").strip() or (error.get("message") if isinstance(error, _Mapping) else "")
            raise _Unknown(f"the {step} failed (exit code {result.get('returncode')}){': ' + detail if detail else ''}"[:1024])
        return None if failed else result.get("stdout") or ""

    def _observe(self, host, lane, allow, noise_cap_mib, lane_noise_mib, deadline) -> dict:
        gpus = _parse_gpus(self._run("gpu query (a)", [self._nvidia_smi, GPU_QUERY, _FORMAT], host, deadline), lane)
        rows = _parse_procs(self._run("process query (b)", [self._nvidia_smi, APPS_QUERY, _FORMAT], host, deadline), lane, gpus)
        types = {uuid: _context_types(self._run(f"PIDS query (c) of {uuid}", [self._nvidia_smi, "-q", "-d", "PIDS", "-i", uuid],
                                                host, deadline)) for uuid in lane}
        identities = {}
        for pid in sorted({pid for _, pid, _, _ in rows}):
            uid = self._run(f"owner read (d) of pid {pid}", ["stat", "-c", "%u", f"/proc/{pid}"], host, deadline, strict=False)
            uid = uid.strip() if uid is not None else ""
            cmdline = self._run(f"command line read (d) of pid {pid}", ["cat", f"/proc/{pid}/cmdline"], host, deadline, strict=False)
            argv0 = (cmdline or "").split("\0", 1)[0]
            identities[pid] = (int(uid) if _DIGITS.fullmatch(uid) else None, argv0 if 0 < len(argv0) <= 4096 else None)
        processes, tenants, noise = [], [], []
        for uuid, pid, name, mem in rows:
            row = {"gpu_uuid": uuid, "pid": pid, "process_name": name[:4096], "used_memory_mib": mem, "attribution": "external",
                   "uid": identities[pid][0], "argv0": identities[pid][1], "context_type": types[uuid].get(pid)}
            if mem is not None and _noise_ok(row, allow):
                row["attribution"] = "noise"
            processes.append(row)
            (noise if row["attribution"] == "noise" else tenants).append(row)
        lane_used = sum(g["memory_used_mib"] for g in gpus)
        unexplained = lane_used - sum(p["used_memory_mib"] for p in noise)
        return {"status": "ok", "reason": None, "gpus": gpus, "processes": processes, "tenants": tenants, "noise": noise,
                "lane_memory_used_mib": lane_used, "unexplained_mib": unexplained,
                "empty": not tenants and unexplained < lane_noise_mib and sum(p["used_memory_mib"] for p in noise) <= noise_cap_mib}


class NvidiaInventoryProbe:
    """InventoryProbe over the same bounded CommandRunner seam as the occupancy real twin."""

    __init__ = NvidiaOccupancyProbe.__init__  # same arguments, same checks, same errors
    _run = NvidiaOccupancyProbe._run  # the shared deadline

    def inventory(self, host_id, *, timeout_s):
        _short(host_id, "host_id")
        _require(isinstance(timeout_s, (int, float)) and not isinstance(timeout_s, bool), TypeError,
                 "timeout_s must be an int or float")
        _require(_math.isfinite(timeout_s) and timeout_s > 0, ValueError, "timeout_s must be finite and > 0")
        deadline = _time.monotonic() + timeout_s
        obs = {"kind": "gpu-inventory", "host_id": host_id,
               "observed_at": self._clock.utc().astimezone(_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "status": "unknown", "reason": None, "vendor": "unknown", "devices": []}
        try:
            return obs | self._inventory(host_id if host_id != self._local else None, deadline)
        except _Unknown as exc:
            return obs | {"reason": (str(exc) or "unknown")[:1024]}

    def _inventory(self, host, deadline):
        devices = _parse_inventory(self._run("inventory query (a)", [self._nvidia_smi, INVENTORY_QUERY, _FORMAT], host, deadline))
        for device in devices:
            bus = device["pci_bus_id"]
            numa = self._run("numa node read (b)", ["cat", f"/sys/bus/pci/devices/{bus}/numa_node"], host, deadline, strict=False)
            minor = self._run("device minor read (c)", ["grep", "Device Minor", f"/proc/driver/nvidia/gpus/{bus}/information"], host, deadline, strict=False)
            device["numa_node"] = _parse_numa(numa)
            device["device_minor"] = _parse_minor(minor)
        return {"status": "ok", "reason": None, "vendor": "nvidia", "devices": devices}
