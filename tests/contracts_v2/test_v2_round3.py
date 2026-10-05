"""Round 3 regression tests: each reproduces a Sol 6 round-2 counterexample (REVIEW-sol6-r2.md) and shows the
corrected contract or gate rejects it, plus the new findings (containment, helper boundary, token scrub)."""

import copy
import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tools import migration_gate

from .validation import (
    adapters_semantics,
    assert_invalid,
    assert_valid,
    auth_decision,
    contained_open,
    examples,
    helper_install_audit,
    host_ceiling_bound,
    work_admission,
    occupancy_semantics,
    sudoers_file_audit,
)

ROOT = Path(__file__).parents[2]


def first_valid(name: str, index: int = 0) -> dict:
    return copy.deepcopy(examples(name)["valid"][index])


# --- B1: controlled shadow-real exception for the inhibitor proof (A-ASM step 4)

def test_b1_shadow_lane_real_inhibitor_needs_the_declared_exception() -> None:
    config = first_valid("adapters")
    config["lanes"]["lane-gpu0"]["ports"]["inhibitor"] = "real"
    assert any("inhibitor is real (must be dryrun)" in p for p in adapters_semantics(config))  # Sol's conflict
    config["lanes"]["lane-gpu0"]["shadow_real"] = ["inhibitor"]
    assert adapters_semantics(config) == []  # the controlled proof path
    config["lanes"]["lane-gpu0"]["legacy_lane"] = None
    assert any("needs legacy_lane" in p for p in adapters_semantics(config))


def test_b1_shadow_real_cannot_name_any_other_port() -> None:
    config = first_valid("adapters")
    config["lanes"]["lane-gpu0"]["shadow_real"] = ["workload_runner"]
    assert_invalid(config, "adapters")
    config["lanes"]["lane-gpu0"]["shadow_real"] = ["inhibitor"]
    config["lanes"]["lane-gpu1"]["shadow_real"] = ["inhibitor"]  # a live lane
    assert any("only valid on a shadow lane" in p for p in adapters_semantics(config))


# --- B2: Sol's second counterexample (unknown-memory external process left out of tenants)

def sol6_r2_occupancy() -> dict:
    obs = first_valid("gpu-probe", 2)
    stray = {"gpu_uuid": obs["gpus"][0]["uuid"], "pid": 31337, "process_name": "python", "used_memory_mib": None, "attribution": "external"}
    obs["processes"].append(stray)
    obs["tenants"] = []
    obs["empty"] = True
    return obs


def test_b2_counterexample_is_rejected_by_the_partition_rule() -> None:
    obs = sol6_r2_occupancy()
    problems = occupancy_semantics(obs)
    assert any("partition" in p for p in problems) and any("unknown memory" in p for p in problems)


def test_b2_noise_with_unknown_memory_is_schema_invalid() -> None:
    obs = first_valid("gpu-probe", 2)
    obs["processes"][0]["used_memory_mib"] = None
    obs["noise"][0]["used_memory_mib"] = None
    assert_invalid(obs, "gpu-probe", "occupancy_observation")


# --- B3: Sol's live-lane override counterexample

def test_b3_live_lane_cannot_override_a_required_port_to_fake() -> None:
    config = first_valid("adapters")
    config["features"].update({"endpoints": True, "jobs": True})
    config["allow_fake"] = [p for p in config["allow_fake"] if p not in {"model_cache", "health_probe"}]
    config["ports"].update({"model_cache": "real", "health_probe": "real", "workload_runner": "real"})
    assert adapters_semantics(config) == []
    config["lanes"]["lane-gpu1"]["ports"] = {"model_cache": "fake", "health_probe": "fake"}
    problems = adapters_semantics(config)
    assert "lane lane-gpu1 is live but model_cache is fake" in problems and "lane lane-gpu1 is live but health_probe is fake" in problems


# --- B5: one active parent per friend per host (round 4 semantics; round-4 race tests in test_v2_round4.py)

def test_b5_second_work_item_for_same_friend_same_host_is_refused() -> None:
    parents = {("friend-a", "host-1"): "lse-0000100"}
    assert work_admission(parents, "friend-a", "host-1", "parent", "lse-0000200", "human") == ("deny", "account_busy")
    assert work_admission(parents, "friend-a", "host-2", "parent", "lse-0000200", "human") == ("allow", None)
    assert work_admission(parents, "friend-a", "host-1", "parent", "lse-0000200", "agent") == ("allow", None)
    policy = first_valid("policy")
    policy["work"]["max_active_parents_per_human_per_host"] = 2
    assert_invalid(policy, "policy")


# --- B6: one identity rule for friends and agents

ACCOUNTS = [
    {"principal_id": "friend-a", "enabled": True, "external_ids": [{"kind": "tailnet-login", "value": "friend@example"}], "allowed_peers": []},
    {"principal_id": "review-agent", "enabled": True, "external_ids": [], "allowed_peers": [{"kind": "tailnet-node", "value": "host-0.example"}]},
]
NOW = "2026-10-02T12:00:00Z"
AGENT_TOKEN = {"principal_id": "review-agent", "revoked_at": None, "expires_at": "2026-11-01T00:00:00Z"}


def test_b6_friend_authenticates_by_peer_and_token_never_alone() -> None:
    login = [{"kind": "tailnet-login", "value": "friend@example"}]
    assert auth_decision(login, ACCOUNTS, None, NOW) == ("allow", "friend-a")
    assert auth_decision([{"kind": "tailnet-node", "value": "stranger.example"}], ACCOUNTS, AGENT_TOKEN, NOW) == ("deny", None)
    assert auth_decision([{"kind": "tailnet-node", "value": "host-0.example"}], ACCOUNTS, AGENT_TOKEN, NOW) == ("allow", "review-agent")
    slices = (ROOT / "docs/v2/SLICES.md").read_text(encoding="utf-8")
    assert "holding only an API token" not in slices, "C7c must describe the friend's peer identity, not a token-only friend"




# --- B7: strict mode is a hard failure; the migration gate checks the real diff

def test_b7_strict_missing_real_twin_fails_the_run() -> None:
    env = dict(os.environ, FLIGHTCTL_CONFORMANCE_STRICT="1", FLIGHTCTL_CONFORMANCE_PORTS="inhibitor")
    env.pop("FLIGHTCTL_CONFORMANCE_TARGET", None)
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-o", "addopts=", "-p", "no:cacheprovider", "tests/conformance/test_power.py"],
                         cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert run.returncode != 0, run.stdout[-800:]
    summary = run.stdout.strip().splitlines()[-1]
    assert "STRICT" in run.stdout and "xfailed" not in summary and ("error" in summary or "failed" in summary), summary


def test_b7_non_strict_run_is_green_with_visible_skips() -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("FLIGHTCTL_CONFORMANCE")}
    run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-o", "addopts=", "-p", "no:cacheprovider", "tests/conformance/test_power.py"],
                         cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    assert run.returncode == 0 and "skipped" in run.stdout


@pytest.mark.parametrize("touched,collected,packet,ok", [
    ({}, set(), "A8", True),
    ({"tests/authority/test_revision2.py": "M"}, {"tests/authority/test_restart.py::test_restart_reconciles_lane_by_inspect"}, "A8", True),
    ({"tests/authority/test_revision2.py": "M"}, {"tests/authority/test_x.py::test_other"}, "A8", False),  # Sol 6 r3: a comment naming it does not count; only a collected node
    ({"tests/authority/test_revision2.py": "M"}, set(), "A8", False),          # rewrite without its replacement test
    ({"tests/authority/test_authority.py": "M"}, set(), "A8", False),          # unmapped pre-existing test touched
    ({"tests/roster/shims.py": "M"}, set(), "B3", False),                      # delete row but file only modified
    ({"tests/roster/shims.py": "D", "tests/roster/fake_client.py": "D"}, set(), "B3", True),
    ({"tests/roster/shims.py": "D"}, set(), "A10", False),                     # right row, wrong packet
])
def test_b7_migration_gate_logic(touched, collected, packet, ok) -> None:
    rows = migration_gate.load_rows(ROOT / "docs/v2/migration-map.tsv")
    packet_files = {nid.split("::")[0] for nid in collected}  # the replacement lives in a file this packet changed
    assert (migration_gate.check(touched, collected, rows, packet, packet_files) == []) is ok


def branch_migration_problems(touched: dict[str, str], collected: set[str], rows: list[dict[str, str]],
                              branch_test_files: set[str]) -> list[str]:
    """Amendment 9: each pre-existing test/roster file touched since 59bdd7f must pass the unchanged per-packet check
    (migration_gate.check, G1b) for at least ONE packet whose rows name it, so a packet that executes its own rows (or
    the lead's contracts-v2 stream) stays green. A file no row names, a whole-file delete row whose file was only
    modified, and a rewrite whose replacement is not collected (a bare name only in a test file added or modified since
    59bdd7f) still fail."""
    problems = []
    for path, status in sorted(touched.items()):
        packets = sorted({r["packet"] for r in rows if path in migration_gate.expand(r["v1_test"])})
        if not packets:
            problems.append(f"{path} ({status}) is a pre-existing test/roster file with no migration-map row for any packet")
            continue
        per_packet = {p: migration_gate.check({path: status}, collected, rows, p, branch_test_files) for p in packets}
        if all(per_packet.values()):
            problems.append(f"{path} ({status}): no packet's rows allow this change: {per_packet}")
    return problems


def test_b7_migration_gate_on_this_branchs_real_diff(monkeypatch: pytest.MonkeyPatch) -> None:
    base = "59bdd7f"
    if subprocess.run(["git", "cat-file", "-e", base], cwd=ROOT, capture_output=True).returncode != 0:
        pytest.skip(f"base commit {base} not in this clone (shallow checkout)")
    monkeypatch.chdir(ROOT)  # migration_gate.git and migration_gate.collect run in the working directory
    status = migration_gate.git("diff", "--name-status", "--no-renames", base, "HEAD", "--", *migration_gate.WATCHED)
    touched, branch_test_files = {}, set()
    for line in status.splitlines():  # the same reading of the diff as migration_gate.main
        code, _, path = line.partition("\t")
        if code[:1] in {"A", "M"} and path.startswith("tests/"):
            branch_test_files.add(path)
        if code[:1] in {"M", "D"}:
            touched[path] = code[:1]
    rows = migration_gate.load_rows(ROOT / "docs/v2/migration-map.tsv")
    collected = migration_gate.collect() if touched else set()
    assert branch_migration_problems(touched, collected, rows, branch_test_files) == []


# --- N1: Sol's 30 s skew + 60 s transport case

def test_n1_slow_transport_is_detected_and_shortened() -> None:
    approved, margin = 10_000.0, 30.0
    send_t = 0.0
    in_s = approved - send_t - margin
    slow = host_ceiling_bound(send_t=send_t, rtt_s=60.0 + 1.0, in_s=in_s, approved_max_end_t=approved)
    assert slow["must_shorten"] and slow["late_by_s"] == pytest.approx(31.0)  # round 2 left it ~30 s late
    resend_t = 61.0
    fixed = host_ceiling_bound(send_t=resend_t, rtt_s=5.0, in_s=approved - resend_t - margin, approved_max_end_t=approved)
    assert not fixed["must_shorten"]
    ceiling = {"schema_version": 2, "kind": "ceiling", "controller_request_id": "creq-9", "controller_id": "controller-a", "sent_at": "2026-10-02T10:01:01Z",
               "identity": first_valid("executor", 1)["identity"], "max_end": {"kind": "max-end", "in_s": approved - resend_t - margin, "sender_utc": "2026-10-02T10:01:01Z"}}
    assert_valid(ceiling, "executor")


# --- New: output containment (symlink escape, list-then-get swap)

def test_output_containment_refuses_symlink_escape_and_swap(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("operator secret")
    root = tmp_path / "out"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "result.txt").write_text("ok")
    (root / "leak").symlink_to(secret)
    (root / "dirlink").symlink_to(tmp_path)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        listed = hashlib.sha256(b"ok").hexdigest()
        assert contained_open(fd, "sub/result.txt", listed) == b"ok"
        for bad in ("leak", "dirlink/secret.txt", "../secret.txt", "/etc/hostname"):
            with pytest.raises(PermissionError):
                contained_open(fd, bad)
        (root / "sub" / "result.txt").unlink()
        (root / "sub" / "result.txt").symlink_to(secret)  # swapped between list and get
        with pytest.raises(PermissionError):
            contained_open(fd, "sub/result.txt", listed)
        (root / "sub" / "result.txt").unlink()
        (root / "sub" / "result.txt").write_text("changed")
        with pytest.raises(ValueError):
            contained_open(fd, "sub/result.txt", listed)
    finally:
        os.close(fd)


# --- New: C7h helper privilege boundary

def test_helper_sudoers_example_passes_and_bad_rules_fail() -> None:
    good = (ROOT / "config/flightctl-helper-sudoers.example").read_text(encoding="utf-8")
    assert sudoers_file_audit(good, "runner", ["runner"]) == []
    assert sudoers_file_audit(good.replace("NOPASSWD: /usr/local/libexec/flightctl-helper", "NOPASSWD: ALL"), "runner", ["runner"])
    assert sudoers_file_audit(good + "runner ALL=(root) NOPASSWD: /usr/bin/systemctl\n", "runner", ["runner"])
    assert sudoers_file_audit(good.replace("!setenv", "setenv"), "runner", ["runner"])
    assert sudoers_file_audit(good.replace("NOPASSWD:", "SETENV: NOPASSWD:"), "runner", ["runner"])


def test_helper_install_chain_must_be_root_owned_and_unwritable() -> None:
    chain = [{"path": "/", "uid": 0, "mode": 0o40755}, {"path": "/usr", "uid": 0, "mode": 0o40755}, {"path": "/usr/local/libexec/flightctl-helper", "uid": 0, "mode": 0o100755}]
    assert helper_install_audit(chain) == []
    assert helper_install_audit(chain[:2] + [{"path": "/usr/local/libexec/flightctl-helper", "uid": 1000, "mode": 0o100755}])
    assert helper_install_audit(chain[:2] + [{"path": "/usr/local/libexec", "uid": 0, "mode": 0o40775}])


def test_helper_config_allowlist_contract() -> None:
    assert_valid(first_valid("helper-config"), "helper-config")
    for bad in examples("helper-config")["invalid"]:
        assert_invalid(bad, "helper-config")


# --- New: D-token-2 storage-level scrub proof (reference procedure on real SQLite files)

TOKEN = "ZZTOKENZZ" + "q" * 34


def _store(path: Path, secure: bool) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA secure_delete={'ON' if secure else 'OFF'}")
    conn.execute("CREATE TABLE idem(id INTEGER PRIMARY KEY, token TEXT, pad TEXT)")
    for i in range(50):
        conn.execute("INSERT INTO idem(token, pad) VALUES (?, ?)", (TOKEN if i == 7 else None, "x" * 100))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return conn


def _leaks(*paths: Path) -> list[str]:
    return [p.name for p in paths if p.exists() and TOKEN.encode() in p.read_bytes()]


def test_token_scrub_procedure_leaves_no_bytes_in_db_wal_or_restored_backup(tmp_path: Path) -> None:
    db = tmp_path / "authority.db"
    conn = _store(db, secure=True)
    conn.execute("UPDATE idem SET token = NULL WHERE token IS NOT NULL")  # scrub after the replay window
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    backup = tmp_path / "backup.db"
    conn.execute(f"VACUUM INTO '{backup}'")
    conn.close()
    restored = tmp_path / "restored.db"
    restored.write_bytes(backup.read_bytes())
    sqlite3.connect(restored).execute("PRAGMA integrity_check").fetchall()
    assert _leaks(db, Path(f"{db}-wal"), backup, restored) == []


def test_token_scrub_negative_control_detects_leftover_bytes(tmp_path: Path) -> None:
    """Without secure_delete the token bytes stay in the database file (measured 2 Oct): the check can fail."""
    db = tmp_path / "authority.db"
    conn = _store(db, secure=False)
    conn.execute("UPDATE idem SET token = NULL WHERE token IS NOT NULL")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    assert _leaks(db) == ["authority.db"]
