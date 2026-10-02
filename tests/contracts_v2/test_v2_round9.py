"""Round 9: Sol 6 round-8 findings (REVIEW-sol6-r8.md): claim-clear fresh authentication, sudoers(5) route matching for
the claim-clear audits, (principal, request_id) idempotency scope; plus the round-9 self-adversarial probes."""

import json
from pathlib import Path

import pytest

from .validation import (
    CLEAR_PATH,
    HELPER_PATH,
    SplitStore,
    clear_fresh_auth_audit,
    no_clear_route_audit,
    operator_sudo_audit,
    sudo_path_matches,
    sudoers_audit,
)

ROOT = Path(__file__).parents[2]
FRESH = f"Runas and Command-specific defaults for opr:\n    Defaults!{CLEAR_PATH} timestamp_timeout=0\n\n"
GRANTS = f"User opr may run the following commands on host-a:\n    (root) {CLEAR_PATH}\n"
MATCH = "Matching Defaults entries for opr on host-a:\n    env_reset, !setenv\n\n"


def opr(specific: str = FRESH, matching: str = MATCH, grants: str = GRANTS) -> str:
    return matching + specific + grants


def friend(line: str, user: str = "fc-frienda") -> str:
    return f"User {user} may run the following commands on host-a:\n    {line}\n"


# ============================================================ 1. fresh authentication

def test_operator_audit_fails_without_fresh_auth() -> None:
    assert operator_sudo_audit(opr(), "opr") == []
    missing = operator_sudo_audit(opr(specific=""), "opr")  # Sol 6 r8: a cached timestamp would satisfy the rule
    assert any("timestamp_timeout" in p and "default (5)" in p for p in missing)


def test_contract_and_example_require_fresh_auth() -> None:
    cfg = json.loads((ROOT / "contracts/v2/helper-config.schema.json").read_text(encoding="utf-8"))
    assert f"Defaults!{CLEAR_PATH} timestamp_timeout=0" in cfg["x-operator-clear"]
    example = (ROOT / "config/flightctl-claim-clear-sudoers.example").read_text(encoding="utf-8")
    assert f"Defaults!{CLEAR_PATH} timestamp_timeout=0" in example.splitlines()
    # what `sudo -l -U opr` prints for the example (measured section format, sudo 1.9.17p2) passes the audit
    line = next(l for l in example.splitlines() if l.startswith("Defaults!"))
    assert operator_sudo_audit(opr(specific=f"Runas and Command-specific defaults for opr:\n    {line}\n\n"), "opr") == []


@pytest.mark.parametrize("label,matching,specific,ok", [
    ("user-wide 0 in matching Defaults", "Matching Defaults entries for opr on host-a:\n    env_reset, timestamp_timeout=0\n\n", "", True),
    ("fractional zero", MATCH, f"Runas and Command-specific defaults for opr:\n    Defaults!{CLEAR_PATH} timestamp_timeout=0.0\n\n", True),
    ("command-specific 0 beats runas 5", MATCH, f"Runas and Command-specific defaults for opr:\n    Defaults>root timestamp_timeout=5\n    Defaults!{CLEAR_PATH} timestamp_timeout=0\n\n", True),
    ("later wildcard Defaults! overrides to 5", MATCH, f"Runas and Command-specific defaults for opr:\n    Defaults!{CLEAR_PATH} timestamp_timeout=0\n    Defaults!/usr/local/libexec/* timestamp_timeout=5\n\n", False),
    ("command-specific 5 overrides user-wide 0", "Matching Defaults entries for opr on host-a:\n    timestamp_timeout=0\n\n", f"Runas and Command-specific defaults for opr:\n    Defaults!{CLEAR_PATH} timestamp_timeout=5\n\n", False),
    ("runas root 5 with no command-specific", "Matching Defaults entries for opr on host-a:\n    timestamp_timeout=0\n\n", "Runas and Command-specific defaults for opr:\n    Defaults>root timestamp_timeout=5\n\n", False),
    ("negative (never expires)", MATCH, f"Runas and Command-specific defaults for opr:\n    Defaults!{CLEAR_PATH} timestamp_timeout=-1\n\n", False),
    ("half a minute", MATCH, f"Runas and Command-specific defaults for opr:\n    Defaults!{CLEAR_PATH} timestamp_timeout=0.5\n\n", False),
    ("authentication disabled", MATCH, f"Runas and Command-specific defaults for opr:\n    Defaults!{CLEAR_PATH} timestamp_timeout=0, !authenticate\n\n", False),
    ("unresolvable alias", MATCH, "Runas and Command-specific defaults for opr:\n    Defaults!CLEARCMD timestamp_timeout=0\n\n", False),
    ("other command only", MATCH, f"Runas and Command-specific defaults for opr:\n    Defaults!{HELPER_PATH} timestamp_timeout=0\n\n", False),
])
def test_self_effective_timestamp_timeout(label, matching, specific, ok) -> None:
    problems = clear_fresh_auth_audit(opr(specific=specific, matching=matching))
    assert (problems == []) is ok, (label, problems)


def test_self_alias_in_defaults_resolves_with_the_sudoers_text() -> None:
    text = opr(specific="Runas and Command-specific defaults for opr:\n    Defaults!CLEARCMD timestamp_timeout=0\n\n")
    sudoers = f"Cmnd_Alias CLEARCMD = {CLEAR_PATH}\nDefaults!CLEARCMD timestamp_timeout=0\nopr ALL=(root) CLEARCMD\n"
    assert operator_sudo_audit(text, "opr") != []
    assert operator_sudo_audit(text, "opr", sudoers_text=sudoers) == []


# ============================================================ 2. route matching (sudoers(5))

def test_sol_r8_friend_wildcard_grant_reaches_clear() -> None:
    assert no_clear_route_audit(friend("(root) NOPASSWD: /usr/local/libexec/*"), "fc-frienda")


def test_sol_r8_operator_extra_nopasswd_wildcard_fails() -> None:
    text = opr(grants=GRANTS + "    (root) NOPASSWD: /usr/local/libexec/*\n")
    assert any("NOPASSWD" in p for p in operator_sudo_audit(text, "opr"))


@pytest.mark.parametrize("line", [
    "(root) /usr/local/libexec/",                                     # directory grant
    "(root) /usr/local/*/flightctl-claim-clear",                      # wildcard directory component
    "(root) /usr/local/libexec/flightctl-claim-*",                    # wildcard file name
    "(root) /usr/local/libexec/flightctl-claim-clea?",
    "(root) /usr/local/libexec/flightctl-[a-c]laim-clear",
    "(root) ^/usr/local/libexec/flightctl-.*$",                       # regex path (1.9.10+)
    f"(root) {CLEAR_PATH} --account fc-frienda --parent-lease lse-1",  # argument-bearing
    f'(root) {CLEAR_PATH} ""',                                         # no-argument form still reaches the program
    f"(root) /usr/bin/true, {CLEAR_PATH}",                             # second command in a list
    f"(root) /usr/local/libexec/*, !{CLEAR_PATH}",                     # a negation never cancels the route
    "(ALL : ALL) ALL",
    "(root) CLEARCMDS",                                                # unexpanded alias: fail closed
    "(root) ^/usr/local/libexec/flightctl-(claim$",                    # broken regex: fail closed
])
def test_self_routes_that_reach_clear(line: str) -> None:
    assert no_clear_route_audit(friend(line), "fc-frienda"), line


@pytest.mark.parametrize("line", [
    f"(root) {HELPER_PATH}",
    "(root) /usr/local/*",                                   # a wildcard never matches '/'
    "(root) /usr/local/libexec/*/flightctl-claim-clear",     # one directory too deep
    "(root) /usr/local/libexec/sub/",                        # another directory
    "(root) sudoedit /etc/motd",
    "(root) ^/usr/local/libexec/flightctl-helper$",
])
def test_self_routes_that_do_not_reach_clear(line: str) -> None:
    assert no_clear_route_audit(friend(line), "fc-frienda") == [], line


def test_self_cmnd_alias_and_group_in_installed_sudoers() -> None:
    sudoers = ("User_Alias FRIENDS = %fcfriends\n"
               f"Cmnd_Alias SAFE = /usr/bin/true\nCmnd_Alias HIDDEN = /usr/bin/true, {CLEAR_PATH[:-5]}*\n"
               "FRIENDS ALL=(root) NOPASSWD: SAFE, HIDDEN\n")
    assert no_clear_route_audit("", "fc-frienda", sudoers_text=sudoers, groups=["fcfriends"])
    assert no_clear_route_audit("", "fc-frienda", sudoers_text=sudoers, groups=["other"]) == []
    safe = sudoers.replace(f", {CLEAR_PATH[:-5]}*", "")
    assert no_clear_route_audit("", "fc-frienda", sudoers_text=safe, groups=["fcfriends"]) == []
    # the same alias expansion applies to the operator: a NOPASSWD alias route fails
    op_sudoers = f"Cmnd_Alias ANYLIB = /usr/local/libexec/*\nopr ALL=(root) NOPASSWD: ANYLIB\n"
    assert any("NOPASSWD" in p for p in operator_sudo_audit(opr(), "opr", sudoers_text=op_sudoers))


def test_self_executor_wildcard_grant_fails_both_audits() -> None:
    text = ("Matching Defaults entries for runner on host-a:\n    env_reset, !setenv, secure_path=/usr/sbin\\:/usr/bin\n\n"
            "User runner may run the following commands on host-a:\n    (root) NOPASSWD: /usr/local/libexec/*\n")
    assert sudoers_audit(text, "runner") and no_clear_route_audit(text, "runner")


def test_self_path_matcher_basics() -> None:
    assert sudo_path_matches(CLEAR_PATH, CLEAR_PATH) and sudo_path_matches("ALL", CLEAR_PATH)
    assert not sudo_path_matches("/usr/local/libexec", CLEAR_PATH)  # the directory itself without '/' is a file path
    assert sudo_path_matches("^(?i)/USR/LOCAL/LIBEXEC/FLIGHTCTL-CLAIM-CLEAR$", CLEAR_PATH)


# ============================================================ 3. (principal, request_id) scope

def store(tmp_path: Path) -> SplitStore:
    return SplitStore(str(tmp_path / "m.db"), str(tmp_path / "r.db"))


def test_second_principal_same_id_same_payload_never_gets_the_first_token(tmp_path: Path) -> None:
    s = store(tmp_path)
    fp = "f" * 64
    a = s.grant("req-1", "lane-1", "lse-1", "A" * 43, deadline=100.0, principal="alice", fingerprint=fp, now=0.0)
    assert a == {"status": 200, "lease_id": "lse-1", "token": "A" * 43}
    b = s.grant("req-1", "lane-1", "lse-2", "B" * 43, deadline=100.0, principal="bob", fingerprint=fp, now=1.0)
    assert b == {"status": 409, "code": "busy"}  # a fresh decision: the lane is held; never alice's lease or token
    assert s.replay_or_record("req-1", principal="bob", fingerprint=fp, now=1.0) is None
    s.close_after_proof("lse-1")
    b2 = s.grant("req-1", "lane-1", "lse-2", "B" * 43, deadline=100.0, principal="bob", fingerprint=fp, now=2.0)
    assert b2 == {"status": 200, "lease_id": "lse-2", "token": "B" * 43}  # bob's own grant
    assert s.replay_or_record("req-1", principal="alice", fingerprint=fp, now=3.0)["token"] == "A" * 43  # alice unaffected
    s.close()


def test_self_other_principal_with_a_different_payload_is_not_a_conflict(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.grant("req-1", "lane-1", "lse-1", "A" * 43, deadline=100.0, principal="alice", fingerprint="a" * 64, now=0.0)
    out = s.grant("req-1", "lane-2", "lse-2", "B" * 43, deadline=100.0, principal="bob", fingerprint="b" * 64, now=1.0)
    assert out == {"status": 200, "lease_id": "lse-2", "token": "B" * 43}  # fresh decision on another lane, not 409 conflict
    assert s.grant("req-1", "lane-1", "lse-9", "C" * 43, deadline=100.0, principal="alice", fingerprint="b" * 64, now=2.0) == {"status": 409, "code": "conflict"}
    s.close()


def test_self_other_principal_after_the_deadline_still_gets_nothing_of_the_first(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.grant("req-1", "lane-1", "lse-1", "A" * 43, deadline=100.0, principal="alice", fingerprint="f" * 64, now=0.0)
    assert s.replay_or_record("req-1", principal="bob", fingerprint="f" * 64, now=150.0) is None  # not the null-token replay of alice's grant
    s.close()


def test_self_crash_and_recover_are_per_principal(tmp_path: Path) -> None:
    s = store(tmp_path)
    s.grant("req-1", "lane-1", "lse-1", "A" * 43, deadline=100.0, principal="alice", fingerprint="f" * 64, now=0.0)
    with pytest.raises(RuntimeError):
        s.grant("req-1", "lane-2", "lse-2", "B" * 43, deadline=100.0, principal="bob", fingerprint="f" * 64, now=1.0, crash_between=True)
    assert s.recover() == 1  # only bob's orphan goes
    assert s.replay_or_record("req-1", principal="alice", fingerprint="f" * 64, now=2.0)["token"] == "A" * 43
    assert s.replay.execute("SELECT count(*) FROM replay").fetchone()[0] == 1
    s.close()


def test_self_contract_states_the_scope() -> None:
    d = json.loads((ROOT / "contracts/v2/rpc-envelope.schema.json").read_text(encoding="utf-8"))
    idem = d["$defs"]["idempotency_record"]
    assert "(principal_id, request_id)" in idem["description"] and "principal_id" in idem["required"]
    assert "(authenticated principal, request_id)" in d["description"]
