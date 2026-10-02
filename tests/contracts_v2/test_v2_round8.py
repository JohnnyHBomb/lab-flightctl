"""Round 8: Sol 6 round-7 findings (REVIEW-sol6-r7.md) as regression tests: one admission rule, operator
authorization for claim-clear, persisted fence evidence before a ceiling grant, request-fingerprint replay, claim
rollback by inode, and honest statuses."""

import copy
import json
import os
import re
from pathlib import Path

import pytest

from .validation import (
    CLEAR_PATH,
    HELPER_PATH,
    SplitStore,
    assert_invalid,
    assert_valid,
    errors,
    examples,
    executor_reply,
    fence_evidence_ok,
    grant_after_reserve,
    helper_claim,
    helper_clear,
    helper_config_semantics,
    helper_reconcile,
    helper_release,
    no_clear_route_audit,
    operator_sudo_audit,
    sudoers_audit,
    sudoers_file_audit,
)

ROOT = Path(__file__).parents[2]
PROOF = {"user_slice_empty": True, "occupancy_empty": True, "key_removed": True}
OPERATOR = {"program": CLEAR_PATH, "ruid": 0, "euid": 0, "sudo_user": "opr", "sudo_uid": 1500}
EXPECT = {"lease_id": "lse-0000100", "generation": 7, "host_id": "host-1", "lane_id": "lane-gpu1", "request_ids": {"creq-1", "creq-2"}}
ARGS = {"lease_id": "lse-0000100", "generation": 7, "host_id": "host-1"}
ONE_RULE = ("while the account is quarantined, no claim of any kind is admitted (no new parent, no child of the old parent);"
            " otherwise a request is admitted only as the account's first parent claim or as a child of the claimed parent")


def rep(**kw):
    base = dict(ARGS, controller_request_id="creq-1", remaining=100.0)
    base.update(kw)
    return executor_reply(**base)


def clear(state, lease="lse-0000100", invocation=OPERATOR, proof=PROOF):
    return helper_clear(state, "fc-frienda", lease, proof, invocation, "opr", 1500)


# ============================================================ 1. one admission rule (C7h text contradiction)

def test_c7h_admission_rule_is_stated_once_everywhere() -> None:
    slices = re.sub(r"\s+", " ", (ROOT / "docs/v2/SLICES.md").read_text(encoding="utf-8"))
    c7h = slices.split("### C7h", 1)[1].split("\n### ", 1)[0] if "### C7h" in slices else slices
    cfg = json.loads((ROOT / "contracts/v2/helper-config.schema.json").read_text(encoding="utf-8"))
    xs = cfg["x-subcommands"]
    for where, text in (("SLICES C7h", c7h), ("session-open", xs["session-open"]), ("unit-start", xs["unit-start"])):
        assert ONE_RULE in text, where
    everything = slices + json.dumps(cfg)
    for contradiction in ("only when the account's claim names a DIFFERENT", "refuses only a claim naming a DIFFERENT",
                          "accepts the SAME parent as a child", "Children of the claimed parent are allowed"):
        assert contradiction not in everything, contradiction


def test_reference_follows_the_one_rule(tmp_path: Path) -> None:
    state = str(tmp_path)
    assert helper_claim(state, "fc-frienda", "lse-0000100")          # first parent claim
    assert helper_claim(state, "fc-frienda", "lse-0000100")          # child of the claimed parent
    assert not helper_claim(state, "fc-frienda", "lse-0000200")      # another parent
    helper_reconcile(state, {}, current_boot_id="b")
    for lease in ("lse-0000100", "lse-0000200"):                       # quarantined: nothing of any kind
        assert not helper_claim(state, "fc-frienda", lease)


# ============================================================ 2. claim-clear operator authorization

def test_helper_has_no_claim_clear_and_the_clear_program_is_contracted() -> None:
    cfg = json.loads((ROOT / "contracts/v2/helper-config.schema.json").read_text(encoding="utf-8"))
    assert "claim-clear" not in cfg["x-subcommands"]
    op = cfg["x-operator-clear"]
    assert CLEAR_PATH in op and "0700 root:root" in op and "WITHOUT NOPASSWD" in op and "SUDO_UID" in op
    assert "operator_account" in cfg["required"]
    good = examples("helper-config")["valid"][0]
    assert_valid(good, "helper-config")
    assert_invalid({k: v for k, v in good.items() if k != "operator_account"}, "helper-config")
    assert helper_config_semantics(good) == []
    assert helper_config_semantics(dict(good, operator_account=good["caller_account"]))
    assert helper_config_semantics(dict(good, operator_account="fc-frienda"))


@pytest.mark.parametrize("bad", [
    dict(OPERATOR, program=HELPER_PATH),         # through the executor's helper
    dict(OPERATOR, ruid=1000),                   # not root
    dict(OPERATOR, euid=1000),
    dict(OPERATOR, sudo_user="runner"),          # the executor
    dict(OPERATOR, sudo_user="fc-frienda"),      # a friend
    dict(OPERATOR, sudo_uid=1000),               # name/uid mismatch
    dict(OPERATOR, sudo_user=None, sudo_uid=None),  # root without sudo (no operator identity)
])
def test_claim_clear_requires_operator_sudo_path(tmp_path: Path, bad) -> None:
    state = str(tmp_path)
    helper_claim(state, "fc-frienda", "lse-0000100")
    helper_reconcile(state, {}, current_boot_id="b")
    assert not clear(state, invocation=bad)
    assert (tmp_path / "fc-frienda.quarantined").exists() and (tmp_path / "fc-frienda.parent").exists()
    assert clear(state)  # the real operator path works
    assert not (tmp_path / "fc-frienda.quarantined").exists()


def test_clear_needs_quarantine_proof_and_the_claimed_lease(tmp_path: Path) -> None:
    state = str(tmp_path)
    helper_claim(state, "fc-frienda", "lse-0000100")
    assert not clear(state)  # not quarantined: use claim-release
    helper_reconcile(state, {}, current_boot_id="b")
    assert not clear(state, proof=dict(PROOF, key_removed=False))
    assert not clear(state, lease="lse-0000999")
    assert not helper_release(state, "fc-frienda", "lse-0000100", PROOF)  # the executor path never lifts it
    assert clear(state)


OPR_PW = ("Matching Defaults entries for opr on host-a:\n    env_reset, !setenv\n\n"
          # round 9 (Sol 6 r8): fresh authentication for every invocation, in the measured sudo -l section format
          f"Runas and Command-specific defaults for opr:\n    Defaults!{CLEAR_PATH} timestamp_timeout=0\n\n"
          f"User opr may run the following commands on host-a:\n    (root) {CLEAR_PATH}\n")
EXEC = ("Matching Defaults entries for runner on host-a:\n    env_reset, !setenv, secure_path=/usr/sbin\\:/usr/bin\\:/sbin\\:/bin\n\n"
        f"User runner may run the following commands on host-a:\n    (root) NOPASSWD: {HELPER_PATH}\n")


def test_operator_route_must_reauthenticate() -> None:
    assert operator_sudo_audit(OPR_PW, "opr") == []
    assert operator_sudo_audit(OPR_PW.replace(f"(root) {CLEAR_PATH}", f"(root) NOPASSWD: {CLEAR_PATH}"), "opr")
    assert operator_sudo_audit(OPR_PW + "    (ALL : ALL) NOPASSWD: ALL\n", "opr")  # a broad NOPASSWD route also reaches it
    assert operator_sudo_audit(OPR_PW.replace(f"(root) {CLEAR_PATH}", "(ALL : ALL) ALL"), "opr") == []  # password ALL is fine
    assert operator_sudo_audit("User opr may run the following commands on host-a:\n    (root) /usr/bin/true\n", "opr")  # no route


def test_executor_and_friends_have_no_route_to_clear() -> None:
    assert no_clear_route_audit(EXEC, "runner") == [] and sudoers_audit(EXEC, "runner") == []
    extra = EXEC + f"    (root) NOPASSWD: {CLEAR_PATH}\n"
    assert no_clear_route_audit(extra, "runner") and sudoers_audit(extra, "runner")
    friend = "User fc-frienda may run the following commands on host-a:\n    (ALL) ALL\n"
    assert no_clear_route_audit(friend, "fc-frienda")
    assert no_clear_route_audit("User fc-frienda is not allowed to run sudo on host-a.\n", "fc-frienda") == []


def test_operator_sudoers_example_has_no_nopasswd_and_does_not_reach_the_executor() -> None:
    text = (ROOT / "config/flightctl-claim-clear-sudoers.example").read_text(encoding="utf-8")
    rules = [l for l in text.splitlines() if l.strip() and not l.startswith("#") and not l.startswith("Defaults")]
    assert rules == [f"opr ALL=(root) {CLEAR_PATH}"]
    assert "NOPASSWD" not in rules[0]
    assert sudoers_file_audit(text, "runner", ["runner"]) == []  # the executor is not named by it


# ============================================================ 3. persisted fence evidence

def test_sol_r7_reserved_reply_with_no_fence_and_no_inhibitor_never_grants() -> None:
    sol = rep(fenced=False)  # Sol 6 r7's exact shape: observed_state reserved, fences [], inhibitor null
    assert sol["observed_state"] == "reserved" and sol["fences"] == [] and sol["inhibitor"] is None
    assert errors(sol, "executor", "reply")  # the contract now refuses it outright...
    assert fence_evidence_ok(sol, EXPECT) is False  # ...and the oracle refuses it even without the schema step
    assert grant_after_reserve([{"received_t": 1.0, "reply": sol}], 1_000.0, EXPECT) == "pending ceiling-unconfirmed"
    assert grant_after_reserve([{"received_t": 1.0, "reply": rep()}], 1_000.0, EXPECT) == "grant"  # control


def _mut(fn):
    r = rep()
    fn(r)
    return r


@pytest.mark.parametrize("name,mutate", [
    ("fence of another generation", lambda r: r["fences"][0]["identity"].update(generation=6)),
    ("fence of another token", lambda r: r["fences"][0]["identity"].update(token_sha256="d" * 64)),
    ("fence state differs from observed", lambda r: r["fences"][0].update(state="running")),
    ("fence lost by reboot", lambda r: r["fences"][0].update(rebooted_since_reserve=True)),
    ("second fence on the lane", lambda r: r["fences"].append(dict(copy.deepcopy(r["fences"][0]), identity=dict(r["fences"][0]["identity"], generation=6)))),
    ("inhibitor missing", lambda r: r.update(inhibitor=None)),
    ("inhibitor not held", lambda r: r["inhibitor"].update(held=False)),
    ("inhibitor for another generation", lambda r: r["inhibitor"].update(unit="flightctl-awake-lane-gpu1-g6.service")),
    ("fence says inhibitor not held", lambda r: r["fences"][0].update(inhibitor_held=False)),
])
def test_ceiling_needs_persisted_fence(name, mutate) -> None:
    bad = _mut(mutate)
    assert grant_after_reserve([{"received_t": 1.0, "reply": bad}], 1_000.0, EXPECT) == "pending ceiling-unconfirmed", name


def test_inhibitor_only_where_the_lane_requires_it() -> None:
    no_inh = _mut(lambda r: (r.update(inhibitor=None), r["fences"][0].update(inhibitor_held=False)))
    always_on = dict(EXPECT, inhibitor_required=False)
    assert grant_after_reserve([{"received_t": 1.0, "reply": no_inh}], 1_000.0, always_on) == "grant"
    assert grant_after_reserve([{"received_t": 1.0, "reply": no_inh}], 1_000.0, EXPECT) == "pending ceiling-unconfirmed"
    missing_lane = {k: v for k, v in EXPECT.items() if k != "lane_id"}
    assert grant_after_reserve([{"received_t": 1.0, "reply": rep()}], 1_000.0, missing_lane) == "pending ceiling-unconfirmed"


def test_ceiling_reply_also_needs_its_fence() -> None:
    reserve = {"received_t": 1.0, "reply": rep(remaining=5_000.0)}
    ceiling = {"received_t": 2.0, "reply": rep(kind="ceiling", controller_request_id="creq-2", observed_state="running", remaining=10.0)}
    assert grant_after_reserve([reserve, ceiling], 1_000.0, EXPECT) == "grant"
    unfenced = copy.deepcopy(ceiling)
    unfenced["reply"]["fences"] = []
    assert grant_after_reserve([reserve, unfenced], 1_000.0, EXPECT) == "pending ceiling-unconfirmed"


# ============================================================ 4. claim rollback by inode (C7h acceptance)

def test_claim_rollback_unlinks_only_its_own_inode(tmp_path: Path) -> None:
    state = str(tmp_path)

    def quarantine() -> None:  # a marker appears right after our link()
        (tmp_path / "fc-frienda.quarantined").write_text("conflict")

    def clear_and_replace() -> None:  # an operator clear and a new parent run before our rollback
        assert helper_clear(state, "fc-frienda", "lse-0000100", PROOF, OPERATOR, "opr", 1500, _lock=False)
        assert helper_claim(state, "fc-frienda", "lse-0000200", _lock=False)

    assert helper_claim(state, "fc-frienda", "lse-0000100", _after_link=quarantine, _before_rollback=clear_and_replace) is False
    assert json.loads((tmp_path / "fc-frienda.parent").read_text())["lease_id"] == "lse-0000200"  # replacement untouched
    assert not [n for n in os.listdir(state) if ".tomb." in n]


def test_claim_cleared_and_replaced_before_the_recheck_is_not_ours(tmp_path: Path) -> None:
    state = str(tmp_path)

    def quarantine_clear_replace() -> None:
        (tmp_path / "fc-frienda.quarantined").write_text("conflict")
        assert helper_clear(state, "fc-frienda", "lse-0000100", PROOF, OPERATOR, "opr", 1500, _lock=False)
        assert helper_claim(state, "fc-frienda", "lse-0000200", _lock=False)

    started = []
    assert helper_claim(state, "fc-frienda", "lse-0000100", start=lambda: started.append(1), _after_link=quarantine_clear_replace) is False
    assert started == []  # a refused claim never starts
    assert json.loads((tmp_path / "fc-frienda.parent").read_text())["lease_id"] == "lse-0000200"


def test_rollback_of_our_own_claim_still_works(tmp_path: Path) -> None:
    state = str(tmp_path)
    assert helper_claim(state, "fc-frienda", "lse-0000100", _after_link=lambda: (tmp_path / "fc-frienda.quarantined").write_text("x")) is False
    assert not (tmp_path / "fc-frienda.parent").exists()


# ============================================================ 5. A5b1 request fingerprint

def test_replay_checks_request_fingerprint(tmp_path: Path) -> None:
    s = SplitStore(str(tmp_path / "m.db"), str(tmp_path / "r.db"))
    A, B = "a" * 64, "b" * 64
    assert s.grant("req-1", "lane-1", "lse-1", "t" * 43, deadline=100.0, principal="pa", fingerprint=A, now=0.0)["status"] == 200
    for now in (10.0, 150.0):  # inside and after the window
        assert s.grant("req-1", "lane-1", "lse-9", "u" * 43, deadline=200.0, principal="pa", fingerprint=B, now=now) == {"status": 409, "code": "conflict"}
    assert s.replay_or_record("req-1", principal="pa", fingerprint=A, now=10.0)["token"] == "t" * 43  # the matching retry still replays
    s.replay.execute("DELETE FROM replay")  # replay store lost
    assert s.replay_or_record("req-1", principal="pa", fingerprint=B, now=10.0) == {"status": 409, "code": "conflict"}
    assert s.main.execute("SELECT state FROM lease WHERE lease_id = 'lse-1'").fetchone()[0] == "active"  # no holder-lost side effect
    assert s.active_leases("lane-1") == 1
    s.close()


def test_fingerprint_contract_text() -> None:
    d = json.loads((ROOT / "contracts/v2/rpc-envelope.schema.json").read_text(encoding="utf-8"))
    desc = d["$defs"]["idempotency_record"]["properties"]["request_fingerprint"]["description"]
    assert "409 conflict" in desc and "never sees the stored response" in desc


# ============================================================ 6. N5 honest statuses

STATUSES = {"kept", "amended", "contract-fixed", "specified", "superseded", "moved", "dropped", "deferred", "probes", "carried"}


def test_conformance_statuses_are_from_the_vocabulary_and_honest() -> None:
    rows = [l.split("\t") for l in (ROOT / "docs/v2/CONFORMANCE.tsv").read_text(encoding="utf-8").splitlines()
            if l and not l.startswith("#") and not l.startswith("source\t")]
    assert {r[3] for r in rows} <= STATUSES
    by = {}
    for r in rows:
        by.setdefault(r[0], []).append(r)
    assert all(r[3] == "probes" for r in by["sol7-self"])
    for rid in ("sol6r6-B5", "sol6r6-N1"):
        assert all(r[3] == "superseded" and "sol6r7" in r[5] for r in by[rid])
    for rid in ("sol6r7-B5", "sol6r7-N1"):
        assert by[rid]
    header = (ROOT / "docs/v2/CONFORMANCE.tsv").read_text(encoding="utf-8").splitlines()[0]
    assert "probes (" in header and "carried (" in header  # freeze: carried = obligation handed to a packet
