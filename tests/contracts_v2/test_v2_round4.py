"""Round 4 regression tests: each reproduces a Sol 6 round-3 counterexample (REVIEW-sol6-r3.md) and shows the
corrected contract, oracle or gate rejects it."""

import copy
import json
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import pytest

from .validation import (
    admit_parent,
    assert_invalid,
    assert_valid,
    auth_decision,
    contained_open,
    examples,
    executor_reply,
    grant_after_reserve,
    helper_claim,
    occupancy_semantics,
    stage_outputs,
    sudo_l_grants,
    sudoers_audit,
    sudoers_file_audit,
    work_admission,
)

ROOT = Path(__file__).parents[2]


def first_valid(name: str, index: int = 0) -> dict:
    return copy.deepcopy(examples(name)["valid"][index])


# --- B2: Sol's zero-GPU observation and card-identity mismatches

def test_b2_zero_gpu_observation_cannot_be_empty() -> None:
    obs = first_valid("gpu-probe", 2)
    obs.update({"gpus": [], "processes": [], "tenants": [], "noise": [], "lane_memory_used_mib": 0, "unexplained_mib": 0, "empty": True})
    assert any("not exactly the lane's cards" in p for p in occupancy_semantics(obs))  # Sol's construction
    assert_invalid(obs, "gpu-probe", "occupancy_observation")  # and the schema (status ok needs >= 1 GPU)


@pytest.mark.parametrize("mutate,expect", [
    (lambda o: o["expected_uuids"].append("GPU-00000000-0000-0000-0000-000000000009"), "not exactly the lane's cards"),  # a lane card missing
    (lambda o: o["gpus"].append(dict(o["gpus"][0], uuid="GPU-00000000-0000-0000-0000-000000000009")), "not exactly the lane's cards"),  # extra card
    (lambda o: o["gpus"].append(dict(o["gpus"][0])), "not exactly the lane's cards"),  # duplicate card
    (lambda o: o["processes"][0].update(gpu_uuid="GPU-00000000-0000-0000-0000-000000000009"), "not a lane card"),
])
def test_b2_observed_cards_must_equal_expected(mutate, expect) -> None:
    obs = first_valid("gpu-probe", 2)
    assert occupancy_semantics(obs) == []
    mutate(obs)
    assert any(expect in p for p in occupancy_semantics(obs))


def test_b2_expected_uuids_required_and_non_empty() -> None:
    obs = first_valid("gpu-probe", 2)
    obs["expected_uuids"] = []
    assert_invalid(obs, "gpu-probe", "occupancy_observation")


# --- B5: parent and child work; atomic single parent; admission and helper agree under a race

def test_b5_child_of_own_parent_is_allowed_second_parent_is_not() -> None:
    parents = {("friend-a", "host-1"): "lse-0000100"}
    assert work_admission(parents, "friend-a", "host-1", "child", "lse-0000100", "human") == ("allow", None)  # Sol's case
    assert work_admission(parents, "friend-a", "host-1", "child", "lse-0000999", "human") == ("deny", "account_busy")
    assert work_admission(parents, "friend-a", "host-1", "parent", "lse-0000200", "human") == ("deny", "account_busy")
    policy = first_valid("policy")
    assert policy["work"] == {"max_active_parents_per_human_per_host": 1, "children_share_parent_slot": True}


def _contend(args: tuple[str, str, str]) -> tuple[str, bool, bool]:
    db, state, lease = args
    won = admit_parent(db, "friend-a", "host-1", lease)
    claimed = helper_claim(state, "fc-frienda", lease) if won else False
    return lease, won, claimed


def test_b5_concurrent_parents_exactly_one_wins_in_both_layers(tmp_path: Path) -> None:
    db, state = str(tmp_path / "authority.db"), str(tmp_path / "helper")
    os.mkdir(state)
    leases = [f"lse-race{i:04d}" for i in range(12)]
    with ProcessPoolExecutor(max_workers=6) as pool:  # real processes, real SQLite locking
        results = list(pool.map(_contend, [(db, state, lease) for lease in leases]))
    winners = [lease for lease, won, _ in results if won]
    assert len(winners) == 1
    assert [lease for lease, _, claimed in results if claimed] == winners
    assert helper_claim(state, "fc-frienda", winners[0]) is True  # its child (session/job) uses the same parent
    assert helper_claim(state, "fc-frienda", "lse-intruder") is False


def test_b5_helper_claim_race_without_admission_still_single(tmp_path: Path) -> None:
    state = str(tmp_path)
    barrier = threading.Barrier(16)

    def go(i: int) -> bool:
        barrier.wait()
        return helper_claim(state, "fc-frienda", f"lse-thr{i:04d}")

    with ThreadPoolExecutor(max_workers=16) as pool:
        wins = list(pool.map(go, range(16)))
    assert wins.count(True) == 1


# --- B6: Sol's mismatched-peer and expired-token cases; C2 acceptance named

ACCOUNTS = [
    {"principal_id": "friend-a", "enabled": True, "external_ids": [{"kind": "tailnet-login", "value": "friend@example"}], "allowed_peers": []},
    {"principal_id": "friend-b", "enabled": True, "external_ids": [{"kind": "tailnet-login", "value": "other@example"}], "allowed_peers": []},
    {"principal_id": "review-agent", "enabled": True, "external_ids": [], "allowed_peers": [{"kind": "tailnet-node", "value": "host-0.example"}]},
]
NOW = "2026-10-02T12:00:00Z"
AGENT_PEER = [{"kind": "tailnet-node", "value": "host-0.example"}]


def test_b6_principal_is_derived_from_the_peer_never_supplied() -> None:
    import inspect

    assert "principal" not in inspect.signature(auth_decision).parameters  # nothing to supply
    assert auth_decision([{"kind": "tailnet-login", "value": "other@example"}], ACCOUNTS, None, NOW) == ("allow", "friend-b")
    assert auth_decision([{"kind": "tailnet-login", "value": "nobody@example"}], ACCOUNTS, None, NOW) == ("deny", None)


@pytest.mark.parametrize("token,ok", [
    ({"principal_id": "review-agent", "revoked_at": None, "expires_at": "2026-11-01T00:00:00Z"}, True),
    ({"principal_id": "review-agent", "revoked_at": None, "expires_at": "2026-10-01T00:00:00Z"}, False),  # Sol: expired
    ({"principal_id": "review-agent", "revoked_at": "2026-10-02T11:00:00Z", "expires_at": "2026-11-01T00:00:00Z"}, False),
    ({"principal_id": "review-agent", "revoked_at": None, "expires_at": NOW}, False),  # expiry instant is expired
])
def test_b6_token_expiry_and_revocation_enforced(token, ok) -> None:
    assert (auth_decision(AGENT_PEER, ACCOUNTS, token, NOW)[0] == "allow") is ok


def test_b6_c2_acceptance_names_the_boundary() -> None:
    slices = (ROOT / "docs/v2/SLICES.md").read_text(encoding="utf-8")
    for name in ("test_expired_token_denied", "test_request_cannot_supply_a_principal", "test_token_from_unlisted_peer_denied_no_fallback"):
        assert name in slices


# --- B7: the migration gate verifies COLLECTED replacement tests on a real git repo

def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@example", "-c", "user.name=t", *args], cwd=cwd, check=True, capture_output=True, timeout=30)


def test_b7_gate_rejects_replacement_named_only_in_a_comment(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_old.py").write_text("def test_old():\n    assert True\n")
    (repo / "map.tsv").write_text("v1_test\tpacket\taction\treplacement\treason\ntests/test_old.py::test_old\tP1\trewrite\ttest_new_behaviour\tdefect\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / "tests" / "test_old.py").write_text("def test_old():\n    assert True\n# test_new_behaviour\n")
    _git(repo, "commit", "-qam", "comment only")
    gate = [sys.executable, str(ROOT / "tools/migration_gate.py"), "--base", "HEAD~1", "--head", "HEAD", "--packet", "P1", "--map", "map.tsv"]
    run = subprocess.run(gate, cwd=repo, capture_output=True, text=True, timeout=120)
    assert run.returncode == 1 and "not a collected test" in run.stdout  # Sol 6 r3 counterexample
    (repo / "tests" / "test_old.py").write_text("def test_new_behaviour():\n    assert True\n")
    _git(repo, "commit", "-qam", "real replacement")
    gate[3] = "HEAD~2"
    run = subprocess.run(gate, cwd=repo, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stdout


# --- N1: grant withheld until the host acknowledges the ceiling; correction dropped

def test_n1_lost_correction_withholds_the_grant() -> None:
    approved = 10_000.0
    ident = {"lease_id": "lse-0000100", "generation": 7, "host_id": "host-1"}
    expect = dict(ident, lane_id="lane-gpu1", request_ids={"creq-1", "creq-2"})
    late_reserve = {"received_t": 61.0, "reply": executor_reply(controller_request_id="creq-1", remaining=9_970.0, **ident)}  # host ceiling up to 10_031 > approved
    assert grant_after_reserve([late_reserve], approved, expect) == "pending ceiling-unconfirmed"
    assert grant_after_reserve([late_reserve, {"lost": True}], approved, expect) == "pending ceiling-unconfirmed"  # Sol r3 case
    acked = {"received_t": 70.0, "reply": executor_reply(controller_request_id="creq-2", remaining=9_900.0, kind="ceiling", **ident)}  # shorten acknowledged
    assert grant_after_reserve([late_reserve, {"lost": True}, acked], approved, expect) == "grant"
    pending = {"schema": 2, "request_id": "req-c", "status": 202, "error": None,
               "data": {"kind": "pending", "operation": "acquire", "reason": "ceiling-unconfirmed", "retry_after_s": 5, "wait_until": "2026-10-02T10:03:00Z", "queue_id": None, "wake_attempt_id": None}}
    assert_valid(pending, "rpc-envelope")


def test_n1_reply_must_carry_the_remaining_ceiling() -> None:
    reply = first_valid("executor", 2)
    del reply["max_end_remaining_s"]
    assert_invalid(reply, "executor")


# --- Output: absolute paths and hard-link smuggling

def test_output_absolute_and_empty_components_rejected(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("x")
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for bad in ("/README.md", "a//b", "./README.md", ""):
            with pytest.raises(PermissionError):
                contained_open(fd, bad)
        assert contained_open(fd, "README.md") == b"x"
    finally:
        os.close(fd)


def test_output_staging_rejects_hard_links_symlinks_and_foreign_owner(tmp_path: Path) -> None:
    secret = tmp_path / "secret.key"
    secret.write_text("operator secret")
    staging = tmp_path / "staging"
    (staging / "sub").mkdir(parents=True)
    (staging / "result.txt").write_text("job output")
    (staging / "sub" / "more.txt").write_text("more")
    os.link(secret, staging / "smuggled.txt")  # Sol 6 r3: a hard link is a regular file
    (staging / "link").symlink_to(secret)
    store = tmp_path / "store"
    store.mkdir()
    sfd, dfd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY), os.open(store, os.O_RDONLY | os.O_DIRECTORY)
    try:
        result = stage_outputs(sfd, dfd, os.getuid())
        assert set(result["accepted"]) == {"result.txt", "sub/more.txt"}
        assert result["rejected"]["smuggled.txt"].startswith("hard link")
        assert "link" in result["rejected"]
        assert not (store / "smuggled.txt").exists() and (store / "result.txt").read_text() == "job output"
        with pytest.raises(PermissionError):
            contained_open(dfd, "smuggled.txt")
        (secret).write_text("changed secret")
        assert (store / "result.txt").read_text() == "job output"  # copies, not links
    finally:
        os.close(sfd)
        os.close(dfd)
    store2 = tmp_path / "store2"
    store2.mkdir()
    sfd, dfd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY), os.open(store2, os.O_RDONLY | os.O_DIRECTORY)
    try:
        foreign = stage_outputs(sfd, dfd, os.getuid() + 1)
        assert foreign["accepted"] == {}
    finally:
        os.close(sfd)
        os.close(dfd)


# --- C7h: effective-privilege audit on real `sudo -l` format, incl. Sol's group grant

SUDO = json.loads((Path(__file__).parent / "captures" / "sudo-l.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", SUDO["cases"], ids=[c["name"] for c in SUDO["cases"]])
def test_c7h_effective_audit_on_sudo_l_output(case) -> None:
    assert len(sudo_l_grants(case["text"], case["user"])) == case["expect_grants"]
    assert (sudoers_audit(case["text"], case["user"]) == []) is case["expect_ok"]


def test_c7h_continuation_lines_join_into_one_grant() -> None:
    real = next(c for c in SUDO["cases"] if c["name"] == "real-operator")
    grants = sudo_l_grants(real["text"], "operator")
    assert grants[3]["commands"] == ["/usr/bin/tool-b one", "/usr/bin/tool-b two", "/usr/bin/tool-b three"]
    assert grants[0] == {"runas": "ALL", "tags": [], "commands": ["ALL"], "raw": "(ALL) ALL"}


def test_c7h_file_audit_sees_group_and_alias_grants() -> None:
    good = (ROOT / "config/flightctl-helper-sudoers.example").read_text(encoding="utf-8")
    assert sudoers_file_audit(good, "runner", ["runner", "flightctl"]) == []
    assert sudoers_file_audit(good + "%flightctl ALL=(ALL) NOPASSWD: ALL\n", "runner", ["runner", "flightctl"])  # Sol 6 r3
    assert sudoers_file_audit(good + "User_Alias OPS = runner, someone\nOPS ALL=(ALL) ALL\n", "runner", ["runner"])
    assert sudoers_file_audit(good + "ALL ALL=(ALL) NOPASSWD: /usr/bin/systemctl\n", "runner", ["runner"])


# --- Token scrub: backups taken DURING the replay window

TOKEN = "ZZTOKENZZ" + "q" * 34


def _has(path: Path) -> bool:
    return path.exists() and TOKEN.encode() in path.read_bytes()


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA secure_delete=ON")
    return conn


def test_token_never_in_backups_taken_during_the_replay_window(tmp_path: Path) -> None:
    """D-token-4: raw tokens live only in a separate replay store that is never backed up."""
    main, replay = _open(tmp_path / "authority.db"), _open(tmp_path / "replay.db")
    main.execute("CREATE TABLE lease(lease_id TEXT, token_sha256 TEXT)")
    main.execute("CREATE TABLE idem(request_id TEXT, response TEXT)")
    replay.execute("CREATE TABLE replay(request_id TEXT, token TEXT, deadline REAL)")
    import hashlib
    main.execute("INSERT INTO lease VALUES ('lse-1', ?)", (hashlib.sha256(TOKEN.encode()).hexdigest(),))
    main.execute("INSERT INTO idem VALUES ('req-1', ?)", (json.dumps({"kind": "grant", "token": None, "lease_id": "lse-1"}),))
    replay.execute("INSERT INTO replay VALUES ('req-1', ?, 100.0)", (TOKEN,))
    backup = tmp_path / "backup-during-window.db"
    main.execute(f"VACUUM INTO '{backup}'")  # taken while the token is live
    replay.execute("DELETE FROM replay WHERE deadline <= 101.0")  # the window ends
    replay.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    main.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    main.close()
    replay.close()
    restored = tmp_path / "restored.db"
    restored.write_bytes(backup.read_bytes())
    assert [p.name for p in (backup, restored, tmp_path / "authority.db", tmp_path / "replay.db", tmp_path / "replay.db-wal") if _has(p)] == []


def test_token_backup_negative_control_round3_design_leaks(tmp_path: Path) -> None:
    """Sol 6 r3: with the token in the main DB, a backup taken during the window still holds it after the scrub."""
    main = _open(tmp_path / "authority.db")
    main.execute("CREATE TABLE idem(request_id TEXT, token TEXT)")
    main.execute("INSERT INTO idem VALUES ('req-1', ?)", (TOKEN,))
    backup = tmp_path / "backup-during-window.db"
    main.execute(f"VACUUM INTO '{backup}'")
    main.execute("UPDATE idem SET token = NULL")
    main.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    main.close()
    assert not _has(tmp_path / "authority.db") and _has(backup)
