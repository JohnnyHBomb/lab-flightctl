"""Replay-backed GPU probe fake: the production probes over recorded command results."""

import copy as _copy
import json as _json
from datetime import datetime as _datetime, timezone as _timezone
from pathlib import Path as _Path

from flightctl.commands import _error, _result
from flightctl.gpu import GPU_QUERY as _GPU_QUERY
from flightctl.gpu import INVENTORY_QUERY as _INVENTORY_QUERY
from flightctl.gpu import NvidiaInventoryProbe as _NvidiaInventoryProbe
from flightctl.gpu import NvidiaOccupancyProbe as _NvidiaOccupancyProbe
from tests.fakes.replay import ReplayCommandRunner as _ReplayCommandRunner

CAPTURE_DIR = _Path(__file__).resolve().parent / "captures" / "gpu"


class _FixedClock:
    def utc(self):
        return _datetime(2026, 10, 4, tzinfo=_timezone.utc)


class _FaultRunner:
    def __init__(self, capture_path, uuids):
        self._replay = _ReplayCommandRunner(capture_path)
        self._uuids = list(uuids)
        self._fault = None

    def script(self, fault):
        self._fault = fault

    def run(self, argv, *, timeout_s, stdin=None, host_id=None):
        fault, self._fault = self._fault, None
        if fault is None:
            return self._replay.run(argv, timeout_s=timeout_s, stdin=stdin, host_id=host_id)
        if fault == "timeout":
            return _result(argv, host_id, timed_out=True, error=_error("timeout", "synthetic timeout", "runner"))
        if fault == "exit-9":
            return _result(argv, host_id, returncode=9)
        if fault == "unparsable":
            return _result(argv, host_id, returncode=0, stdout="garbage\n")
        if fault == "na-only":
            if len(argv) > 1 and argv[1] == _GPU_QUERY:
                stdout = "\n".join(f"{uuid}, " + ", ".join(["[N/A]"] * 9) for uuid in self._uuids) + "\n"
            elif len(argv) > 1 and argv[1] == _INVENTORY_QUERY:
                stdout = "\n".join(f"{index}, " + ", ".join(["[N/A]"] * 5) for index in range(len(self._uuids))) + "\n"
            else:
                stdout = "garbage\n"
            return _result(argv, host_id, returncode=0, stdout=stdout)
        raise ValueError(fault)


class ReplayGPUProbe:
    def __init__(self, capture="titan-rtx"):
        expected_path = CAPTURE_DIR / f"{capture}.expected.json"
        document = _json.loads(expected_path.read_text(encoding="utf-8"))
        runner = _FaultRunner(CAPTURE_DIR / f"{capture}.jsonl", document["lane_uuids"])
        clock = _FixedClock()
        self._runner = runner
        self._occupancy = _NvidiaOccupancyProbe(runner, clock=clock, local_host_id=document["local_host_id"])
        self._inventory = _NvidiaInventoryProbe(runner, clock=clock, local_host_id=document["local_host_id"])
        self.test_host = document["host_id"]
        self.test_lane = document["lane_id"]
        self.test_uuids = _copy.deepcopy(document["lane_uuids"])
        self.test_noise_allowlist = []

    def occupancy(self, host_id, lane_id, uuids, *, noise_allowlist, noise_cap_mib, lane_noise_mib, timeout_s):
        return _copy.deepcopy(self._occupancy.occupancy(host_id, lane_id, uuids, noise_allowlist=noise_allowlist,
                                                       noise_cap_mib=noise_cap_mib, lane_noise_mib=lane_noise_mib,
                                                       timeout_s=timeout_s))

    def inventory(self, host_id, *, timeout_s):
        return _copy.deepcopy(self._inventory.inventory(host_id, timeout_s=timeout_s))

    def script_next(self, fault):
        if fault not in {"timeout", "exit-9", "unparsable", "na-only"}:
            raise ValueError(fault)
        self._runner.script(fault)

    def golden_captures(self):
        return [_json.loads(path.read_text(encoding="utf-8"))
                for path in sorted(CAPTURE_DIR.glob("*.expected.json"), key=lambda item: item.name)]

    def parse_capture(self, capture):
        runner = _ReplayCommandRunner(CAPTURE_DIR / capture["capture"])
        probe = _NvidiaOccupancyProbe(runner, clock=_FixedClock(), local_host_id=capture["local_host_id"])
        result = probe.occupancy(capture["host_id"], capture["lane_id"], list(capture["lane_uuids"]),
                                 noise_allowlist=list(capture["noise_allowlist"]), noise_cap_mib=capture["noise_cap_mib"],
                                 lane_noise_mib=capture["lane_noise_mib"], timeout_s=capture["timeout_s"])
        return _copy.deepcopy(result)
