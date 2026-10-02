"""WorkloadRunner conformance (unit.schema.json). Real cases start a harmless 'sleep' unit on the
target host; no GPU is touched. Every result is validated against the v2 schema."""

import time

import pytest

from tests.contracts_v2.validation import assert_valid

from .conftest import port_params

pytestmark = pytest.mark.parametrize("kind,factory", port_params("workload_runner"))
UNIT = "flightctl-conformance-g{n}.service"


def _runner(kind, factory):
    from . import registry
    return factory(registry.target())


def test_never_started_unit_is_absent_with_empty_cgroup(kind, factory) -> None:
    runner = _runner(kind, factory)
    obs = runner.inspect(UNIT.format(n=901), None, timeout_s=10)
    assert_valid(obs, "unit", "observation")
    assert obs["state"] == "absent" and obs["cgroup_empty"] is True and obs["cgroup_pids"] == []


def test_stop_of_absent_unit_is_idempotent_success(kind, factory) -> None:
    runner = _runner(kind, factory)
    for _ in range(2):
        result = runner.stop(UNIT.format(n=902), None, timeout_s=30)
        assert_valid(result, "unit", "stop_result")
        assert result["ok"] is True


@pytest.mark.realtime
def test_start_inspect_stop_roundtrip_with_invocation_identity(kind, factory) -> None:
    runner = _runner(kind, factory)
    unit = UNIT.format(n=903)
    started = runner.start(unit, "run-conform903", ["sleep", "300"], work_id="job-conform01", lease_id="lse-conform01", lane_id=runner.test_lane, parent_lease_id=None, env={}, run_as="", workdir="", cards=[], grace_s=5, timeout_s=30)
    assert_valid(started, "unit", "start_result")
    if kind == "dryrun":
        assert started["dry_run"] is True and runner.inspect(unit, None, timeout_s=10)["state"] == "absent"
        return
    assert started["ok"] and started["invocation_id"]
    obs = runner.inspect(unit, "run-conform903", timeout_s=10)
    assert obs["state"] == "active" and obs["cgroup_empty"] is False and obs["run_id"] == "run-conform903"
    stopped = runner.stop(unit, started["invocation_id"], timeout_s=60)
    assert stopped["ok"] and stopped["observation"]["cgroup_empty"] is True


@pytest.mark.not_dryrun
@pytest.mark.realtime
def test_stop_with_wrong_invocation_is_refused_and_unit_keeps_running(kind, factory) -> None:
    runner = _runner(kind, factory)
    unit = UNIT.format(n=904)
    started = runner.start(unit, "run-conform904", ["sleep", "300"], work_id="job-conform01", lease_id="lse-conform01", lane_id=runner.test_lane, parent_lease_id=None, env={}, run_as="", workdir="", cards=[], grace_s=5, timeout_s=30)
    try:
        refused = runner.stop(unit, "0" * 32, timeout_s=30)
        assert refused["ok"] is False and refused["error"]["code"] == "identity_mismatch"
        assert runner.inspect(unit, "run-conform904", timeout_s=10)["state"] == "active"
    finally:
        runner.stop(unit, started["invocation_id"], timeout_s=60)


@pytest.mark.not_dryrun
@pytest.mark.realtime
def test_units_are_isolated_and_crash_is_observed(kind, factory) -> None:
    runner = _runner(kind, factory)
    a, b = UNIT.format(n=905), UNIT.format(n=906)
    sa = runner.start(a, "run-conform905", ["sleep", "300"], work_id="job-conform01", lease_id="lse-conform01", lane_id=runner.test_lane, parent_lease_id=None, env={}, run_as="", workdir="", cards=[], grace_s=5, timeout_s=30)
    sb = runner.start(b, "run-conform906", ["sh", "-c", "exit 3"], work_id="job-conform01", lease_id="lse-conform01", lane_id=runner.test_lane, parent_lease_id=None, env={}, run_as="", workdir="", cards=[], grace_s=5, timeout_s=30)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and runner.inspect(b, "run-conform906", timeout_s=10)["state"] in {"active", "starting"}:
            time.sleep(0.5)
        ob = runner.inspect(b, "run-conform906", timeout_s=10)
        assert ob["state"] in {"failed", "absent", "inactive"} and ob["cgroup_empty"] is True
        assert runner.inspect(a, "run-conform905", timeout_s=10)["state"] == "active", "stopping/crashing B must not touch A"
    finally:
        runner.stop(a, sa["invocation_id"], timeout_s=60)
        if sb.get("invocation_id"):
            runner.stop(b, sb["invocation_id"], timeout_s=60)


@pytest.mark.fake_only
def test_unscripted_or_unparsable_result_is_unknown_not_success(kind, factory) -> None:
    runner = _runner(kind, factory)
    runner.script_next("inspect", "garbage")  # FakeRunner API owed by slice A6
    obs = runner.inspect(UNIT.format(n=907), None, timeout_s=10)
    assert obs["state"] == "unknown" and obs["cgroup_empty"] is None and obs["error"]["code"] in {"unknown", "probe_failed"}
