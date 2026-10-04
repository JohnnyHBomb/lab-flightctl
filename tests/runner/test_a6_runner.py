"""A6 named acceptance tests: the WorkloadRunner real twin (SystemdUserRunner) and the linger probe."""

import json
import os
import time

import pytest

from flightctl.runner import SystemdUserRunner
from tests.contracts_v2.validation import assert_valid
from tests.fakes.clock import FakeClock

TARGET = os.environ.get("FLIGHTCTL_CONFORMANCE_TARGET") or None
ONLAB = pytest.mark.skipif(TARGET is None, reason="on-lab: set FLIGHTCTL_CONFORMANCE_TARGET to a real host")
UNIT = "flightctl-lane1-g7.service"
INV = "0123456789abcdef0123456789abcdef"
CG = "/user.slice/user-1000.slice/user@1000.service/app.slice/" + UNIT
CARD = "GPU-00000000-0000-0000-0000-000000000011"
PREFIX = ["env", "XDG_RUNTIME_DIR=/run/user/1000"]
PROPS = "--property=LoadState,ActiveState,SubState,Result,InvocationID,ControlGroup,MainPID,ExecMainStatus,Environment"


def show(load="loaded", active="active", sub="running", result="success", inv=INV, cg=CG, pid="4242", status="0",
         env='FLIGHTCTL_RUN_ID=run-a6test01 "X=a b \\"q\\"" CUDA_VISIBLE_DEVICES=' + CARD):
    return (f"Result={result}\nMainPID={pid}\nExecMainStatus={status}\nLoadState={load}\nActiveState={active}\n"
            f"SubState={sub}\nInvocationID={inv}\nControlGroup={cg}\nEnvironment={env}\n\n")


class Scripted:  # answers systemd-run, each systemctl verb and cat from a dict; records argv; unscripted exits 1
    def __init__(self, script):
        self.script, self.calls = script, []

    def run(self, argv, *, timeout_s):
        self.calls.append(list(argv))
        args = argv[2:] if argv[0] == "env" else argv
        key = args[0] if args[0] != "systemctl" else "systemctl " + args[2]
        rc, out = self.script.get(key, (1, ""))
        return {"argv": list(argv), "host_id": None, "returncode": rc, "stdout": out, "stderr": "", "timed_out": False,
                "duration_s": 0.0, "error": None}


def twin(script):
    commands = Scripted(script)
    return SystemdUserRunner(commands, clock=FakeClock(), runtime_dir="/run/user/1000"), commands


def test_real_twin_reads_systemd_captures() -> None:
    runner, commands = twin({"systemd-run": (0, "Running as unit: x\n"), "systemctl show": (0, show()), "cat": (0, "4243\n4242\n")})
    started = runner.start(UNIT, "run-a6test01", ["sleep", "300"], work_id="job-a6test01", lease_id="lse-a6test01",
                           lane_id="lane1", parent_lease_id=None, env={"B": "2", "A": "x y"}, run_as="", workdir="",
                           cards=[CARD], grace_s=5, timeout_s=30)
    assert_valid(started, "unit", "start_result")
    assert started["ok"] is True and started["invocation_id"] == INV and started["error"] is None
    obs = started["observation"]
    assert obs["state"] == "active" and obs["run_id"] == "run-a6test01" and obs["cgroup_pids"] == [4242, 4243]
    assert obs["cgroup_empty"] is False and obs["observed_at"] == "2026-09-27T20:00:00Z"
    assert commands.calls == [
        PREFIX + ["systemd-run", "--user", "--unit=" + UNIT, "--collect", "--property=KillMode=control-group",
                  "--property=TimeoutStopSec=5", "--setenv=FLIGHTCTL_RUN_ID=run-a6test01",
                  "--setenv=CUDA_VISIBLE_DEVICES=" + CARD, "--setenv=A=x y", "--setenv=B=2", "--", "sleep", "300"],
        PREFIX + ["systemctl", "--user", "show", UNIT, PROPS],
        ["cat", "/sys/fs/cgroup" + CG + "/cgroup.procs"]]

    runner, commands = twin({"systemctl show": (0, show(active="failed", sub="failed", result="exit-code", cg="", pid="0", status="3"))})
    failed = runner.inspect(UNIT, "run-a6test01", timeout_s=10)
    assert_valid(failed, "unit", "observation")
    assert (failed["state"], failed["exit_status"], failed["cgroup"], failed["cgroup_pids"], failed["cgroup_empty"]) == ("failed", 3, None, [], True)
    assert len(commands.calls) == 1

    gone = show(load="not-found", active="inactive", sub="dead", inv="", cg="", pid="0", env="")
    runner, commands = twin({"systemctl show": (0, gone)})
    absent = runner.inspect(UNIT, "run-a6test01", timeout_s=10)
    stopped = runner.stop(UNIT, None, timeout_s=30)
    assert_valid(absent, "unit", "observation") or assert_valid(stopped, "unit", "stop_result")
    assert absent["state"] == "absent" and absent["cgroup_empty"] is True and absent["error"] is None
    assert stopped["ok"] is True and [c[4] for c in commands.calls] == ["show", "show"]

    for answer in [(0, "garbage\n"), (1, show()), (0, show(load="masked")), (0, show(inv="XYZ"))]:
        runner, _ = twin({"systemctl show": answer, "cat": (0, "4242\n")})
        unknown = runner.inspect(UNIT, None, timeout_s=10)
        stopped = runner.stop(UNIT, INV, timeout_s=30)
        assert_valid(unknown, "unit", "observation") or assert_valid(stopped, "unit", "stop_result")
        assert unknown["state"] == "unknown" and unknown["cgroup_empty"] is None and unknown["error"]["code"] in {"unknown", "probe_failed"}
        assert stopped["ok"] is False


def _onlab():
    from tests.conformance.impl_workload_runner import SshToTarget, factory
    return factory(TARGET), (None if TARGET == "host-local" else SshToTarget(TARGET))


def _start(runner, n, argv):
    return runner.start(f"flightctl-a6test-g{n}.service", f"run-a6test{n:02d}", argv, work_id="job-a6test01",
                        lease_id="lse-a6test01", lane_id="a6test", parent_lease_id=None, env={}, run_as="",
                        workdir="", cards=[], grace_s=5, timeout_s=30)


@pytest.mark.realtime
@pytest.mark.onlab
@ONLAB
def test_crash_observed_and_cgroup_empty() -> None:
    runner, _ = _onlab()
    a, b = "flightctl-a6test-g1.service", "flightctl-a6test-g2.service"
    sa, sb = _start(runner, 1, ["sleep", "300"]), _start(runner, 2, ["sleep", "not-a-number"])
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and runner.inspect(b, "run-a6test02", timeout_s=10)["state"] in {"active", "starting"}:
            time.sleep(0.5)
        ob = runner.inspect(b, "run-a6test02", timeout_s=10)
        assert ob["state"] in {"failed", "absent"} and ob["cgroup_empty"] is True and ob["cgroup_pids"] == []
        assert sa["ok"] and runner.inspect(a, "run-a6test01", timeout_s=10)["state"] == "active"
    finally:
        runner.stop(a, sa["invocation_id"], timeout_s=60)
        if sb.get("invocation_id"):
            runner.stop(b, sb["invocation_id"], timeout_s=60)


@pytest.mark.onlab
@ONLAB
@pytest.mark.skipif(TARGET == "host-local", reason="linger probe needs an ssh target: a local test cannot log out")
def test_unit_and_inhibitor_survive_logout() -> None:
    runner, ssh = _onlab()
    uid = ssh.run(["id", "-u"], timeout_s=30)["stdout"].strip()
    shown = dict(line.split("=", 1) for line in ssh.run(["loginctl", "show-user", uid, "--property=Linger,Sessions"],
                                                         timeout_s=30)["stdout"].splitlines() if "=" in line)
    units = [(3, ["sleep", "120"]), (4, ["systemd-inhibit", "--what=idle", "--mode=block", "--who=flightctl-a6",
                                         "--why=linger probe", "sleep", "120"])]
    started = []
    try:
        for n, argv in units:
            started.append((f"flightctl-a6test-g{n}.service", _start(runner, n, argv)))
        time.sleep(20)
        survived = [runner.inspect(unit, None, timeout_s=10)["state"] == "active" for unit, _ in started]
    finally:
        for unit, result in started:
            runner.stop(unit, result["invocation_id"], timeout_s=60)
    report = {"target": TARGET, "linger": shown.get("Linger"), "other_sessions": len(shown.get("Sessions", "").split()) - 1,
              "wait_s": 20, "unit_survived": survived[0], "inhibitor_survived": survived[1]}
    print("LINGER-PROBE " + json.dumps(report, sort_keys=True))
    assert report["linger"] == "yes" or report["other_sessions"] == 0, "inconclusive: " + json.dumps(report)
    assert survived == [True, True]
