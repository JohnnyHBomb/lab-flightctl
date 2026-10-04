"""Round 5 regression tests: each reproduces a Sol 6 round-4 counterexample (REVIEW-sol6-r4.md) and shows the
corrected contract, oracle or gate rejects it."""

import copy
import json
import os
from pathlib import Path

import pytest

from tools import migration_gate

from .validation import (
    SplitStore,
    assert_invalid,
    assert_valid,
    auth_decision,
    examples,
    executor_reply,
    grant_after_reserve,
    helper_claim,
    helper_reconcile,
    helper_release,
    sudoers_audit,
)

ROOT = Path(__file__).parents[2]


def first_valid(name: str, index: int = 0) -> dict:
    return copy.deepcopy(examples(name)["valid"][index])


# --- 1. B5: helper commands carry the parent lease; claim lifecycle

def test_helper_contract_carries_parent_lease_and_lifecycle() -> None:
    sub = json.loads((ROOT / "contracts/v2/helper-config.schema.json").read_text(encoding="utf-8"))["x-subcommands"]
    assert "--parent-lease" in sub["session-open"] and "--parent-lease" in sub["unit-start"]
    assert "any active item" not in json.dumps(sub)  # Sol 6 r4: the old wording refused the permitted child
    assert {"claim-release", "claim-reconcile"} <= set(sub)


def test_parent_claim_lifecycle(tmp_path: Path) -> None:
    state = str(tmp_path)
    proof = {"user_slice_empty": True, "occupancy_empty": True, "key_removed": True}
    assert helper_claim(state, "fc-frienda", "lse-0000100")            # parent claimed (e.g. session-open)
    assert helper_claim(state, "fc-frienda", "lse-0000100")            # its child (unit-start) is accepted
    assert not helper_claim(state, "fc-frienda", "lse-0000200")        # another parent refused
    assert not helper_release(state, "fc-frienda", "lse-0000200", proof)  # wrong lease cannot release
    assert not helper_release(state, "fc-frienda", "lse-0000100", dict(proof, user_slice_empty=False))  # unproven close
    assert helper_release(state, "fc-frienda", "lse-0000100", proof)
    assert helper_claim(state, "fc-frienda", "lse-0000200")            # next parent after a proven close


def test_stale_claims_reconciled_from_authority_never_by_reboot_alone(tmp_path: Path) -> None:
    state = str(tmp_path)
    helper_claim(state, "fc-frienda", "lse-0000100", boot_id="boot-1")
    helper_claim(state, "fc-friendb", "lse-0000300", boot_id="boot-1")
    helper_claim(state, "fc-friendc", "lse-0000400", boot_id="boot-1")
    # reboot: boot id changes, but nothing is freed until the authority says which parents are active
    assert not helper_claim(state, "fc-frienda", "lse-0000999", boot_id="boot-2")
    outcome = helper_reconcile(state, {"fc-frienda": "lse-0000100", "fc-friendc": "lse-0000555"}, current_boot_id="boot-2")
    # round 6 (Sol 6 r5): an omitted claim is quarantined and alerted, never freed by reconcile
    assert outcome == {"fc-frienda": "kept", "fc-friendb": "orphaned-quarantined", "fc-friendc": "conflict"}
    assert not helper_claim(state, "fc-friendb", "lse-0000777", boot_id="boot-2")


# --- 2. agent isolation: per-job DynamicUser instead of a shared UID

def test_agent_isolation_is_dynamic_user_per_job_and_fc_svc_is_gone() -> None:
    cfg = first_valid("helper-config")
    assert cfg["agent_isolation"] == "dynamic-user" and "service_account" not in cfg
    shared = dict(cfg, service_account="fc-svc")
    assert_invalid(shared, "helper-config")
    sub = json.loads((ROOT / "contracts/v2/helper-config.schema.json").read_text(encoding="utf-8"))["x-subcommands"]["unit-start"]
    for prop in ("DynamicUser=yes", "StateDirectory=flightctl-jobs/<job>", "StateDirectoryMode=0700"):
        assert prop in sub
    slices = (ROOT / "docs/v2/SLICES.md").read_text(encoding="utf-8")
    assert "test_agent_jobs_cannot_read_each_others_staging" in slices  # the real-host proof (owner-installed helper)
    assert "`fc-svc` runs agents" not in slices


# --- 3. split replay store: crash ordering and restore without the replay store

def test_split_store_crash_between_commits_leaves_no_grant(tmp_path: Path) -> None:
    s = SplitStore(str(tmp_path / "main.db"), str(tmp_path / "replay.db"))
    with pytest.raises(RuntimeError):
        s.grant("req-1", "lane-1", "lse-1", "t" * 43, deadline=100.0, principal="pa", fingerprint="a" * 64, now=0.0, crash_between=True)
    assert s.active_leases("lane-1") == 0
    assert s.recover() == 1  # the orphan replay row is deleted at startup
    assert s.grant("req-1", "lane-1", "lse-1", "t" * 43, deadline=100.0, principal="pa", fingerprint="a" * 64, now=0.0)["status"] == 200
    s.close()


def test_restore_without_replay_store_answers_replay_unavailable_never_double_grant(tmp_path: Path) -> None:
    main, replay = tmp_path / "main.db", tmp_path / "replay.db"
    s = SplitStore(str(main), str(replay))
    first = s.grant("req-1", "lane-1", "lse-1", "t" * 43, deadline=1_000.0, principal="pa", fingerprint="a" * 64, now=0.0)
    assert first["status"] == 200
    backup = tmp_path / "backup.db"
    s.main.execute(f"VACUUM INTO '{backup}'")  # backup taken inside the replay window
    s.close()
    os.replace(backup, main)                    # restore the main DB ...
    os.unlink(replay)                           # ... the replay store is never backed up, so it is absent
    for extra in ("-wal", "-shm"):
        p = Path(f"{replay}{extra}")
        if p.exists():
            p.unlink()
    r = SplitStore(str(main), str(replay))
    again = r.replay_or_record("req-1", principal="pa", fingerprint="a" * 64, now=10.0)  # retry inside the window
    assert again == {"status": 409, "code": "replay_unavailable", "lease_id": "lse-1"}
    assert r.grant("req-2", "lane-1", "lse-2", "u" * 43, deadline=1_000.0, principal="pa", fingerprint="a" * 64, now=0.0) == {"status": 409, "code": "busy"}  # no second grant
    assert r.active_leases("lane-1") == 1
    r.close_after_proof("lse-1")                # holder-lost path: stop + emptiness proof
    assert r.grant("req-2", "lane-1", "lse-2", "u" * 43, deadline=1_000.0, principal="pa", fingerprint="a" * 64, now=0.0)["status"] == 200
    r.close()
    err = {"schema": 2, "request_id": "req-1", "status": 409, "data": None,
           "error": {"code": "replay_unavailable", "message": "grant token cannot be replayed after restore; acquire again", "retryable": False, "failure_class": "state", "cause": None, "details": None}}
    assert_valid(err, "rpc-envelope")


# --- 4. B6: Sol's fractional-second timestamp

ACCOUNTS = [{"principal_id": "review-agent", "enabled": True, "external_ids": [], "allowed_peers": [{"kind": "tailnet-node", "value": "host-0.example"}]}]
PEER = [{"kind": "tailnet-node", "value": "host-0.example"}]


@pytest.mark.parametrize("now,expires,ok", [
    ("2026-10-02T12:00:00.9Z", "2026-10-02T12:00:00Z", False),   # Sol 6 r4: text comparison said allow
    ("2026-10-02T12:00:00Z", "2026-10-02T12:00:00.5Z", True),
    ("2026-10-02T11:59:59.999999Z", "2026-10-02T12:00:00Z", True),
    ("2026-10-02T12:00:00.000Z", "2026-10-02T12:00:00Z", False),  # equal instants, different spellings
])
def test_token_expiry_compares_parsed_instants(now, expires, ok) -> None:
    token = {"principal_id": "review-agent", "revoked_at": None, "expires_at": expires}
    assert (auth_decision(PEER, ACCOUNTS, token, now)[0] == "allow") is ok


# --- 5. B7: replacement must be collected in the packet's own test files (or named exactly)

ROWS = [{"v1_test": "tests/authority/test_revision2.py::test_x", "packet": "A8", "action": "rewrite", "replacement": "test_restart_reconciles_lane_by_inspect", "reason": "-"},
        {"v1_test": "tests/client/test_vectors.py", "packet": "A5b2", "action": "rewrite", "replacement": "tests/authority/test_renew.py::test_renew_rolls_within_ceiling_and_refuses_past_it", "reason": "-"}]


def test_gate_rejects_replacement_found_only_in_an_unrelated_file() -> None:
    touched = {"tests/authority/test_revision2.py": "M"}
    collected = {"tests/other/test_unrelated.py::test_restart_reconciles_lane_by_inspect"}
    assert migration_gate.check(touched, collected, ROWS, "A8", {"tests/authority/test_revision2.py"})  # Sol 6 r4 probe
    collected.add("tests/authority/test_revision2.py::test_restart_reconciles_lane_by_inspect")
    assert migration_gate.check(touched, collected, ROWS, "A8", {"tests/authority/test_revision2.py"}) == []


def test_gate_explicit_node_mapping_must_match_exactly() -> None:
    touched = {"tests/client/test_vectors.py": "M"}
    wrong = {"tests/other/test_renew.py::test_renew_rolls_within_ceiling_and_refuses_past_it"}
    assert migration_gate.check(touched, wrong, ROWS, "A5b2", set())
    right = {"tests/authority/test_renew.py::test_renew_rolls_within_ceiling_and_refuses_past_it[case0]"}
    assert migration_gate.check(touched, right, ROWS, "A5b2", set()) == []


# --- 6. N1: an acknowledgement must be bound to this reserve and host

def test_unbound_acknowledgement_never_grants() -> None:
    expect = {"lease_id": "lse-0000100", "generation": 7, "host_id": "host-1", "lane_id": "lane-gpu1", "request_ids": {"creq-1"}}
    args = {"lease_id": "lse-0000100", "generation": 7, "host_id": "host-1", "controller_request_id": "creq-1"}
    good = {"received_t": 1.0, "reply": executor_reply(remaining=100.0, **args)}
    assert grant_after_reserve([good], 1_000.0, expect) == "grant"
    bare = {"received_t": 1.0, "reply": {"max_end_remaining_s": 100.0}}  # Sol 6 r4: no identity (not a valid reply)
    assert grant_after_reserve([bare], 1_000.0, expect) == "pending ceiling-unconfirmed"
    for field, value in (("lease_id", "lse-0000999"), ("generation", 8), ("host_id", "host-2"), ("controller_request_id", "creq-9")):
        wrong = {"received_t": 1.0, "reply": executor_reply(remaining=100.0, **dict(args, **{field: value}))}
        assert grant_after_reserve([wrong], 1_000.0, expect) == "pending ceiling-unconfirmed", field
    assert grant_after_reserve([good], 1_000.0, None) == "pending ceiling-unconfirmed"  # no expectation, no grant


# --- 7. sudo audit: required Defaults must be present

HELPER_ONLY = ("Matching Defaults entries for runner on host-a:\n    env_reset, !setenv, secure_path=/usr/sbin\\:/usr/bin\\:/sbin\\:/bin\n\n"
               "User runner may run the following commands on host-a:\n    (root) NOPASSWD: /usr/local/libexec/flightctl-helper\n")


@pytest.mark.parametrize("drop", ["env_reset, ", "!setenv, ", ", secure_path=/usr/sbin\\:/usr/bin\\:/sbin\\:/bin"])
def test_sudo_audit_requires_named_defaults(drop: str) -> None:
    assert sudoers_audit(HELPER_ONLY, "runner") == []
    assert sudoers_audit(HELPER_ONLY.replace(drop, "", 1), "runner")  # Sol 6 r4: missing env_reset returned []


# --- B2 note: expected UUIDs from confirmed inventory is an explicit acceptance test (A4u since Amendment 5)

def test_a3_names_the_inventory_binding_test() -> None:
    text = (ROOT / "docs/v2/SLICES.md").read_text(encoding="utf-8")
    a4u = next(line for line in text.splitlines() if line.startswith("- ACCEPTANCE:") and "test_config_loader_rejects_hash_mismatch" in line)
    assert "test_expected_uuids_come_from_confirmed_inventory" in a4u
