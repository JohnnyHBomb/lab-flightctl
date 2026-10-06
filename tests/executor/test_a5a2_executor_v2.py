"""A5a2 part 1 named acceptance tests: ExecutorV2's inhibitor first (D-pow-3), inspect, ceiling and extend."""

import copy
import json
import subprocess
import sys
import time
from datetime import datetime, timezone

import pytest

import flightctl.executor
from flightctl.clock import RealClock
from flightctl.executor import JsonStateStore, MemoryStateStore
from tests.contracts_v2.validation import assert_invalid, assert_valid, fence_evidence_ok, grant_after_reserve
from tests.executor.test_a5a_executor_v2 import CARD, CARDS, ENFORCE, LANE, LANE2, ROOT, SENT, UTC, ScriptedProbe, call, observation, request
from tests.sim.rig import SimClock

UNIT, AWAKE = "flightctl-lane-gpu1-g7.service", "flightctl-awake-lane-gpu1-g7.service"  # the lease identity's unit; its inhibitor's unit
HOLD, RELEASE = ("hold", "lane-gpu1", 7, 10.0), ("release", "lane-gpu1", 7, 10.0)  # the executor's calls: lane, generation, timeout_s
PORT_ERROR = {"code": "inhibitor_failed", "message": "systemd-inhibit refused: Access denied", "layer": "executor", "cause": None}
HELD, FREED = ({"held": held, "unit": AWAKE, "error": None} for held in (True, False))
REFUSED, STUCK = ({"held": held, "unit": AWAKE, "error": PORT_ERROR} for held in (False, True))  # a refused hold; a release that failed
EVIDENCE = {"lane_id": "lane-gpu1", "host_id": "host-1"}


class ScriptedInhibitor:  # the Inhibitor port answering its scripted results in order (an exception is raised); records every call
    def __init__(self, *results):
        self.results, self.calls = list(results), []

    def _answer(self, *record):
        self.calls.append(record)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def hold(self, lane_id, generation, *, why, timeout_s):
        return self._answer("hold", lane_id, generation, timeout_s)

    def release(self, lane_id, generation, *, timeout_s):
        return self._answer("release", lane_id, generation, timeout_s)


class FailingStore(MemoryStateStore):
    def save(self, state):
        raise OSError("disk full")


def executor(clock, store, probe=None, port=None):
    return flightctl.executor.ExecutorV2(clock, host_id="host-1", store=store, lane_cards=CARDS, occupancy=probe, inhibitor=port)


def reserve(sent_at=SENT, inhibitor=True, **kwargs):
    """The contract's own reserve example (awake.hold_inhibitor true), checked against the schema."""
    req = request("reserve", sent_at, **kwargs)
    req["awake"]["hold_inhibitor"] = inhibitor
    return req


def ask(kind, sent_at, ident=None, *, valid=True, **fields):
    """A ceiling, extend or inspect request built from the contract's own beat example."""
    req = request("beat", sent_at, **(ident or {}))
    del req["deadlines"]
    req.update(kind=kind, controller_request_id=f"creq-{kind}", **fields)
    (assert_valid if valid else assert_invalid)(req, "executor", "request")
    return req


def max_end(sent_at, in_s):
    return {"kind": "max-end", "in_s": in_s, "sender_utc": sent_at.strftime(UTC)}


def test_definite_refusal_leaves_no_fence_and_no_inhibitor():
    clock = SimClock(boot_id="sim-host-1", utc_start=SENT)
    store, empty, port = MemoryStateStore(), observation(), ScriptedInhibitor(REFUSED, FREED, HELD, STUCK, FREED)
    host = executor(clock, store, ScriptedProbe(empty, empty), port)
    refused = call(host, reserve())
    assert (refused["ok"], refused["definite"], refused["observed_state"], refused["fences"], refused["inhibitor"]) == (False, True, "free", [], None)
    assert (refused["error"]["code"], refused["error"]["cause"], port.calls, store.value) == ("inhibitor_failed", PORT_ERROR, [HOLD, RELEASE], None)
    reserved = call(host, reserve())  # the refusal kept nothing, not even generation 7
    assert (reserved["ok"], reserved["fences"][0]["inhibitor_held"], reserved["inhibitor"]) == (True, True, {"held": True, "unit": AWAKE, "what": "idle"})
    assert fence_evidence_ok(reserved, EVIDENCE)
    other = call(host, reserve(generation=8))
    assert (other["ok"], other["definite"], other["error"]["code"], other["inhibitor"], len(port.calls)) == (False, False, "fenced", reserved["inhibitor"], 3)
    stuck = call(host, request("stop", SENT))  # the lane is empty but its inhibitor's release is unconfirmed: the fence stays
    assert (stuck["ok"], stuck["definite"], stuck["error"]["code"], stuck["observed_state"], stuck["occupancy"]) == (False, False, "inhibitor_failed", "stopping", empty)
    assert (stuck["fences"][0]["inhibitor_held"], stuck["inhibitor"]) == (True, reserved["inhibitor"])
    freed = call(host, request("stop", SENT))
    assert (freed["ok"], freed["observed_state"], freed["fences"], freed["inhibitor"], port.calls[3:]) == (True, "free", [], None, [RELEASE, RELEASE])
    for release, definite, state in ((FREED, True, "free"), (STUCK, False, "unknown"), (None, False, "unknown"), (OSError("bus"), False, "unknown")):
        port = ScriptedInhibitor(HELD, release)  # a save that raises after a confirmed hold: released first; definite only when confirmed
        failed = call(executor(clock, FailingStore(), None, port), reserve())
        assert (failed["ok"], failed["definite"], failed["error"]["code"], failed["observed_state"], failed["fences"], failed["inhibitor"]) == (False, definite, "unavailable", state, [], None)
        assert port.calls == [HOLD, RELEASE]
    store = MemoryStateStore()  # no inhibitor port: a reserve that asks for one is refused and nothing is saved
    nothing = call(executor(clock, store), reserve())
    assert (nothing["ok"], nothing["definite"], nothing["error"]["code"], nothing["observed_state"], store.value) == (False, True, "unavailable", "free", None)


@pytest.mark.realtime
def test_ceiling_shortens_only_and_extend_needs_approval(tmp_path):
    path, port, now = tmp_path / "executor-v2.json", ScriptedInhibitor(HELD), lambda: datetime.now(timezone.utc)
    host, begun = executor(RealClock(), JsonStateStore(path), port=port), time.monotonic()
    expect = {"lease_id": "lse-0000100", "generation": 7, "host_id": "host-1", "lane_id": "lane-gpu1", "request_ids": {"creq-reserve", "creq-ceiling"}}
    seen = lambda *replies: [{"received_t": time.monotonic(), "reply": reply} for reply in replies]
    reserved = call(host, reserve(now()))  # lease 1: the inhibitor held and a 14400 s max-end
    approved = time.monotonic() + 3600  # the approval ends within the hour, the host's max-end is 4 hours out
    assert (reserved["ok"], grant_after_reserve(seen(reserved), approved, expect)) == (True, "pending ceiling-unconfirmed")
    for bad in (ask("extend", now(), valid=False, max_end=max_end(now(), 99999)), ask("extend", now(), valid=False, approval_id=None, max_end=max_end(now(), 99999)),
                request("beat", now(), (("max-end", 99999),))):
        refused = call(host, bad)
        assert (refused["ok"], refused["definite"], refused["error"]["code"], refused["fences"]) == (False, True, "invalid", reserved["fences"])
    second = call(host, request("reserve", now(), (("expiry", 1800), ("max-end", 1), ("heartbeat-stale", 600)), lane=LANE2))  # lease 2: a 1 s max-end
    extended = call(host, ask("extend", now(), {"lane": LANE2}, approval_id="apr-0000001", max_end=max_end(now(), 3600)))
    assert extended["ok"] and 3599 < extended["max_end_remaining_s"] <= 3600
    others = lambda reply: [d for d in reply["fences"][0]["local_deadlines"] if d["kind"] != "max-end"]
    assert others(extended) == others(second)  # only the max-end moved
    ceiling = call(host, ask("ceiling", now(), max_end=max_end(now(), 1)))
    capped = time.monotonic()
    later = call(host, ask("ceiling", now(), max_end=max_end(now(), 9000)))  # never later
    assert (ceiling["ok"], later["ok"], ceiling["max_end_remaining_s"] <= 1, later["max_end_remaining_s"] <= ceiling["max_end_remaining_s"]) == (True,) * 4
    assert later["fences"][0]["local_deadlines"] == ceiling["fences"][0]["local_deadlines"]
    assert grant_after_reserve(seen(reserved, ceiling), approved, expect) == "grant"  # the ceiling reply confirms what the reserve reply could not
    time.sleep(max(0.0, 1.5 - (time.monotonic() - capped)))
    ran = time.monotonic()
    run = subprocess.run([sys.executable, "-c", ENFORCE, str(path), json.dumps(CARDS)], cwd=ROOT, capture_output=True, text=True, timeout=60)
    print(f"enforcer one-shot started {ran - capped:.3f} s after the 1 s ceiling and took {time.monotonic() - ran:.3f} s")
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == [{"lane": LANE, "generation": 7, "due": ["max-end"], "state": "quarantined"}]  # the extended lease is left alone
    after = executor(RealClock(), JsonStateStore(path))
    assert (call(after, request("beat", now()))["error"]["code"], call(after, request("beat", now(), lane=LANE2))["ok"]) == ("conflict", True)
    print(f"whole test: {time.monotonic() - begun:.3f} s of real time")


def test_inspect_reports_holder_unit_absent():
    clock = SimClock(boot_id="sim-host-1", utc_start=SENT)
    store, empty, port = MemoryStateStore(), observation(), ScriptedInhibitor(HELD)
    probe = ScriptedProbe(empty, empty)
    host = executor(clock, store, probe, port)
    call(host, reserve())
    saved = copy.deepcopy(store.value)
    leased = call(host, ask("inspect", SENT, scope="lease"))
    assert (leased["ok"], leased["definite"], leased["error"], leased["observed_state"], leased["occupancy"]) == (True, True, None, "reserved", empty)
    assert [(fence["state"], fence["inhibitor_held"]) for fence in leased["fences"]] == [("reserved", True)] and fence_evidence_ok(leased, EVIDENCE)
    assert leased["unit"] == {"kind": "unit-observation", "unit": UNIT, "run_id": "run-0000007a", "invocation_id": None,
                              "state": "absent", "load_state": "not-found", "active_state": None, "sub_state": None, "result": None, "main_pid": None,
                              "exit_status": None, "cgroup": None, "cgroup_pids": [], "cgroup_empty": True, "observed_at": "2026-10-02T10:00:00Z", "error": None}
    free = call(host, ask("inspect", SENT, scope="lane", identity=None, lane=LANE2))
    assert (free["ok"], free["definite"], free["observed_state"], free["fences"], free["unit"], free["occupancy"]) == (True, True, "free", [], None, None)
    assert (store.value, len(probe.calls), port.calls) == (saved, 1, [HOLD])  # nothing saved, the free lane not probed, the inhibitor not touched
    clock.advance(600)  # the protected lease's heartbeat goes stale: the enforcer quarantines it without a probe or a release
    assert [acted["state"] for acted in host.enforce_deadlines()] == ["quarantined"]
    quarantined = call(host, ask("inspect", clock.utc(), scope="lane", identity=None, lane=LANE))
    assert (quarantined["ok"], quarantined["definite"], quarantined["error"]["code"], quarantined["observed_state"]) == (False, True, "conflict", "quarantined")
    assert (quarantined["unit"]["unit"], quarantined["unit"]["state"], quarantined["inhibitor"]["held"], port.calls) == (UNIT, "absent", True, [HOLD])
    assert probe.calls == [("host-1", "lane-gpu1", [CARD], [{"argv0": "browser", "uid": 1000}], 64, 1024, 10.0)] * 2
