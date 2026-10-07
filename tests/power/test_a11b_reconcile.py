"""A11b named acceptance tests: ExecutorV2's inhibitor reconcile after a restart (the fake twin), and the on-lab proof
that the pilot host's sleep guard sees a held inhibitor as protected work (the real twin; it skips off-lab)."""

import copy
import json
import os
import time

import pytest

from flightctl import power
from flightctl.executor import ExecutorV2, MemoryStateStore
from tests.contracts_v2.validation import assert_valid
from tests.executor.test_a5a_executor_v2 import CARDS, LANE2, SENT, call, request
from tests.power.test_a11_inhibitor import awake, host
from tests.sim.rig import SimClock

TARGET = os.environ.get("FLIGHTCTL_CONFORMANCE_TARGET") or None
GUARD = os.environ.get("FLIGHTCTL_GUARD_STATUS") or None  # the target's sleep guard status command: a JSON list of str
T = 2.5  # the restarted executor's timeout_s: every port call gets it
LIST = ("list", T)
# lane-gpu1's unit (reserved with the inhibitor), lane-gpu2's (reserved without it) and that of a lane with no fence,
# which this executor does not serve and whose id holds -g
U1, U2, U3 = (f"flightctl-awake-{lane}-g{n}.service" for lane, n in (("lane-gpu1", 7), ("lane-gpu2", 7), ("gpu-g2", 5)))
HOLD1, RELEASE2, RELEASE3 = ("hold", "lane-gpu1", 7, T), ("release", "lane-gpu2", 7, T), ("release", "gpu-g2", 5, T)


class Port(power.FakeInhibitor):  # the fake twin already holding `held` (lane id, generation); records each later call
    def __init__(self, *held, listed=None):  # `listed`, when given, answers every list (an exception is raised)
        super().__init__()
        for lane_id, generation in held:
            super().hold(lane_id, generation, why="left over", timeout_s=1)
        self.listed, self.calls = listed, []

    def list(self, *, timeout_s):
        self.calls.append(("list", timeout_s))
        if isinstance(self.listed, Exception):
            raise self.listed
        return super().list(timeout_s=timeout_s) if self.listed is None else self.listed

    def hold(self, lane_id, generation, *, why, timeout_s):
        self.calls.append(("hold", lane_id, generation, timeout_s))
        return super().hold(lane_id, generation, why=why, timeout_s=timeout_s)

    def release(self, lane_id, generation, *, timeout_s):
        self.calls.append(("release", lane_id, generation, timeout_s))
        return super().release(lane_id, generation, timeout_s=timeout_s)


def restart(store, port):  # a new executor on the same store, after the host rebooted
    return ExecutorV2(SimClock(boot_id="sim-host-1-b", utc_start=SENT), host_id="host-1", store=store, lane_cards=CARDS,
                      inhibitor=port, timeout_s=T)


def report(held=(), released=(), failed=(), error=None):
    return {"held": list(held), "released": list(released), "failed": list(failed), "error": error}


def made(port):  # the list first, then the holds and releases in any order (call order is not judged)
    return port.calls[0], sorted(port.calls[1:])


def test_reconcile_recreates_missing_and_removes_orphan_inhibitors():
    store = MemoryStateStore()
    before = host(store, inhibitor=power.FakeInhibitor())
    assert call(before, awake(request("reserve", SENT)))["inhibitor"]["unit"] == U1
    assert call(before, request("reserve", SENT, lane=LANE2))["inhibitor"] is None
    before.clock.advance(600)  # both protected leases go stale and are quarantined; a quarantine keeps the inhibitor
    assert [acted["state"] for acted in before.enforce_deadlines()] == ["quarantined"] * 2
    document, saved = store.value, copy.deepcopy(store.value)  # a save would replace the document
    port = Port(("lane-gpu2", 7), ("gpu-g2", 5))  # the rebooted host lost U1 and holds two units no fence records
    executor = restart(store, port)
    assert executor.reconcile_inhibitors() == report(held=[U1], released=[U3, U2])
    assert made(port) == (LIST, sorted([HOLD1, RELEASE2, RELEASE3]))
    assert port.list(timeout_s=1) == {"units": [U1], "error": None}
    port.calls.clear()
    assert (executor.reconcile_inhibitors(), port.calls) == (report(), [LIST])  # in agreement: only the list is called

    port = Port(("gpu-g2", 5))  # the host lost U1 again, and its next two calls fail and change nothing
    port.script_next("refused")
    port.script_next("timeout")
    executor = restart(store, port)
    assert executor.reconcile_inhibitors() == report(failed=[U3, U1])  # each call is made once, whatever failed before
    assert made(port) == (LIST, sorted([HOLD1, RELEASE3]))
    assert executor.reconcile_inhibitors() == report(held=[U1], released=[U3])  # the fence still records U1: again

    odd = ["flightctl-awake-gpu.service", "flightctl-awake-lane-gpu1-g07.service"]  # g07 read as 7 would name U1
    port = Port(listed={"units": odd + [U1], "error": None})
    assert (restart(store, port).reconcile_inhibitors(), port.calls) == (report(failed=odd), [LIST])

    error = {"code": "timeout", "message": "systemctl --user list-units timed out", "layer": "runner", "cause": None}
    for listed in ({"units": None, "error": error}, {"units": [U2], "error": error}):  # unknown, never empty: no hold
        port = Port(listed=listed)
        assert (restart(store, port).reconcile_inhibitors(), port.calls) == (report(error=error), [LIST])
    for listed in (OSError("no user bus"), [U2], {"units": [U2, 7], "error": None}, {"units": None, "error": "lost"}):
        port = Port(listed=listed)  # the list raised, or its answer is no mapping or holds no list of unit names
        found = restart(store, port).reconcile_inhibitors()
        assert (found["held"], found["released"], found["failed"], port.calls) == ([], [], [], [LIST])
        assert (found["error"]["code"], found["error"]["layer"]) == ("inhibitor_failed", "executor")
        assert_valid(found["error"], "common", "typed_error")
    found = restart(store, None).reconcile_inhibitors()
    assert (found["held"], found["released"], found["failed"], found["error"]["code"], found["error"]["layer"]) == (
        [], [], [], "unavailable", "executor")
    assert_valid(found["error"], "common", "typed_error")
    assert store.value is document and store.value == saved  # nothing was saved or changed


@pytest.mark.realtime
@pytest.mark.onlab
@pytest.mark.skipif(TARGET is None or GUARD is None, reason="on-lab: set FLIGHTCTL_CONFORMANCE_TARGET to a real host "
                    "and FLIGHTCTL_GUARD_STATUS to its sleep guard's status command (a JSON list of strings)")
def test_guard_sees_inhibitor():
    from flightctl.commands import LocalCommandRunner
    from tests.conformance import impl_inhibitor
    from tests.conformance.impl_workload_runner import SshToTarget
    command = json.loads(GUARD)
    assert isinstance(command, list) and command and all(isinstance(arg, str) for arg in command), GUARD
    real = dict(impl_inhibitor.registry.implementations("inhibitor"))["real"](TARGET)  # built as conformance builds it
    runner = LocalCommandRunner() if TARGET == "host-local" else SshToTarget(TARGET)

    def listed():  # True or False: the guard's status lists a flightctl inhibitor or not; the run when it is unreadable
        ran = runner.run(command, timeout_s=30)
        try:
            reasons = json.loads(ran["stdout"])["reasons"] if ran["returncode"] == 0 and ran["error"] is None else None
        except (TypeError, ValueError, KeyError):
            reasons = None
        readable = isinstance(reasons, list) and all(isinstance(reason, str) for reason in reasons)
        return any(reason.endswith(": flightctl") for reason in reasons) if readable else ran

    def wait(want):  # poll for at most 90 s, 1 s apart, until listed() is `want`: the seconds it took
        started = time.monotonic()
        while (seen := listed()) is not want:
            assert time.monotonic() - started < 90, f"after 90 s the guard's status still gives {seen}"
            time.sleep(1)
        return time.monotonic() - started

    wait(False)
    try:
        held = real.hold("guardtest", 1, why="flightctl guard test", timeout_s=30)
        assert held["held"] is True, held
        shown = wait(True)
    finally:
        released = real.release("guardtest", 1, timeout_s=30)
        assert (released["held"], released["error"]) == (False, None), released
    cleared = wait(False)
    print(f"GUARD-WAITS listed {shown:.1f} s after the hold, no longer listed {cleared:.1f} s after the release")
