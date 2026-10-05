"""A6b named acceptance tests: the per-unit, fail-closed FakeRunner and the dryrun twin that never starts a unit; and the
linger probe's reading of the Amendment 8 captures (linger_report). The A6b names are imported inside each test."""

import json
import os

import pytest

from flightctl.clock import RealClock
from flightctl.commands import LocalCommandRunner
from tests.contracts_v2.validation import assert_valid
from tests.runner.test_a6_runner import ONLAB, TARGET

UNIT = "flightctl-a6btest-g{}.service"


def _start(runner, n, argv=("sleep", "120")):
    return runner.start(UNIT.format(n), f"run-a6btest{n:02d}", list(argv), work_id="job-a6btest1", lease_id="lse-a6btest1",
                        lane_id="a6btest", parent_lease_id=None, env={}, run_as="", workdir="", cards=[], grace_s=5,
                        timeout_s=30)


def test_fake_runner_per_unit_fail_closed() -> None:
    from tests.fakes.runner import FakeRunner
    runner = FakeRunner(crashing=[["false"]])
    never = runner.inspect(UNIT.format(9), None, timeout_s=10)
    assert never["state"] == "absent" and never["cgroup_pids"] == [] and never["cgroup_empty"] is True
    a, b = _start(runner, 1), _start(runner, 2)
    assert a["ok"] is True and b["ok"] is True and a["invocation_id"] != b["invocation_id"]
    pids = [r["observation"]["cgroup_pids"] for r in (a, b)]
    assert pids[0] != pids[1] and [] not in pids
    stopped = runner.stop(UNIT.format(1), a["invocation_id"], timeout_s=30)
    other = runner.inspect(UNIT.format(2), "run-a6btest02", timeout_s=10)
    assert stopped["ok"] is True and other["state"] == "active" and other["cgroup_pids"] == pids[1]
    runner.script_next("stop", "garbage")
    refused = runner.stop(UNIT.format(2), b["invocation_id"], timeout_s=30)
    still = runner.inspect(UNIT.format(2), "run-a6btest02", timeout_s=10)
    assert refused["ok"] is False and still["state"] == "active" and still["invocation_id"] == b["invocation_id"]
    runner.script_next("start", "success")
    failed, crashed = _start(runner, 3), _start(runner, 4, ["false"])
    assert failed["ok"] is False and failed["error"]["code"] == "unit_failed"
    assert crashed["ok"] is False and crashed["error"]["code"] == "unit_absent"
    runner.script_next("inspect", "timeout")
    timed = runner.inspect(UNIT.format(2), None, timeout_s=10)
    assert timed["state"] == "unknown" and timed["cgroup_empty"] is None and timed["error"]["code"] == "timeout"
    for definition, results in (("observation", [never, other, still, timed]), ("start_result", [a, b, failed, crashed]),
                                ("stop_result", [stopped, refused])):
        for result in results:
            assert_valid(result, "unit", definition)


def _sessions(*entries):  # one line of `loginctl list-sessions --json=short` for user1 (uid 1000); entries: (id, class, tty)
    return json.dumps([{"session": s, "uid": 1000, "user": "user1", "seat": tty and "seat0", "leader": 2000 + int(s),
                        "class": cls, "tty": tty, "idle": False, "since": None} for s, cls, tty in entries])


def test_linger_report_reads_captures() -> None:
    from tests.runner.test_a6_runner import Inconclusive, Unreadable, linger_report
    manager, tty, cron = ("1", "manager", None), ("2", "user", "tty1"), ("3", "background", None)
    a, b = _sessions(manager, ("5", "user", None)), _sessions(manager, ("9", "user", None))
    assert linger_report(1000, "Linger=no\n", a, b) == {
        "linger": "no", "sessions": [{"id": "1", "class": "manager"}, {"id": "5", "class": "user"}], "other_sessions": 0}
    for other in (tty, cron):
        with pytest.raises(Inconclusive) as caught:
            linger_report(1000, "Linger=no\n", _sessions(manager, other, ("5", "user", None)),
                          _sessions(manager, other, ("9", "user", None)))
        assert caught.value.report["other_sessions"] == 1
    for linger, snapshot_a, snapshot_b in (("", a, b), ("Linger=no\n", a, a), ("Linger=no\n", _sessions(manager), b)):
        with pytest.raises(Unreadable):
            linger_report(1000, linger, snapshot_a, snapshot_b)


class _Recording:  # the target's CommandRunner, recording every argv the dryrun twin gives it
    def __init__(self, inner):
        self.inner, self.sent = inner, []

    def run(self, argv, *, timeout_s):
        self.sent.append(list(argv))
        return self.inner.run(argv, timeout_s=timeout_s)


def _bare(argv):  # without the ["env", "XDG_RUNTIME_DIR=..."] prefix of the host-local twins
    return argv[2:] if argv[0] == "env" else argv


@pytest.mark.onlab
@ONLAB
def test_dryrun_never_starts() -> None:
    from flightctl.runner import DryRunSystemdUserRunner
    from tests.conformance.impl_workload_runner import SshToTarget, factory
    local = TARGET == "host-local"
    commands = _Recording(LocalCommandRunner() if local else SshToTarget(TARGET))
    dry = DryRunSystemdUserRunner(commands, clock=RealClock(), runtime_dir=f"/run/user/{os.getuid()}" if local else None)
    real, unit, started = factory(TARGET), UNIT.format(1), {}
    try:
        planned = _start(dry, 1)
        assert planned["ok"] is True and planned["dry_run"] is True and planned["invocation_id"] is None
        assert len(dry.recorded) == 1 and _bare(dry.recorded[0])[:3] == ["systemd-run", "--user", "--unit=" + unit]
        assert real.inspect(unit, None, timeout_s=10)["state"] == "absent"
        started = _start(real, 1)
        assert started["ok"] is True
        stopped = dry.stop(unit, started["invocation_id"], timeout_s=30)
        assert stopped["ok"] is True and stopped["dry_run"] is True
        assert _start(dry, 1)["ok"] is False and real.inspect(unit, "run-a6btest01", timeout_s=10)["state"] == "active"
        assert commands.sent and all(_bare(argv)[:3] == ["systemctl", "--user", "show"] or argv[0] == "cat"
                                     for argv in commands.sent)
    finally:
        if started.get("invocation_id"):
            real.stop(unit, started["invocation_id"], timeout_s=60)
