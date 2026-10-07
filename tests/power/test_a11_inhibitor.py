"""A11 named acceptance tests: the Inhibitor twins (real, dryrun, fake) over a scripted CommandRunner, and the
executor's definite refusal when the real twin's hold fails. Nothing here starts systemd: G3 runs the on-lab cases."""

import pytest

from flightctl.executor import ExecutorV2, MemoryStateStore
from tests.contracts_v2.validation import assert_valid, fence_evidence_ok
from tests.executor.test_a5a_executor_v2 import CARDS, SENT, ScriptedProbe, call, observation, request
from tests.sim.rig import SimClock

RUNTIME = "/run/user/1000"


def host(store=None, probe=None, inhibitor=None):
    clock = SimClock(boot_id="sim-host-1", utc_start=SENT)
    store = store or MemoryStateStore()
    return ExecutorV2(clock, host_id="host-1", store=store, lane_cards=CARDS, occupancy=probe, inhibitor=inhibitor)


def awake(req):  # the reserve example for a lane on a host that sleeps: the authority asks for the inhibitor
    req["awake"]["hold_inhibitor"] = True
    return req

PREFIX = ["env", "XDG_RUNTIME_DIR=" + RUNTIME]
ARGS = ("lane-gpu1", 7)
UNIT = "flightctl-awake-lane-gpu1-g7.service"
WHY = "flightctl lease lse-0000100"  # the executor's reason for the example reserve's lease
HOLD = ["systemd-run", "--user", "--unit=" + UNIT[:-8], "--collect", "systemd-inhibit", "--what=idle", "--mode=block",
        "--who=flightctl", "--why=" + WHY, "sleep", "infinity"]
STOP = ["systemctl", "--user", "stop", UNIT]
# the executor's reason is free text (A5a2 DECIDED 3)
EXECUTOR_HOLD = [a if a != "--why=" + WHY else "--why=*" for a in HOLD]


def why_free(calls):  # a non-empty --why= of any text compares as --why=*
    return [[("--why=*" if a.startswith("--why=") and len(a) > 6 else a) for a in argv] for argv in calls]


LIST = ["systemctl", "--user", "list-units", "--plain", "--no-legend", "--full", "flightctl-awake-*.service"]
LINE = UNIT + " loaded active running systemd-inhibit --what=idle sleep infinity\n"
FAIL = {"returncode": 1, "stderr": "Failed to connect to bus: No medium found\n"}
GONE = {"returncode": 5, "stderr": f"Failed to stop {UNIT}: Unit {UNIT} not loaded.\n"}
LOST = {"error": {"code": "transport_failed", "message": "no ssh", "layer": "transport", "cause": None}}
TIMEOUT = {"returncode": None, "timed_out": True,
           "error": {"code": "timeout", "message": "no answer", "layer": "runner", "cause": None}}


class Scripted:  # a CommandRunner: hold, stop and list answer with the fields of `script`; records argv and time left
    def __init__(self, clock=None, cost=0, **script):
        self.script, self.clock, self.cost, self.calls, self.left = script, clock, cost, [], []

    def run(self, argv, *, timeout_s):
        self.calls.append(argv)
        self.left.append(timeout_s)
        if self.clock:
            self.clock.advance(self.cost)  # the command takes `cost` seconds of the twin's clock
        verb = "hold" if "systemd-run" in argv else "stop" if "stop" in argv else "list"
        return {"argv": argv, "host_id": None, "returncode": 0, "stdout": "", "stderr": "", "timed_out": False,
                "duration_s": 0.0, "error": None, **self.script.get(verb, {})}


def result(held, error=None, *, dry_run=False):
    return {"held": held, "unit": UNIT, "error": error, "dry_run": dry_run}


def reserve(executor):  # the example reserve for a lane on a host that sleeps: the authority asks for the inhibitor
    return call(executor, awake(request("reserve", SENT)))


def verdict(reply):
    return reply["ok"], reply["definite"], reply["error"] and reply["error"]["code"], reply["observed_state"]


def test_inhibitor_failure_is_definite_refusal():
    from flightctl import power
    store, runner = MemoryStateStore(), Scripted(hold=FAIL, stop=GONE)
    refused = reserve(host(store, inhibitor=power.SystemdInhibitor(runner, runtime_dir=RUNTIME)))
    assert verdict(refused) == (False, True, "inhibitor_failed", "free")
    assert (refused["fences"], refused["inhibitor"], store.value) == ([], None, None)
    # the hold, then its release: stop and list
    assert why_free(runner.calls) == [PREFIX + EXECUTOR_HOLD, PREFIX + STOP, PREFIX + LIST]
    cause = refused["error"]["cause"]  # the twin's typed error, with the reason systemd gave
    assert cause["code"] == "inhibitor_failed" and "No medium found" in cause["message"]
    lost = reserve(host(store, inhibitor=power.SystemdInhibitor(Scripted(hold=LOST), runtime_dir=RUNTIME)))
    assert (*verdict(lost), lost["error"]["cause"]["cause"]) == (False, True, "inhibitor_failed", "free", LOST["error"])
    unverified = Scripted(hold=FAIL, stop=FAIL, list=FAIL)  # the release is not verified either: not definite
    unsure = reserve(host(store, inhibitor=power.SystemdInhibitor(unverified, runtime_dir=RUNTIME)))
    assert verdict(unsure) == (False, False, "inhibitor_failed", "unknown") and store.value is None
    fake = power.FakeInhibitor()
    fake.script_next("refused")
    same = reserve(host(store, inhibitor=fake))  # the fake's refused hold: the same definite refusal, nothing held
    assert verdict(same) == (False, True, "inhibitor_failed", "free")
    assert (same["error"]["cause"]["code"], fake.list(timeout_s=10), store.value) == (
        "inhibitor_failed", {"units": [], "error": None}, None)
    runner = Scripted()  # a hold that succeeds: the list after the stop names no unit
    executor = host(store, ScriptedProbe(observation()), power.SystemdInhibitor(runner, runtime_dir=RUNTIME))
    held = reserve(executor)
    assert held["ok"] and fence_evidence_ok(held, {"lane_id": "lane-gpu1", "host_id": "host-1"})
    assert held["inhibitor"] == {"held": True, "unit": UNIT, "what": "idle"}
    assert why_free(runner.calls) == [PREFIX + EXECUTOR_HOLD]
    freed = call(executor, request("stop", SENT))  # proven empty, then released: the stop, and the list shows it gone
    assert (freed["ok"], freed["inhibitor"], runner.calls[1:]) == (True, None, [PREFIX + STOP, PREFIX + LIST])
    assert fake.hold(*ARGS, why=WHY, timeout_s=10) == result(True)
    fake.script_next("timeout")  # the next release changes nothing: the unit stays held, with a timeout error
    stuck, gone = (fake.release(*ARGS, timeout_s=10) for _ in range(2))  # only that one call is scripted
    assert (stuck["held"], stuck["error"]["code"], gone, fake.what) == (True, "timeout", result(False), "idle")
    with pytest.raises(ValueError):
        fake.script_next("success")


def test_real_twin_runs_the_contract_commands(monkeypatch):
    from flightctl import power

    def twin(**script):
        return power.SystemdInhibitor(Scripted(**script), runtime_dir=RUNTIME)

    runner = Scripted()
    real = power.SystemdInhibitor(runner, runtime_dir=RUNTIME)
    assert real.hold(*ARGS, why=WHY, timeout_s=10) == result(True)
    assert (real.what, runner.calls) == ("idle", [PREFIX + HOLD])  # nothing follows a hold that exited 0
    for answer, code in ((FAIL, "inhibitor_failed"), (LOST, "inhibitor_failed"), ({"stdout": None}, "inhibitor_failed"),
                         (TIMEOUT, "timeout")):
        held = twin(hold=answer).hold(*ARGS, why=WHY, timeout_s=10)
        assert (held["held"], held["unit"], held["error"]["code"]) == (False, UNIT, code)
        assert held["error"]["cause"] == answer.get("error")  # the runner's own error is kept
        assert_valid(held["error"], "common", "typed_error")
    a, b = "flightctl-awake-a-g1.service", "flightctl-awake-b-g2.service"
    out = f"{b} loaded failed failed b\n\n  \n  {a} loaded active running a b\n{b} loaded active running\n"
    runner = Scripted(list={"stdout": out})
    listed = power.SystemdInhibitor(runner, runtime_dir=RUNTIME).list(timeout_s=10)
    assert (listed, runner.calls) == ({"units": [a, b], "error": None}, [PREFIX + LIST])  # distinct, ascending
    assert twin(list={"stdout": " \n\n"}).list(timeout_s=10) == {"units": [], "error": None}
    for stdout in ("No units listed.\n", LINE + "oops\n", "flightctl-awake-A-g1.service x\n",
                   "UNIT LOAD ACTIVE SUB DESCRIPTION\n" + LINE):  # the header line --no-legend leaves out
        unparsable = twin(list={"stdout": stdout}).list(timeout_s=10)  # one line that is no unit spoils it all
        assert (unparsable["units"], unparsable["error"]["code"]) == (None, "inhibitor_failed")
    for answer, code in ((FAIL, "inhibitor_failed"), (TIMEOUT, "timeout")):
        failed = twin(list=answer).list(timeout_s=10)
        assert (failed["units"], failed["error"]["code"], set(failed)) == (None, code, {"units", "error"})
    for stop in (GONE, FAIL, TIMEOUT):  # the stop is never decisive (a gone unit exits 5): the list that follows is
        runner = Scripted(stop=stop)
        released = power.SystemdInhibitor(runner, runtime_dir=RUNTIME).release(*ARGS, timeout_s=10)
        assert (released, runner.calls) == (result(False), [PREFIX + STOP, PREFIX + LIST])
    for script, code in (({"list": FAIL}, "inhibitor_failed"), ({"list": TIMEOUT}, "timeout"),
                         ({"list": {"stdout": "?\n"}}, "inhibitor_failed"),
                         ({"list": {"stdout": LINE}}, "inhibitor_failed")):
        stuck = twin(**script).release(*ARGS, timeout_s=10)  # still held, with a typed error: never "released"
        assert (stuck["held"], stuck["unit"], stuck["error"]["code"]) == (True, UNIT, code)
        assert_valid(stuck["error"], "common", "typed_error")
    clock = SimClock(boot_id="sim-host-1")
    monkeypatch.setattr(power, "_time", clock)  # one budget per call: each command gets what is left of timeout_s
    for cost, calls, left, held in ((6, [STOP, LIST], [10, 4], False), (11, [STOP], [10], True)):
        runner = Scripted(clock, cost)
        released = power.SystemdInhibitor(runner).release(*ARGS, timeout_s=10)  # no runtime dir: no prefix
        assert (runner.calls, runner.left, released["held"]) == (calls, left, held)
    assert released["error"]["code"] == "timeout"  # no time left: the list is not run
    runner = Scripted()
    twins = (power.SystemdInhibitor(runner), power.DryRunInhibitor(runner), power.FakeInhibitor())
    bad = (({"lane_id": "Lane"}, ValueError), ({"lane_id": 7}, TypeError), ({"generation": 0}, ValueError),
           ({"generation": True}, TypeError), ({"why": ""}, ValueError), ({"why": "a\0b"}, ValueError),
           ({"why": None}, TypeError), ({"timeout_s": 0}, ValueError), ({"timeout_s": True}, TypeError),
           ({"timeout_s": float("inf")}, ValueError), ({"lane_id": "lane\n"}, ValueError))
    for one in twins:  # every refusal comes before any command, record or change
        for change, error in bad:
            with pytest.raises(error):
                one.hold(**{"lane_id": "lane-gpu1", "generation": 7, "why": WHY, "timeout_s": 10, **change})
        with pytest.raises(ValueError):
            one.release("lane-gpu1", 0, timeout_s=10)
        with pytest.raises(ValueError):
            one.release(*ARGS, timeout_s=0)
        with pytest.raises(ValueError):
            one.list(timeout_s=0)
    assert (runner.calls, twins[1].recorded, twins[2].list(timeout_s=10)["units"]) == ([], [], [])
    for cls in (power.SystemdInhibitor, power.DryRunInhibitor):
        for runtime_dir, error in ((7, TypeError), ("run", ValueError), ("/a\0b", ValueError)):
            with pytest.raises(error):
                cls(runner, runtime_dir=runtime_dir)
        with pytest.raises(TypeError):
            cls(object())


def test_dryrun_twin_lists_for_real_and_records_the_rest():
    from flightctl import power
    runner = Scripted(list={"stdout": LINE})
    dry = power.DryRunInhibitor(runner, runtime_dir=RUNTIME)
    assert (dry.what, dry.recorded) == ("idle", [])
    assert dry.list(timeout_s=10) == {"units": [UNIT], "error": None}  # the read goes through the runner
    assert dry.hold(*ARGS, why=WHY, timeout_s=10) == result(True, dry_run=True)
    assert dry.release(*ARGS, timeout_s=10) == result(False, dry_run=True)
    assert (dry.recorded, runner.calls) == ([PREFIX + HOLD, PREFIX + STOP], [PREFIX + LIST])  # only the read ran
    with pytest.raises(ValueError):
        dry.hold("lane-gpu1", 0, why=WHY, timeout_s=10)
    assert len(dry.recorded) == 2  # a refused call records nothing
    other = power.DryRunInhibitor(Scripted(list=FAIL))  # the read is the real twin's: unknown, never empty
    unread = other.list(timeout_s=10)
    assert (unread["units"], unread["error"]["code"], other.recorded) == (None, "inhibitor_failed", [])
    other.hold(*ARGS, why=WHY, timeout_s=10)  # no runtime dir: the records carry no prefix
    assert (other.release(*ARGS, timeout_s=10), other.recorded) == (result(False, dry_run=True), [HOLD, STOP])
