"""Discriminating mutations run only in disposable copies of the package."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest


MUTANTS = [
    ("closed-generation", "if generation <= max(previous_generation, closed_generation):", "if False:", "test_reserve_start_fence"),
    ("invocation", "dict(saved) == dict(identity)", "{k: v for k, v in saved.items() if k != 'invocation'} == {k: v for k, v in identity.items() if k != 'invocation'}", "test_pid_reuse_and_invocation"),
    ("unknown-success", '_KNOWN_SUCCESS_STATUSES = {"ok", "success"}', '_KNOWN_SUCCESS_STATUSES = {"ok", "success", "unrecognised-status"}', "test_cleanup_rejects_unknown_status_and_malformed_occupants"),
    ("unchecked-release", "if stop_ok and inspect_ok and gpu_ok:", "if stop_ok:", "test_cleanup_release_waits_for_independent_probes"),
    ("protected-kill", "if protected:", "if False:", "test_deadlines_controller_loss_and_grace"),
    ("preemptible-threshold", "preemptible_stale_s = 180.0", "preemptible_stale_s = 1.0", "test_deadline_enforcement_calls_before_and_at_boundaries"),
    ("service-threshold", "service_grace_s = 120.0", "service_grace_s = 1.0", "test_deadline_enforcement_calls_before_and_at_boundaries"),
    ("standby-threshold", "standby_grace_s = 300.0", "standby_grace_s = 1.0", "test_deadline_enforcement_calls_before_and_at_boundaries"),
    ("clock-freeze", 'return bool(isinstance(clock, dict) and clock.get("frozen"))', "return False", "test_clock_skew_freezes_free_lane_admission"),
    ("approval-reference", "return approved is not None and approval_id in set(approved)", "return True", "test_protected_stop_authority"),
    ("reboot-anchor", 'deadline["utc_anchor"] = _utc_text(now[0])', "pass", "test_repeated_reboots_preserve_absolute_deadline"),
    ("pending-deadline-start", 'if current.get("start_in_flight") or generation_key in self._inflight_starts:', "if False:", "test_deadline_stop_serializes_pending_start"),
    ("timed-out-start", "if self._mark_start_timeout(generation_key, identity):", "if False:", "test_timed_out_start_retains_fence_until_worker_returns"),
]


@pytest.mark.parametrize("name,old,new,target", MUTANTS, ids=[item[0] for item in MUTANTS])
def test_executor_mutations(tmp_path, name, old, new, target):
    root = Path(__file__).resolve().parents[2]
    for directory in ("flightctl", "contracts", "tests"):
        shutil.copytree(root / directory, tmp_path / directory, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    source_path = tmp_path / "flightctl" / "executor.py"
    source = source_path.read_text()
    assert source.count(old) == 1, f"{name}: mutation target changed"
    source_path.write_text(source.replace(old, new))
    result = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", "-q", "-o", "addopts=", f"tests/executor/test_executor.py::{target}"],
        cwd=tmp_path, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 1, f"{name}: mutation survived or test did not run\n{result.stdout}\n{result.stderr}"
    assert "1 failed" in result.stdout and "AssertionError" in result.stdout, result.stdout
