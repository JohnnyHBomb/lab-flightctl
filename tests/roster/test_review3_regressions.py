from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.roster.test_roster_scripts import (
    ROOT, events_named, fresh_plan, one_batch, queue_response, run_queue,
    run_script, state_value,
)


@pytest.mark.parametrize("field", ["status", "state", "reservation", "code", "failure_class"])
def test_malformed_registration_enum_exits_unavailable(tmp_path: Path, field: str) -> None:
    response = queue_response()
    if field == "status":
        response["status"] = []
    elif field == "state":
        response["data"]["state"] = []
    elif field == "reservation":
        response["data"]["reservation"]["state"] = []
    else:
        response.update(status=403, data=None, error={
            "code": "denied", "message": "denied", "retryable": False,
            "failure_class": "policy",
        })
        response["error"][field] = []
    result = run_queue(one_batch(), tmp_path, ROSTER_BRIDGE_OUTCOMES=json.dumps([{"response": response}]))
    assert result.returncode == 3, result.stderr
    assert result.stdout == ""
    assert "Traceback" not in result.stderr
    assert not events_named(tmp_path, "client-argv")


@pytest.mark.parametrize("field", ["unit", "invocation", "lease_id"])
def test_schema_invalid_grant_never_starts_workload(tmp_path: Path, field: str) -> None:
    result = run_script(
        ROOT / "roster/run_arm.sh", ["lane-a", "purpose", "--", "workload"], tmp_path,
        ROSTER_LIFECYCLE_PLAN=json.dumps({"acquire": [{"outcome": "success", field: ""}]}),
    )
    assert result.returncode == 3, result.stderr
    assert not events_named(tmp_path, "workload")
    assert not events_named(tmp_path, "cleanup")
    assert not events_named(tmp_path, "release")


@pytest.mark.parametrize("release_outcome", ["success", "release-unknown"])
def test_replayed_arm_never_repeats_workload(tmp_path: Path, release_outcome: str) -> None:
    plan = fresh_plan() | {"release": [{"outcome": release_outcome}]}
    args = ["lane-a", "purpose", "--", "workload"]
    first = run_script(ROOT / "roster/run_arm.sh", args, tmp_path, ROSTER_LIFECYCLE_PLAN=json.dumps(plan))
    assert first.returncode == (0 if release_outcome == "success" else 3)
    before = state_value(tmp_path)["lanes"]["lane-a"]
    second = run_script(ROOT / "roster/run_arm.sh", args, tmp_path, ROSTER_LIFECYCLE_PLAN=json.dumps(plan))
    assert second.returncode == 3
    assert len(events_named(tmp_path, "workload")) == 1
    assert state_value(tmp_path)["lanes"]["lane-a"] == before


def test_delayed_cleanup_rechecks_durable_successor_identity(tmp_path: Path) -> None:
    hook = tmp_path / "replace-reservation.py"
    hook.write_text('''#!/usr/bin/env python3
import json, os
from pathlib import Path
path = Path(os.environ["ROSTER_LIFECYCLE_STATE_FILE"])
state = json.loads(path.read_text())
state["lanes"]["lane-a"].update(
    state="running", generation=8, token="token-successor-abcdefghijkl",
    unit="unit-successor", invocation="invocation-successor",
    occupants=["pid-successor"],
)
path.write_text(json.dumps(state))
path.with_name("successor.json").write_text(json.dumps(state["lanes"]["lane-a"]))
''', encoding="utf-8")
    hook.chmod(0o755)
    result = run_script(
        ROOT / "roster/run_arm.sh", ["lane-a", "purpose", "--", "workload"], tmp_path,
        ROSTER_LIFECYCLE_PLAN=json.dumps(fresh_plan()), ROSTER_CLEANUP_HOOK=str(hook),
    )
    assert result.returncode == 3
    assert not events_named(tmp_path, "cleanup")
    assert not events_named(tmp_path, "release")
    assert state_value(tmp_path)["lanes"]["lane-a"] == json.loads((tmp_path / "successor.json").read_text())


@pytest.mark.parametrize("phase", ["loading", "gate", "workload"])
def test_runtime_phases_consume_forwarded_budget(tmp_path: Path, phase: str) -> None:
    plan = fresh_plan() | {phase: [{"outcome": "success", "duration_s": 61}]}
    result = run_script(
        ROOT / "roster/run_arm.sh",
        ["lane-a", "purpose", "--est", "1", "--max", "1", "--", "workload"], tmp_path,
        ROSTER_LIFECYCLE_PLAN=json.dumps(plan),
        ROSTER_WORKLOAD_HOOK=str(ROOT / "tests/roster/fake_workload.py"),
    )
    assert result.returncode == 3
    assert state_value(tmp_path)["clock_monotonic_s"] == 60
    assert events_named(tmp_path, "cleanup")
    assert events_named(tmp_path, "release")
    assert not events_named(tmp_path, "injected-workload")


def test_cleanup_poll_requires_clock_progress(tmp_path: Path) -> None:
    plan = fresh_plan() | {"cleanup": [
        {"outcome": "pending", "occupants": ["pid-owned"], "advance_s": 0},
        {"outcome": "success", "occupants": []},
    ]}
    result = run_script(
        ROOT / "roster/run_arm.sh", ["lane-a", "purpose", "--", "workload"], tmp_path,
        ROSTER_LIFECYCLE_PLAN=json.dumps(plan),
    )
    assert result.returncode == 3
    assert state_value(tmp_path)["lanes"]["lane-a"]["state"] == "quarantined"
    assert not events_named(tmp_path, "release")
