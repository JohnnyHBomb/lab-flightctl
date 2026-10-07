"""A5a3 part 1 named acceptance tests: the executor stdio entry point serves protocol v2 through ExecutorV2 and runs the enforcer one-shot."""

import fcntl
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from flightctl.clock import RealClock
from flightctl.executor_stdio import main
from tests.contracts.validation import validate_instance
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
    validate_instance(v1["reply"], "executor-v1.schema.json")
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

    def enforce():  # the host timer's one-shot, in its own process
        started = time.monotonic()
        done = rig.run(rig.argv("--enforce"))
        print(f"enforcer one-shot started {started - reserved_at:.3f} s after the 1 s reserve and took {time.monotonic() - started:.3f} s")
        return done

    first = enforce()
    assert (first.returncode, first.stdout) == (0, b"[]\n"), first
    time.sleep(max(0.0, 1.5 - (time.monotonic() - reserved_at)))
    late = enforce()
    assert late.returncode == 0 and late.stdout.count(b"\n") == 1, late
    assert json.loads(late.stdout) == [{"lane": LANE1, "generation": 7, "due": ["heartbeat-stale"], "state": "quarantined"}]
    assert rig.smi_calls() == []  # a protected lease is quarantined, never probed; the unprotected one is not due
    assert (rig.reply(request("beat"))["error"]["code"], rig.reply(request("beat", lane=LANE2))["ok"]) == ("conflict", True)
    no_site = [*rig.argv()[:2], "--enforce", "--state", str(rig.state), "--host-id", "host-1"]
    for argv in (no_site, rig.argv("--enforce", "--controller", "controller-a")):  # the enforce form refuses a missing site and a controller
        usage = rig.run(argv)
        assert (usage.returncode, usage.stdout) == (64, b"") and usage.stderr.count(b"\n") == 1, usage
    print(f"whole test: {time.monotonic() - begun:.3f} s of real time ({time.monotonic() - reserved_at:.3f} s after the 1 s reserve)")


# What the brief decides but names no test for: the argument grammar, the refusals, the lock.

USAGE_ERRORS = """--state S --controller
--state S --controller C --state S
--state S --controller C --bogus x
--state S --controller EMPTY
--state relative.json --controller C
--state S --controller C --host-id host-1
--state S --controller C --nvidia-smi N
--state S --controller C --host-id Host-1 --site-dir D
--state S --controller C --host-id host-1 --site-dir D --nvidia-smi nvidia-smi""".splitlines()


def test_bad_arguments_are_usage_errors_that_read_and_write_nothing(tmp_path, monkeypatch, capsys):
    rig = Rig(tmp_path)
    words = {"S": str(rig.state), "D": str(rig.site), "N": str(rig.smi), "C": "controller-a", "EMPTY": ""}
    monkeypatch.setattr(sys, "stdin", None)  # a read would end in exit 1, not in a usage error
    before = sorted(tmp_path.rglob("*"))
    for line in USAGE_ERRORS:
        assert main([words.get(word, word) for word in line.split()]) == 64, line
        out, err = capsys.readouterr()
        assert out == "" and err.startswith("usage: ") and err.count("\n") == 1, line
    assert sorted(tmp_path.rglob("*")) == before


def test_refusals_answer_nothing_and_write_nothing(tmp_path):
    rig = Rig(tmp_path)
    good, before = json.dumps(rig.inventory), sorted(tmp_path.rglob("*"))
    serve, reserve = rig.argv("--controller", "controller-a"), request("reserve")

    def refused(code, start, payload=None, argv=serve):
        done = rig.run(argv, json.dumps(reserve).encode() if payload is None else payload)
        assert (done.returncode, done.stdout) == (code, b"") and done.stderr.startswith(start) and done.stderr.count(b"\n") == 1, done

    refused(2, b"unparsable:", b"not json")
    refused(2, b"unparsable:", json.dumps(dict(reserve, schema_version=3)).encode())
    refused(255, b"Permission denied", json.dumps(dict(reserve, controller_id="controller-b")).encode())
    refused(1, b"failed:", argv=[*serve[:2], "--state", str(rig.state), "--controller", "controller-a"])  # a v2 request needs --host-id
    for mutate in (lambda inv: (inv.update(stage="draft"), inv["lanes"].clear()), lambda inv: inv["lanes"][0].update(device_ids=["gpu-zzz"])):
        rig.inventory = json.loads(good)
        mutate(rig.inventory)
        rig.publish()
        refused(1, b"failed:")  # the hashes are right: a draft inventory (even with no lane of this host), a lane that does not bind
    (rig.site / "SHA256SUMS").write_text("not a manifest\n", encoding="utf-8")
    refused(1, b"failed:")
    assert sorted(tmp_path.rglob("*")) == before  # no state, v2 or lock file


def test_second_invocation_waits_for_the_lock(tmp_path):
    rig = Rig(tmp_path)
    with open(f"{rig.state}.lock", "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        waiting = subprocess.Popen(rig.argv("--enforce"), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd="/")
        time.sleep(0.5)
        assert waiting.poll() is None  # the lock is held: it has not answered
    out, err = waiting.communicate(timeout=30)  # released: it answers
    assert (waiting.returncode, out, err) == (0, b"[]\n", b"")
