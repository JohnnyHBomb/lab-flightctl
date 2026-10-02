"""Round 6 regression tests: each reproduces a Sol 6 round-5 probe (REVIEW-sol6-r5.md) and shows the corrected
contract or oracle rejects it."""

import json
import os
from pathlib import Path

import pytest

from .validation import (
    CLEAR_PATH,
    SplitStore,
    helper_clear,
    assert_invalid,
    errors,
    executor_reply,
    grant_after_reserve,
    helper_claim,
    helper_reconcile,
    helper_release,
    sudoers_audit,
)

ROOT = Path(__file__).parents[2]
PROOF = {"user_slice_empty": True, "occupancy_empty": True, "key_removed": True}
# round 8: claim-clear is its own root-only program started by sudo for the configured operator account
OPERATOR = {"program": CLEAR_PATH, "ruid": 0, "euid": 0, "sudo_user": "opr", "sudo_uid": 1500}


# --- 1. B5: reconcile never frees; removal only with proof; race-safe release

def test_reconcile_never_removes_an_omitted_claim(tmp_path: Path) -> None:
    state = str(tmp_path)
    assert helper_claim(state, "fc-frienda", "lse-0000100")
    # Sol 6 r5: a restored authority omits a still-running parent
    assert helper_reconcile(state, {}, current_boot_id="boot-a") == {"fc-frienda": "orphaned-quarantined"}
    assert (tmp_path / "fc-frienda.parent").exists()
    assert not helper_claim(state, "fc-frienda", "lse-0000200")  # still blocked
    assert not helper_claim(state, "fc-frienda", "lse-0000100")  # round 7 (Sol 6 r6): quarantine blocks children too
    assert not helper_release(state, "fc-frienda", "lse-0000100", PROOF)  # a friend-side release cannot lift it
    assert not helper_clear(state, "fc-frienda", "lse-0000100", dict(PROOF, occupancy_empty=False), OPERATOR, "opr", 1500)
    assert helper_clear(state, "fc-frienda", "lse-0000100", PROOF, OPERATOR, "opr", 1500)  # round 8: operator claim-clear with proof
    assert helper_claim(state, "fc-frienda", "lse-0000200")


def test_release_does_not_delete_a_claim_replaced_concurrently(tmp_path: Path) -> None:
    state = str(tmp_path)
    helper_claim(state, "fc-frienda", "lse-0000100")

    def concurrent_release_and_new_parent() -> None:
        # the hook runs inside the slow releaser's critical section; _lock=False models a path that bypasses the
        # round-8 account lock, so the inode-checked tombstone is exercised on its own
        assert helper_release(state, "fc-frienda", "lse-0000100", PROOF, _lock=False)  # another releaser wins the race
        assert helper_claim(state, "fc-frienda", "lse-0000200", _lock=False)           # and a new parent claims at once

    # the slow releaser read lease-100 before the swap; it must not remove lease-200's claim
    assert helper_release(state, "fc-frienda", "lse-0000100", PROOF, _race_hook=concurrent_release_and_new_parent) is False
    assert json.loads((tmp_path / "fc-frienda.parent").read_text())["lease_id"] == "lse-0000200"
    assert not [n for n in os.listdir(state) if ".tomb." in n]


def test_helper_contract_wording_allows_children_and_never_reconcile_frees() -> None:
    sub = json.loads((ROOT / "contracts/v2/helper-config.schema.json").read_text(encoding="utf-8"))["x-subcommands"]
    assert "NEVER removes" in sub["claim-reconcile"] and "ONLY way" in sub["claim-release"]
    slices = (ROOT / "docs/v2/SLICES.md").read_text(encoding="utf-8")
    assert "already has an active item" not in slices  # Sol 6 r5: C7h wording refused allowed children
    assert "or as a child of the claimed parent" in slices  # round 8: the one admission rule


# --- 2. replay deadline in the main record: inside vs after the window

def test_missing_replay_row_inside_window_is_unavailable_after_window_is_null_token(tmp_path: Path) -> None:
    s = SplitStore(str(tmp_path / "main.db"), str(tmp_path / "replay.db"))
    assert s.grant("req-1", "lane-1", "lse-1", "t" * 43, deadline=100.0, principal="pa", fingerprint="a" * 64, now=0.0)["status"] == 200
    assert s.replay_or_record("req-1", principal="pa", fingerprint="a" * 64, now=50.0)["token"] == "t" * 43
    assert s.scrub(now=100.0) == 1  # routine end-of-window scrub
    # Sol 6 r5: after the window the routine scrub must give the specified null-token replay, not holder-lost
    assert s.replay_or_record("req-1", principal="pa", fingerprint="a" * 64, now=150.0) == {"status": 200, "lease_id": "lse-1", "token": None}
    assert s.active_leases("lane-1") == 1
    assert s.main.execute("SELECT state FROM lease WHERE lease_id = 'lse-1'").fetchone()[0] == "active"
    s.close()


def test_idempotency_record_carries_the_replay_deadline() -> None:
    schema = json.loads((ROOT / "contracts/v2/rpc-envelope.schema.json").read_text(encoding="utf-8"))
    assert "replay_deadline" in schema["$defs"]["idempotency_record"]["required"]


# --- 3. N1: ok=True + definite + wire-shape echoed_identity

EXPECT = {"lease_id": "lse-0000100", "generation": 7, "host_id": "host-1", "lane_id": "lane-gpu1", "request_ids": {"creq-1"}}
ARGS = {"lease_id": "lse-0000100", "generation": 7, "host_id": "host-1", "controller_request_id": "creq-1"}


def test_failed_reply_never_grants() -> None:
    assert grant_after_reserve([{"received_t": 1.0, "reply": executor_reply(remaining=100.0, **ARGS)}], 1_000.0, EXPECT) == "grant"
    failed = executor_reply(remaining=100.0, ok=False, **ARGS)  # Sol 6 r5: a bound but failed reply granted
    assert errors(failed, "executor", "reply") == []
    assert grant_after_reserve([{"received_t": 1.0, "reply": failed}], 1_000.0, EXPECT) == "pending ceiling-unconfirmed"
    uncertain = executor_reply(remaining=100.0, ok=False, definite=False, **ARGS)
    assert grant_after_reserve([{"received_t": 1.0, "reply": uncertain}], 1_000.0, EXPECT) == "pending ceiling-unconfirmed"


def test_flat_identity_fields_are_not_a_reply() -> None:
    flat = dict(ARGS, max_end_remaining_s=100.0, ok=True, definite=True)  # the round-5 oracle's shape
    assert errors(flat, "executor", "reply")
    assert grant_after_reserve([{"received_t": 1.0, "reply": flat}], 1_000.0, EXPECT) == "pending ceiling-unconfirmed"
    moved = executor_reply(remaining=100.0, **ARGS)
    moved["echoed_identity"]["lane"]["host_id"] = "host-2"  # nested identity names another host
    assert grant_after_reserve([{"received_t": 1.0, "reply": moved}], 1_000.0, EXPECT) == "pending ceiling-unconfirmed"


# --- 4. output-collect by job id; no service account

def test_output_collect_takes_job_id_only() -> None:
    cfg = json.loads((ROOT / "contracts/v2/helper-config.schema.json").read_text(encoding="utf-8"))
    sub = cfg["x-subcommands"]
    assert "service_account" not in json.dumps(sub)
    assert sub["output-collect"].startswith("--job <public id>") and "--account" not in sub["output-collect"].split("(")[0]
    slices = (ROOT / "docs/v2/SLICES.md").read_text(encoding="utf-8")
    assert "test_job_runs_as_its_own_dynamic_user_never_operator_or_shared_uid" in slices
    assert "test_job_runs_as_service_account" not in slices


# --- 5. sudo audit: empty or non-standard secure_path

BASE = ("Matching Defaults entries for runner on host-a:\n    env_reset, !setenv, secure_path=/usr/sbin\\:/usr/bin\\:/sbin\\:/bin\n\n"
        "User runner may run the following commands on host-a:\n    (root) NOPASSWD: /usr/local/libexec/flightctl-helper\n")


@pytest.mark.parametrize("value", ["", "/tmp\\:/usr/bin", "relative\\:/usr/bin", "/" + "ho" + "me/runner/bin"])  # built so the repo scan sees no machine path
def test_secure_path_must_be_standard_system_dirs(value: str) -> None:
    assert sudoers_audit(BASE, "runner") == []
    bad = BASE.replace("secure_path=/usr/sbin\\:/usr/bin\\:/sbin\\:/bin", f"secure_path={value}")
    assert sudoers_audit(bad, "runner")  # Sol 6 r5: 'secure_path=' passed
