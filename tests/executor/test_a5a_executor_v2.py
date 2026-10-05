"""A5a part 1 named acceptance tests: executor protocol v2 in holder mode (ExecutorV2) and its deadline enforcer."""

import copy
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from flightctl.clock import RealClock
from flightctl.executor import ExecutorV2, JsonStateStore, MemoryStateStore
from tests.contracts_v2.validation import assert_invalid, assert_valid, examples, occupancy_from_capture
from tests.sim.rig import SimClock

ROOT = Path(__file__).resolve().parents[2]
UTC = "%Y-%m-%dT%H:%M:%SZ"
SENT = datetime(2026, 10, 2, 10, 0, tzinfo=timezone.utc)  # the reserve example's sent_at
CARD = "GPU-00000000-0000-0000-0000-000000000011"
CARDS = {"lane-gpu1": [CARD], "lane-gpu2": ["GPU-00000000-0000-0000-0000-000000000012"]}
LANE = {"site_id": "site-a", "host_id": "host-1", "lane_id": "lane-gpu1"}
LANE2 = dict(LANE, lane_id="lane-gpu2")
RESERVE = (("expiry", 1800), ("max-end", 14400), ("heartbeat-stale", 600))  # the reserve example's deadlines
BEAT = (("expiry", 1800), ("heartbeat-stale", 600))


def request(kind, sent_at, deadlines=None, *, mode="owner-release", protected=True, **identity):
    """A v2 request built from the contract's reserve example (awake.hold_inhibitor false), checked against the schema."""
    deadlines = deadlines or (RESERVE if kind == "reserve" else BEAT)
    req, stamp = copy.deepcopy(examples("executor")["valid"][0]), sent_at.strftime(UTC)
    req["identity"].update(identity)
    req["execution_policy"]["protected"] = protected
    req.update(kind=kind, controller_request_id=f"creq-{kind}", sent_at=stamp, awake={"hold_inhibitor": False},
               deadlines=[{"kind": k, "in_s": s, "sender_utc": stamp} for k, s in deadlines])
    if kind != "reserve":
        del req["awake"], req["execution_policy" if kind == "beat" else "deadlines"]
    if kind == "stop":
        req["stop_authority"] = {"mode": mode, "approval_id": None, "reason": "test stop"}
    (assert_invalid if kind == "beat" and "max-end" in dict(deadlines) else assert_valid)(req, "executor", "request")
    return req


def call(host, req):
    reply = host.handle(req)
    assert_valid(reply, "executor", "reply")
    assert (reply["kind"], reply["controller_request_id"], reply["echoed_identity"]) == (req["kind"], req["controller_request_id"], req["identity"])
    return reply


def executor(clock, store, probe=None):
    return ExecutorV2(clock, host_id="host-1", store=store, lane_cards=CARDS, occupancy=probe)


def anchored(clock, kind, in_s):
    """The local deadline this host writes for a received in_s at the clock's current reading (G02)."""
    return {"kind": kind, "boot_id": clock.boot_id(), "monotonic_deadline_s": clock.monotonic() + in_s,
            "utc_estimate": (clock.utc() + timedelta(seconds=in_s)).strftime(UTC)}


def observation(procs="", returncode=0):
    """A schema-valid occupancy observation of lane-gpu1, from the contract's oracle."""
    obs = occupancy_from_capture(f"{CARD}, 300, 24576, 0, 41, 18.2, 200, Not Active, Not Active, [N/A]", procs, returncode=returncode,
                                 lane_id="lane-gpu1", host_id="host-1", observed_at="2026-10-02T10:00:00Z", lane_uuids=[CARD])
    assert_valid(obs, "gpu-probe", "occupancy_observation")
    return obs


class ScriptedProbe:  # an OccupancyProbe answering its scripted observations in order; records every call
    def __init__(self, *observations):
        self.observations, self.calls = list(observations), []

    def occupancy(self, host_id, lane_id, uuids, *, noise_allowlist, noise_cap_mib, lane_noise_mib, timeout_s):
        self.calls.append((host_id, lane_id, uuids, noise_allowlist, noise_cap_mib, lane_noise_mib, timeout_s))
        return self.observations.pop(0)


def test_relative_deadline_anchored_on_host_clock():
    clock = SimClock(boot_id="sim-host-1", utc_start=SENT + timedelta(seconds=7), monotonic_start=5000.0)
    store, probe = MemoryStateStore(), ScriptedProbe()
    host = executor(clock, store, probe)
    old = call(host, request("reserve", clock.utc() - timedelta(seconds=31)))
    assert (old["ok"], old["definite"], old["error"]["code"], old["observed_state"], old["fences"]) == (False, True, "clock_skew", "free", [])
    reply = call(host, request("reserve", SENT))  # the controller's sent_at is 7 s behind this host's UTC
    assert (reply["ok"], reply["observed_state"], reply["max_end_remaining_s"]) == (True, "reserved", 14400)
    assert reply["fences"][0]["local_deadlines"] == [anchored(clock, kind, in_s) for kind, in_s in RESERVE]
    assert reply["fences"][0]["local_deadlines"][0] == {"kind": "expiry", "boot_id": "sim-host-1", "monotonic_deadline_s": 6800.0,
                                                        "utc_estimate": "2026-10-02T10:30:07Z"}
    clock.advance(599)
    assert host.enforce_deadlines() == []
    clock.advance(1)
    assert host.enforce_deadlines() == [{"lane": LANE, "generation": 7, "due": ["heartbeat-stale"], "state": "quarantined"}]
    assert probe.calls == []  # a protected lease is quarantined, never probed or stopped
    assert call(host, request("reserve", clock.utc(), lane=LANE2))["ok"]
    clock.reboot("sim-host-1-b")
    clock.advance(20001)  # the new boot's monotonic clock is past every deadline value of the old boot
    host = executor(clock, store, probe)
    assert host.enforce_deadlines() == []
    beat = call(host, request("beat", clock.utc(), lane=LANE2))
    assert (beat["ok"], beat["definite"], beat["error"]["code"], beat["observed_state"]) == (False, True, "reconcile_required", "reserved")
    assert (beat["fences"][0]["rebooted_since_reserve"], beat["max_end_remaining_s"], probe.calls) == (True, None, [])


def test_beat_extends_expiry_never_max_end():
    clock = SimClock(boot_id="sim-host-1", utc_start=SENT, monotonic_start=100.0)
    host = executor(clock, MemoryStateStore())
    reserve = call(host, request("reserve", SENT, (("expiry", 900), ("max-end", 3600), ("heartbeat-stale", 600))))
    max_end = reserve["fences"][0]["local_deadlines"][1]
    for step in range(1, 12):
        clock.advance(300)
        beat = call(host, request("beat", clock.utc(), (("expiry", 900), ("heartbeat-stale", 600))))
        assert beat["ok"] and beat["fences"][0]["local_deadlines"] == [anchored(clock, "expiry", 900), max_end, anchored(clock, "heartbeat-stale", 600)]
        assert beat["max_end_remaining_s"] == 3600 - 300 * step
    bad = call(host, request("beat", clock.utc(), (("max-end", 99999),)))  # the contract's own invalid beat
    assert (bad["ok"], bad["definite"], bad["error"]["code"]) == (False, True, "invalid")
    assert (bad["fences"], bad["max_end_remaining_s"]) == (beat["fences"], beat["max_end_remaining_s"])
    other = call(host, request("beat", clock.utc(), run_id="run-0000007b"))
    assert (other["ok"], other["error"]["code"], other["fences"]) == (False, "identity_mismatch", beat["fences"])
    clock.advance(300)
    late = call(host, request("beat", clock.utc()))
    assert (late["ok"], late["error"]["code"], late["max_end_remaining_s"], late["fences"][0]["local_deadlines"][1]) == (False, "conflict", 0, max_end)
    assert host.enforce_deadlines() == [{"lane": LANE, "generation": 7, "due": ["max-end"], "state": "quarantined"}]


def test_stop_requires_reserve_identity_and_empty_proof():
    clock = SimClock(boot_id="sim-host-1", utc_start=SENT)
    tenant, unknown, empty = observation(f"{CARD}, 77, python, 300"), observation(returncode=9), observation()
    probe = ScriptedProbe(tenant, unknown, empty)
    host = executor(clock, MemoryStateStore(), probe)
    call(host, request("reserve", SENT))
    for change in ({"run_id": "run-0000007b"}, {"lease_id": "lse-0000101"}, {"token_sha256": "d" * 64}):
        reply = call(host, request("stop", SENT, **change))
        assert (reply["ok"], reply["definite"], reply["error"]["code"], reply["observed_state"]) == (False, True, "identity_mismatch", "reserved")
    denied = call(host, request("stop", SENT, mode="controller-match", protected=False))
    assert (denied["ok"], denied["error"]["code"], denied["observed_state"], probe.calls) == (False, "denied", "reserved", [])
    for code, seen in (("gpu_tenant", tenant), ("probe_unknown", unknown)):
        reply = call(host, request("stop", SENT))
        assert (reply["ok"], reply["definite"], reply["error"]["code"], reply["observed_state"], reply["occupancy"]) == (False, False, code, "stopping", seen)
        assert reply["fences"][0]["state"] == "stopping"
    freed = call(host, request("stop", SENT))
    assert (freed["ok"], freed["definite"], freed["observed_state"], freed["fences"], freed["occupancy"]) == (True, True, "free", [], empty)
    assert probe.calls == [("host-1", "lane-gpu1", [CARD], [{"argv0": "browser", "uid": 1000}], 64, 1024, 10.0)] * 3
    assert call(host, request("reserve", SENT))["error"]["code"] == "stale_generation"  # the lane keeps its highest generation


ENFORCE = """import json, sys
from flightctl.clock import RealClock
from flightctl.executor import ExecutorV2, JsonStateStore
host = ExecutorV2(RealClock(), host_id="host-1", store=JsonStateStore(sys.argv[1]), lane_cards=json.loads(sys.argv[2]))
print(json.dumps(host.enforce_deadlines()))
"""


@pytest.mark.realtime
def test_enforcer_real_seconds(tmp_path):
    path = tmp_path / "executor-v2.json"
    host = ExecutorV2(RealClock(), host_id="host-1", store=JsonStateStore(path), lane_cards=CARDS)
    assert call(host, request("reserve", datetime.now(timezone.utc), (("expiry", 1800), ("max-end", 14400), ("heartbeat-stale", 1))))["ok"]
    reserved_at = time.monotonic()
    assert call(host, request("reserve", datetime.now(timezone.utc), (("expiry", 1800), ("max-end", 14400), ("heartbeat-stale", 30)), lane=LANE2))["ok"]

    def enforce():  # the host timer's one-shot, in its own process on the shared state file
        begun = time.monotonic()
        run = subprocess.run([sys.executable, "-c", ENFORCE, str(path), json.dumps(CARDS)], cwd=ROOT, capture_output=True, text=True, timeout=60)
        print(f"enforcer one-shot started {begun - reserved_at:.3f} s after the 1 s reserve and took {time.monotonic() - begun:.3f} s")
        assert run.returncode == 0, run.stderr
        return json.loads(run.stdout)

    assert enforce() == []
    time.sleep(max(0.0, 1.5 - (time.monotonic() - reserved_at)))
    assert enforce() == [{"lane": LANE, "generation": 7, "due": ["heartbeat-stale"], "state": "quarantined"}]
    after = ExecutorV2(RealClock(), host_id="host-1", store=JsonStateStore(path), lane_cards=CARDS)
    assert call(after, request("beat", datetime.now(timezone.utc)))["observed_state"] == "quarantined"
    assert call(after, request("beat", datetime.now(timezone.utc), lane=LANE2))["ok"]
    print(f"whole test: {time.monotonic() - reserved_at:.3f} s of real time after the 1 s reserve")
