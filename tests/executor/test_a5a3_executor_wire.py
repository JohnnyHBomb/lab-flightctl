"""A5a3 part 1 named acceptance tests: the executor stdio entry point serves protocol v2 through ExecutorV2 and runs the enforcer one-shot."""

import json
import subprocess
import time
from pathlib import Path

import pytest

from flightctl.clock import RealClock
from tests.executor.wire_site import CARDS, LANE1, LANE2, OTHER, ROOT, Rig, request

V1_RESERVE = json.loads((ROOT / "contracts" / "executor-v1.schema.json").read_text(encoding="utf-8"))["x-examples"]["valid"][0]


@pytest.mark.realtime
def test_stdio_serves_v2_through_executor_v2(tmp_path):
    rig = Rig(tmp_path)
    v2 = Path(f"{rig.state}.v2")
    reserve = rig.reply(request("reserve"))
    assert (reserve["ok"], reserve["observed_state"], reserve["host_boot"]["host_id"], reserve["host_boot"]["boot_id"]) == (
        True, "reserved", "host-1", RealClock().boot_id())
    assert [lane["fence"]["state"] for lane in json.loads(v2.read_text(encoding="utf-8"))["lanes"].values()] == ["reserved"]
    assert not rig.state.exists()  # the v2 fence is in the sibling file only
    beat = rig.reply(request("beat"))  # a later invocation finds the fence in the v2 file
    assert (beat["ok"], beat["observed_state"]) == (True, "reserved")
    other = rig.reply(request("reserve", lane=OTHER))
    assert (other["ok"], other["definite"], other["error"]["code"]) == (False, True, "not_found")
    kept, stop = v2.read_bytes(), request("stop")
    refused = [("denied", "denied", dict(stop, controller_id="controller-b"))]  # a v2 request from another controller
    refused += [("unparsable", "reply_unparsable", dict(stop, schema_version=version)) for version in (3, 2.0, True)]
    for status, code, bad in refused:
        result = rig.call(bad)
        assert (result["status"], result["reply"], result["error"]["code"]) == (status, None, code), result
    assert v2.read_bytes() == kept and rig.smi_calls() == []  # nothing ran and nothing changed
    stopped = rig.reply(request("stop"))
    occupancy = stopped["occupancy"]
    assert (stopped["ok"], stopped["observed_state"], stopped["fences"]) == (True, "free", [])
    assert (occupancy["status"], occupancy["empty"], occupancy["expected_uuids"]) == ("ok", True, [CARDS["lane-gpu1"]])
    assert f"-q -d PIDS -i {CARDS['lane-gpu1']}" in rig.smi_calls()  # the stub ran for the lane's card
    freed = v2.read_bytes()
    v1 = rig.call(V1_RESERVE)
    assert (v1["status"], v1["reply"]["schema_version"], v1["reply"]["ok"]) == ("ok", 1, True), v1
    assert rig.state.exists() and v2.read_bytes() == freed  # the v1 fence is in --state, the v2 file is untouched
    inventory = rig.site / "inventory.json"
    inventory.write_text(inventory.read_text(encoding="utf-8") + " ", encoding="utf-8")  # no longer its sha256 in SHA256SUMS
    changed = rig.call(request("reserve", lane=LANE2))
    assert (changed["status"], changed["reply"], changed["error"]["code"]) == ("failed", None, "transport_failed"), changed
    assert "inventory.json" in changed["error"]["message"] and v2.read_bytes() == freed


@pytest.mark.realtime
def test_enforcer_one_shot_entry_point(tmp_path):
    begun, rig = time.monotonic(), Rig(tmp_path)
    assert rig.reply(request("reserve", {"expiry": 30, "heartbeat-stale": 600, "max-end": 14400}, lane=LANE2, protected=False))["ok"]
    assert rig.reply(request("reserve", {"expiry": 1800, "heartbeat-stale": 1, "max-end": 14400}))["ok"]
    reserved_at = time.monotonic()

    def run(argv):
        return subprocess.run(argv, cwd="/", capture_output=True, text=True, timeout=60)

    def enforce():  # the host timer's one-shot, in its own process
        started = time.monotonic()
        done = run(rig.argv("--enforce"))
        print(f"enforcer one-shot started {started - reserved_at:.3f} s after the 1 s reserve and took {time.monotonic() - started:.3f} s")
        return done

    first = enforce()
    assert (first.returncode, first.stdout) == (0, "[]\n"), first
    time.sleep(max(0.0, 1.5 - (time.monotonic() - reserved_at)))
    late = enforce()
    assert late.returncode == 0 and late.stdout.count("\n") == 1, late
    assert json.loads(late.stdout) == [{"lane": LANE1, "generation": 7, "due": ["heartbeat-stale"], "state": "quarantined"}]
    assert rig.smi_calls() == []  # a protected lease is quarantined, never probed; the unprotected one is not due
    assert (rig.reply(request("beat"))["error"]["code"], rig.reply(request("beat", lane=LANE2))["ok"]) == ("conflict", True)
    no_site = [*rig.argv()[:2], "--enforce", "--state", str(rig.state), "--host-id", "host-1"]
    for argv in (no_site, rig.argv("--enforce", "--controller", "controller-a")):  # the enforce form refuses a missing site and a controller
        usage = run(argv)
        assert (usage.returncode, usage.stdout) == (64, "") and usage.stderr.count("\n") == 1, usage
    print(f"whole test: {time.monotonic() - begun:.3f} s of real time ({time.monotonic() - reserved_at:.3f} s after the 1 s reserve)")
