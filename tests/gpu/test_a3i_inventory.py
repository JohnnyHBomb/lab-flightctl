import json as _json
import subprocess as _subprocess
import time as _time
from datetime import datetime as _datetime, timezone as _timezone

import pytest

from tests.contracts_v2.validation import assert_valid

from flightctl.commands import LocalCommandRunner
from flightctl.clock import RealClock
from flightctl.gpu import INVENTORY_QUERY, NvidiaInventoryProbe
from tests.fakes.gpu_replay import CAPTURE_DIR, ReplayGPUProbe


class _Clock:
    def utc(self):
        return _datetime(2026, 10, 4, tzinfo=_timezone.utc)


class _Runner:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def run(self, argv, *, timeout_s, host_id=None):
        key = tuple(argv)
        self.calls.append((list(argv), timeout_s, host_id))
        answer = self.answers.get(key, {"returncode": 1, "stderr": "unscripted"})
        return {"argv": list(argv), "host_id": host_id, "returncode": answer.get("returncode", 0),
                "stdout": answer.get("stdout", ""), "stderr": answer.get("stderr", ""),
                "timed_out": answer.get("timed_out", False), "duration_s": 0.0, "error": answer.get("error")}


def _inventory_rows(capture):
    records = [_json.loads(line) for line in (CAPTURE_DIR / f"{capture}.jsonl").read_text().splitlines()]
    rows = [cell.strip() for cell in records[0]["stdout"].splitlines()]
    details = {}
    for record in records[1:]:
        argv = record["argv"]
        if argv[0] == "cat":
            details.setdefault(argv[1].split("/devices/", 1)[1].rsplit("/", 1)[0], {})["numa_node"] = record["stdout"].strip()
        elif argv[0] == "grep":
            details.setdefault(argv[2].split("/gpus/", 1)[1].rsplit("/", 1)[0], {})["device_minor"] = record["stdout"].split(":", 1)[-1].strip()
    found = []
    for row in rows:
        cells = [cell.strip() for cell in row.split(",")]
        parts = cells[2].split(":")
        bus = f"{parts[0][-4:].lower()}:{parts[1].lower()}:{parts[2].lower()}"
        found.append((int(cells[0]), cells[1], bus, cells[3], int(cells[4]), cells[5],
                     None if details[bus]["numa_node"] == "-1" else int(details[bus]["numa_node"]),
                     int(details[bus]["device_minor"])))
    return found


def test_golden_captures_all_card_models():
    docs = ReplayGPUProbe().golden_captures()
    assert {d["card_model"].split(" (")[0] for d in docs} == {"NVIDIA TITAN RTX", "Quadro RTX 8000", "Tesla T4"} and len(docs) == 4
    for path in sorted(CAPTURE_DIR.glob("*.expected.json")):
        document = _json.loads(path.read_text())
        probe = ReplayGPUProbe(path.name.removesuffix(".expected.json"))
        observation = probe.inventory(document["host_id"], timeout_s=10)
        assert_valid(observation, "gpu-probe", "inventory_observation")
        assert observation["status"] == "ok"
        expected = _inventory_rows(document["capture"].removesuffix(".jsonl"))
        assert [tuple(device[key] for key in ("index", "uuid", "pci_bus_id", "name", "memory_total_mib", "driver_version", "numa_node", "device_minor"))
                for device in observation["devices"]] == expected
        assert ReplayGPUProbe().parse_capture(document) == document["expected"]


def _scripted_probe(row, details):
    answers = {("nvidia-smi", INVENTORY_QUERY, "--format=csv,noheader,nounits"): {"stdout": row}}
    for bus, numa, minor in details:
        answers[("cat", f"/sys/bus/pci/devices/{bus}/numa_node")] = numa if isinstance(numa, dict) else {"stdout": numa}
        answers[("grep", "Device Minor", f"/proc/driver/nvidia/gpus/{bus}/information")] = minor
    runner = _Runner(answers)
    return NvidiaInventoryProbe(runner, clock=_Clock(), local_host_id="host-1"), runner


def test_inventory_normalises_bus_and_reads_numa_and_minor():
    uuid1 = "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    uuid2 = "GPU-11111111-2222-3333-4444-555555555555"
    uuid3 = "GPU-99999999-8888-7777-6666-555555555555"
    row = (f"0, {uuid1}, 00000000:8D:00.0, Card One, 100, 1.2\n"
           f"1, {uuid2}, 00000001:AB:01.1, Card Two, 200, 1.2\n"
           f"2, {uuid3}, 00000002:CD:02.0, Card Three, 300, 1.2\n")
    probe, runner = _scripted_probe(row, (
        ("0000:8d:00.0", "1\n", {"stdout": "Device Minor: \t 3\n"}),
        ("0001:ab:01.1", "-1\n", {"stdout": "Device Minor: \t 4\n"}),
        ("0002:cd:02.0", {"returncode": 1, "stdout": "2\n"}, {"returncode": 1}),
    ))
    observation = probe.inventory("host-1", timeout_s=10)
    assert_valid(observation, "gpu-probe", "inventory_observation")
    assert observation["status"] == "ok"
    assert [(d["pci_bus_id"], d["numa_node"], d["device_minor"]) for d in observation["devices"]] == [
        ("0000:8d:00.0", 1, 3), ("0001:ab:01.1", None, 4), ("0002:cd:02.0", None, None)]
    assert [call[0] for call in runner.calls] == [
        ["nvidia-smi", INVENTORY_QUERY, "--format=csv,noheader,nounits"],
        ["cat", "/sys/bus/pci/devices/0000:8d:00.0/numa_node"],
        ["grep", "Device Minor", "/proc/driver/nvidia/gpus/0000:8d:00.0/information"],
        ["cat", "/sys/bus/pci/devices/0001:ab:01.1/numa_node"],
        ["grep", "Device Minor", "/proc/driver/nvidia/gpus/0001:ab:01.1/information"],
        ["cat", "/sys/bus/pci/devices/0002:cd:02.0/numa_node"],
        ["grep", "Device Minor", "/proc/driver/nvidia/gpus/0002:cd:02.0/information"],
    ]


def test_inventory_failures_are_unknown():
    uuid = "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    valid = f"0, {uuid}, 00000000:8D:00.0, Card, 100, 1.2\n"
    cases = [
        {"returncode": 9},
        {"returncode": None, "timed_out": True, "error": {"code": "timeout", "message": "late"}},
        {"stdout": "0, GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee, 00000000:8D:00.0, Card, 100\n"},
        {"stdout": "0, GPU-not-a-uuid, 00000000:8D:00.0, Card, 100, 1.2\n"},
        {"stdout": valid + valid.replace(uuid, "GPU-11111111-2222-3333-4444-555555555555")},
        {"stdout": ""},
    ]
    for answer in cases:
        runner = _Runner({("nvidia-smi", INVENTORY_QUERY, "--format=csv,noheader,nounits"): answer})
        observation = NvidiaInventoryProbe(runner, clock=_Clock(), local_host_id="host-1").inventory("host-1", timeout_s=1)
        assert observation["status"] == "unknown"
        assert observation["vendor"] == "unknown" and observation["devices"] == [] and observation["reason"]
    assert "timeout" in NvidiaInventoryProbe(
        _Runner({("nvidia-smi", INVENTORY_QUERY, "--format=csv,noheader,nounits"): cases[1]}),
        clock=_Clock(), local_host_id="host-1").inventory("host-1", timeout_s=1)["reason"]


@pytest.mark.realtime
def test_inventory_real_process_timeout(tmp_path):
    pid_path = tmp_path / "pid"
    stub = tmp_path / "nvidia-smi-stub"
    stub.write_text(f"#!/bin/sh\nif [ \"$1\" = \"{INVENTORY_QUERY}\" ]; then\n  echo $$ > '{pid_path}'\n  exec sleep 30\nfi\nexit 1\n")
    stub.chmod(0o755)
    probe = NvidiaInventoryProbe(LocalCommandRunner(), clock=RealClock(), nvidia_smi=str(stub), local_host_id="host-1")
    started = _time.monotonic()
    observation = probe.inventory("host-1", timeout_s=1)
    elapsed = _time.monotonic() - started
    pid = int(pid_path.read_text())
    process = _subprocess.run(["kill", "-0", str(pid)], check=False)
    assert elapsed < 4 and process.returncode != 0
    assert observation["status"] == "unknown" and "timeout" in observation["reason"]
