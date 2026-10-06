"""A3i named acceptance tests: the InventoryProbe real twin (flightctl.gpu.NvidiaInventoryProbe) and the replay-backed GPU
probe fake (tests.fakes.gpu_replay.ReplayGPUProbe)."""

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.contracts_v2.validation import assert_valid, load, schema_path

CAPTURES = Path(__file__).resolve().parents[1] / "fakes" / "captures" / "gpu"
FMT = "--format=csv,noheader,nounits"
ROW = "{0}, GPU-00000000-0000-0000-0000-00000000000{0}, {1}, NVIDIA TITAN RTX, 24576, 610.57.04\n"


class _Clock:
    def utc(self):
        return datetime(2026, 10, 4, tzinfo=timezone.utc)


class _Scripted:
    """Answers each argv from a dict {argv tuple: (returncode, stdout[, delay_s])}; anything unscripted exits 1; returncode None
    times out. A delay never outlasts timeout_s."""

    def __init__(self, answers):
        self.answers, self.calls, self.timeouts = answers, [], []

    def run(self, argv, *, timeout_s, stdin=None, host_id=None):
        self.calls.append((tuple(argv), host_id))
        self.timeouts.append(timeout_s)
        rc, out, delay = (*self.answers.get(tuple(argv), (1, "")), 0)[:3]
        time.sleep(min(delay, timeout_s))
        return {"argv": list(argv), "host_id": host_id, "returncode": rc, "stdout": out, "stderr": "", "timed_out": rc is None,
                "duration_s": 0.0, "error": None}


def _smi():
    from flightctl.gpu import INVENTORY_QUERY
    return ("nvidia-smi", INVENTORY_QUERY, FMT)


def _numa(bus):
    return ("cat", f"/sys/bus/pci/devices/{bus}/numa_node")


def _minor(bus):
    return ("grep", "Device Minor", f"/proc/driver/nvidia/gpus/{bus}/information")


def _inventory(answers, host_id="host-1", timeout_s=10):
    from flightctl.gpu import NvidiaInventoryProbe
    runner = _Scripted(answers)
    obs = NvidiaInventoryProbe(runner, clock=_Clock(), local_host_id="host-1").inventory(host_id, timeout_s=timeout_s)
    assert_valid(obs, "gpu-probe", "inventory_observation")
    return obs, runner


def _recorded(name):
    """The cards of a golden capture as its own commands printed them: the inventory rows, then each card's numa_node and minor."""
    out = {tuple(r["argv"]): r["stdout"] for r in map(json.loads, (CAPTURES / f"{name}.jsonl").read_text().splitlines())}
    cards = []
    for row in next(iter(out.values())).splitlines():  # the capture starts with the inventory query
        index, uuid, raw, model, mib, driver = row.split(", ")
        bus = raw[4:].lower()
        numa = int(out[_numa(bus)])
        cards.append({"index": int(index), "uuid": uuid, "pci_bus_id": bus, "name": model, "memory_total_mib": int(mib),
                      "driver_version": driver, "numa_node": numa if numa >= 0 else None,
                      "device_minor": int(out[_minor(bus)].split(":")[1])})
    return cards


def test_golden_captures_all_card_models() -> None:
    from tests.fakes.gpu_replay import ReplayGPUProbe
    names = [f.removesuffix(".expected.json") for f in sorted(p.name for p in CAPTURES.glob("*.expected.json"))]  # by file name
    for name in names:
        doc = json.loads((CAPTURES / f"{name}.expected.json").read_text())
        obs = ReplayGPUProbe(name).inventory(doc["host_id"], timeout_s=10)
        assert_valid(obs, "gpu-probe", "inventory_observation")
        assert (obs["status"], obs["vendor"], obs["host_id"], obs["reason"]) == ("ok", "nvidia", doc["host_id"], None), name
        assert obs["devices"] == _recorded(name), name
    default, docs = ReplayGPUProbe(), ReplayGPUProbe().golden_captures()
    titan = next(d for d in docs if d["capture"] == "titan-rtx.jsonl")  # the default capture
    assert (default.test_host, default.test_lane, default.test_uuids, default.test_noise_allowlist) == (
        titan["host_id"], titan["lane_id"], titan["lane_uuids"], [])
    assert [d["capture"] for d in docs] == [f"{n}.jsonl" for n in names]
    assert {d["card_model"].split(" (")[0] for d in docs} == {"NVIDIA TITAN RTX", "Quadro RTX 8000", "Tesla T4"}
    for doc in docs:
        assert ReplayGPUProbe().parse_capture(doc) == doc["expected"], doc["capture"]
    docs[0]["lane_uuids"].clear()
    assert ReplayGPUProbe().golden_captures()[0]["lane_uuids"]  # a new list of new documents on every call
    probe = ReplayGPUProbe()  # the inventory port under every scripted fault, then back to replay
    for fault in ("timeout", "exit-9", "unparsable", "na-only"):
        probe.script_next(fault)
        obs = probe.inventory(probe.test_host, timeout_s=1)
        assert obs["status"] == "unknown" and obs["vendor"] == "unknown" and obs["devices"] == [] and obs["reason"], fault
        assert ("timeout" in obs["reason"]) == (fault == "timeout"), obs["reason"]
        assert probe.inventory(probe.test_host, timeout_s=1)["status"] == "ok", fault
    with pytest.raises(ValueError):
        probe.script_next("no-such-fault")


def test_inventory_normalises_bus_and_reads_numa_and_minor() -> None:
    from flightctl.gpu import INVENTORY_QUERY
    assert load(schema_path("gpu-probe"))["x-real-commands"]["inventory"] == f"nvidia-smi {INVENTORY_QUERY} {FMT}"
    raw = ["00000000:8D:00.0", "00000000:99:00.0", "00000000:0A:00.0", "00009D3B:AB:0F.7"]
    buses = ["0000:8d:00.0", "0000:99:00.0", "0000:0a:00.0", "9d3b:ab:0f.7"]
    answers = {_smi(): (0, "".join(ROW.format(i, bus) for i, bus in enumerate(raw))),
               _numa(buses[0]): (0, "1\n"), _minor(buses[0]): (0, "Device Minor: \t 3\n"),
               _numa(buses[1]): (0, "-1\n"),  # no NUMA on this host; its grep is unscripted and exits 1
               _minor(buses[2]): (0, "Device Minor: \t 0\n"),  # its cat is unscripted and exits 1
               _numa(buses[3]): (0, "x\n"), _minor(buses[3]): (0, "Device Minor: \t 256\n")}  # neither is a usable number
    obs, runner = _inventory(answers)
    assert (obs["status"], obs["vendor"], obs["reason"], obs["host_id"], obs["observed_at"]) == ("ok", "nvidia", None, "host-1", "2026-10-04T00:00:00Z")
    assert [(d["index"], d["pci_bus_id"], d["numa_node"], d["device_minor"]) for d in obs["devices"]] == [
        (0, buses[0], 1, 3), (1, buses[1], None, None), (2, buses[2], None, 0), (3, buses[3], None, None)]
    assert obs["devices"][2] == {"index": 2, "uuid": "GPU-00000000-0000-0000-0000-000000000002", "pci_bus_id": "0000:0a:00.0",
                                 "name": "NVIDIA TITAN RTX", "memory_total_mib": 24576, "driver_version": "610.57.04",
                                 "numa_node": None, "device_minor": 0}
    assert [argv for argv, _ in runner.calls] == [_smi()] + [argv for bus in buses for argv in (_numa(bus), _minor(bus))]
    assert {host for _, host in runner.calls} == {None}  # host-1 is the local host
    assert {host for _, host in _inventory(answers, host_id="host-2")[1].calls} == {"host-2"}


def test_inventory_failures_are_unknown() -> None:
    row, bus = ROW.format(0, "00000000:8D:00.0"), "0000:8d:00.0"
    cases = {"exit 9": {_smi(): (9, "")}, "timeout": {_smi(): (None, "")}, "empty output": {_smi(): (0, "")},
             "five cells": {_smi(): (0, row.rsplit(",", 1)[0] + "\n")}, "bad uuid": {_smi(): (0, row.replace("GPU-0", "GPU-x"))},
             "duplicate bus id": {_smi(): (0, row + ROW.format(1, "00000000:8D:00.0"))},
             "duplicate uuid": {_smi(): (0, row + ROW.format(0, "00000000:99:00.0"))},
             "bad bus id": {_smi(): (0, row.replace(":00.0", ":00.8"))}, "zero memory": {_smi(): (0, row.replace("24576", "0"))},
             "empty name": {_smi(): (0, row.replace("NVIDIA TITAN RTX", ""))},
             "numa timeout": {_smi(): (0, row), _numa(bus): (None, "")},
             "minor timeout": {_smi(): (0, row), _numa(bus): (0, "0\n"), _minor(bus): (None, "")}}
    for label, answers in cases.items():
        obs, _ = _inventory(answers)
        assert obs["status"] == "unknown" and obs["vendor"] == "unknown" and obs["devices"] == [] and obs["reason"], label
        assert ("timeout" in obs["reason"]) == ("timeout" in label), obs["reason"]
        assert label != "exit 9" or "exit code 9" in obs["reason"], obs["reason"]  # the lowest-level reason is kept
    slow = {_smi(): (0, row, 0.6), _numa(bus): (0, "0\n", 0.6)}  # each command alone fits the 1 s budget, the first two together do not
    obs, runner = _inventory(slow, timeout_s=1)
    assert obs["status"] == "unknown" and "timeout" in obs["reason"] and len(runner.calls) == 2, (obs, runner.calls)
    assert runner.timeouts[0] <= 1 and runner.timeouts[1] <= 0.5, runner.timeouts  # each command gets the time left


@pytest.mark.realtime
def test_inventory_real_process_timeout(tmp_path) -> None:
    from flightctl.clock import RealClock
    from flightctl.commands import LocalCommandRunner
    from flightctl.gpu import INVENTORY_QUERY, NvidiaInventoryProbe
    stub, pidfile = tmp_path / "nvidia-smi", tmp_path / "stub.pid"
    stub.write_text(f'#!/bin/sh\n[ "$1" = "{INVENTORY_QUERY}" ] && echo $$ > "{pidfile}" && exec sleep 30\nexit 1\n')
    stub.chmod(0o755)
    probe = NvidiaInventoryProbe(LocalCommandRunner(), clock=RealClock(), nvidia_smi=str(stub), local_host_id="host-1")
    start = time.monotonic()
    obs = probe.inventory("host-1", timeout_s=1)
    assert time.monotonic() - start < 4
    assert_valid(obs, "gpu-probe", "inventory_observation")
    assert obs["status"] == "unknown" and obs["vendor"] == "unknown" and obs["devices"] == [] and "timeout" in obs["reason"], obs
    assert subprocess.run(["kill", "-0", pidfile.read_text().strip()], capture_output=True).returncode != 0  # the stub is gone
