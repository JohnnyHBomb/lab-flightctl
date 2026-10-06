"""Round 7: Sol 6 round-6 probes (REVIEW-sol6-r6.md) plus the SELF-ADVERSARIAL pass over every rule touched in rounds
4-7 (at least two extra probes per rule: boundaries, alternate wire shapes, concurrent/crash interleavings, another
code path to the same state, and docs that could contradict the rule)."""

import copy
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tools import migration_gate

from .validation import (
    CLEAR_PATH,
    SplitStore,
    assert_invalid,
    auth_decision,
    contained_open,
    errors,
    examples,
    executor_reply,
    grant_after_reserve,
    helper_claim,
    helper_clear,
    helper_reconcile,
    helper_release,
    occupancy_from_capture,
    occupancy_semantics,
    stage_outputs,
    sudo_l_grants,
    schema_prose,
    service_account_offenders,
    sudoers_audit,
    work_admission,
)

ROOT = Path(__file__).parents[2]
PROOF = {"user_slice_empty": True, "occupancy_empty": True, "key_removed": True}
EXPECT = {"lease_id": "lse-0000100", "generation": 7, "host_id": "host-1", "lane_id": "lane-gpu1", "request_ids": {"creq-1", "creq-2"}}
ARGS = {"lease_id": "lse-0000100", "generation": 7, "host_id": "host-1"}
OPERATOR = {"program": CLEAR_PATH, "ruid": 0, "euid": 0, "sudo_user": "opr", "sudo_uid": 1500}


def reply(**kw):
    base = dict(ARGS, controller_request_id="creq-1", remaining=100.0)
    base.update(kw)
    return {"received_t": 1.0, "reply": executor_reply(**base)}


def with_fence(entry, fence_state="reserved"):
    """Round 8 (Sol 6 r7): give a probe the full persisted-fence evidence, so the rule under test is the only reason
    it can be refused."""
    r = copy.deepcopy(entry)
    good = executor_reply(**dict(ARGS, controller_request_id="creq-1", remaining=1.0, observed_state=fence_state))
    r["reply"]["fences"], r["reply"]["inhibitor"] = good["fences"], good["inhibitor"]
    return r


# ======================================================================= Sol 6 round-6 probes

def test_sol_r6_quarantine_blocks_children_of_the_old_parent(tmp_path: Path) -> None:
    state = str(tmp_path)
    helper_claim(state, "fc-frienda", "lse-0000100")
    helper_reconcile(state, {}, current_boot_id="b")
    assert not helper_claim(state, "fc-frienda", "lse-0000100")


@pytest.mark.parametrize("mutation", [{"dry_run": True}, {"kind": "inspect"}, {"observed_state": "free"}])
def test_sol_r6_ceiling_only_after_a_real_reserve(mutation) -> None:
    assert grant_after_reserve([reply()], 1_000.0, EXPECT) == "grant"
    probe = with_fence(reply(**mutation))  # round 8: fenced, so only the mutated field can refuse it
    assert errors(probe["reply"], "executor", "reply") == []  # the schema allows these values...
    assert grant_after_reserve([probe], 1_000.0, EXPECT) == "pending ceiling-unconfirmed"  # ...the grant rule does not


def test_sol_r6_duplicate_grant_after_deadline_with_row_present_returns_null_token(tmp_path: Path) -> None:
    s = SplitStore(str(tmp_path / "m.db"), str(tmp_path / "r.db"))
    s.grant("req-1", "lane-1", "lse-1", "t" * 43, deadline=100.0, principal="pa", fingerprint="a" * 64, now=0.0)
    assert s.grant("req-1", "lane-1", "lse-1", "t" * 43, deadline=100.0, principal="pa", fingerprint="a" * 64, now=150.0) == {"status": 200, "lease_id": "lse-1", "token": None}
    assert s.grant("req-1", "lane-1", "lse-1", "t" * 43, deadline=100.0, principal="pa", fingerprint="a" * 64, now=50.0)["token"] == "t" * 43
    s.close()


def test_sol_r6_no_service_account_in_agent_execution_docs() -> None:
    # round 8 (Sol 6 r7): each occurrence must sit INSIDE a withdrawal phrase; a same-line 'withdrawn' no longer exempts it
    sources = [*sorted((ROOT / "docs/v2").glob("*.md")), *sorted((ROOT / "docs/v2").glob("*.txt")), *sorted((ROOT / "contracts/v2").glob("*.md")),
               ROOT / "contracts/v2/interfaces.py", *sorted((ROOT / "contracts/v2").glob("*.schema.json")), *sorted((ROOT / "config").glob("*.example"))]
    def text(path: Path) -> str:
        raw = path.read_text(encoding="utf-8")
        return schema_prose(json.loads(raw)) if path.name.endswith(".schema.json") else raw

    offenders = {path.name: service_account_offenders(text(path)) for path in sources}
    assert {k: v for k, v in offenders.items() if v} == {}
    friends = json.loads((ROOT / "contracts/v2/helper-config.schema.json").read_text(encoding="utf-8"))["properties"]["friend_accounts"]
    assert friends["items"]["not"] == {"pattern": "^fc-svc$"}  # the structural rejection stays


@pytest.mark.parametrize("text", [
    "Agents run as fc-svc (the old name is withdrawn).",
    "Start the job as the service account; the per-job DynamicUser is withdrawn.",
    "withdrawn: no. Use `fc-svc` for agent jobs.",
    "output-collect --account <service_account>",
])
def test_sol_r7_affirmative_instruction_next_to_withdrawn_is_still_an_offender(text: str) -> None:
    assert service_account_offenders(text)


def test_sol_r7_withdrawal_phrases_are_not_offenders() -> None:
    assert service_account_offenders("The shared `fc-svc` account is withdrawn. No service account is involved.") == []


SUDO = ("Matching Defaults entries for runner on host-a:\n    env_reset, !setenv, secure_path=/usr/sbin\\:/usr/bin\\:/sbin\\:/bin\n\n"
        "User runner may run the following commands on host-a:\n    (root) NOPASSWD: /usr/local/libexec/flightctl-helper\n")


@pytest.mark.parametrize("value", ["/usr/bin\\:\\:/bin", "/usr/bin\\:", "\\:/usr/bin"])
def test_sol_r6_secure_path_empty_components(value: str) -> None:
    assert sudoers_audit(SUDO, "runner") == []
    bad = SUDO.replace("secure_path=/usr/sbin\\:/usr/bin\\:/sbin\\:/bin", f"secure_path={value}")
    assert any("empty component" in p for p in sudoers_audit(bad, "runner"))


# ======================================================================= SELF-ADVERSARIAL probes

# --- (a) occupancy emptiness (r4-r7)

def test_self_occupancy_unexplained_exactly_at_lane_noise_is_not_empty() -> None:
    obs = copy.deepcopy(examples("gpu-probe")["valid"][2])
    obs["gpus"][0]["memory_used_mib"] = 1024 + 5
    obs["lane_memory_used_mib"] = 1029
    obs["unexplained_mib"] = 1024  # == lane_noise_mib: boundary must be strict
    obs["empty"] = True
    assert any("contradicts" in p for p in occupancy_semantics(obs))


def test_self_occupancy_process_memory_above_card_usage_is_inconsistent() -> None:
    obs = copy.deepcopy(examples("gpu-probe")["valid"][2])
    obs["processes"][0]["used_memory_mib"] = obs["noise"][0]["used_memory_mib"] = 7  # card reports only 6 MiB used
    obs["unexplained_mib"] = -1
    assert any("memory.used" in p for p in occupancy_semantics(obs))
    uuid = obs["gpus"][0]["uuid"]
    oracle = occupancy_from_capture(f"{uuid}, 6, 24576, 0, 40, 30.00, 200.00, Not Active, Not Active, [N/A]\n",
                                    f"{uuid}, 4242, python, 900\n", returncode=0, lane_id="l", host_id="h",
                                    observed_at="2026-10-02T00:00:00Z", lane_uuids=[uuid])
    assert oracle["status"] == "unknown" and oracle["empty"] is False  # separate samples disagree: unknown, never empty


def test_self_occupancy_header_or_extra_card_row_cannot_sneak_in() -> None:
    uuid = "GPU-00000000-0000-0000-0000-000000000011"
    rows = f"{uuid}, 6, 24576, 0, 40, 30.00, 200.00, Not Active, Not Active, [N/A]\n"
    dup = occupancy_from_capture(rows + rows, "", returncode=0, lane_id="l", host_id="h", observed_at="2026-10-02T00:00:00Z", lane_uuids=[uuid])
    assert dup["status"] == "unknown"  # the same card twice is not 'all present'
    hdr = occupancy_from_capture("uuid, memory.used [MiB]\n" + rows, "", returncode=0, lane_id="l", host_id="h", observed_at="2026-10-02T00:00:00Z", lane_uuids=[uuid])
    assert hdr["status"] == "unknown"  # an unexpected header row (format drift) is unparsable, not ignored


# --- (b) parent/child claims (r4-r7)

def test_self_claim_rejects_path_like_or_withdrawn_account_names(tmp_path: Path) -> None:
    for bad in ("../etc", "fc-a/../../x", "fc-svc", "root", "FC-A"):
        with pytest.raises(ValueError):
            helper_claim(str(tmp_path), bad, "lse-0000100")


def test_self_release_refuses_a_symlink_planted_at_the_claim_path(tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.write_text(json.dumps({"lease_id": "lse-0000100"}))
    (tmp_path / "fc-frienda.parent").symlink_to(target)
    assert helper_release(str(tmp_path), "fc-frienda", "lse-0000100", PROOF) is False
    assert target.exists()


def test_self_quarantine_racing_a_claim_never_leaves_an_admitted_child(tmp_path: Path) -> None:
    """Round 8 (Sol 6 r7): every child admitted during the race must have FINISHED starting before the quarantine
    marker existed, and no child may start after it. start() widens the window with a sleep; without the account lock
    a reconcile lands mid-start (checked by the vacuity control below)."""
    state = str(tmp_path)
    marker = tmp_path / "fc-frienda.quarantined"
    helper_claim(state, "fc-frienda", "lse-0000100")
    barrier = threading.Barrier(9)
    guard = threading.Lock()
    events: list[str] = []
    done = threading.Event()

    def start() -> None:
        before = marker.exists()
        time.sleep(0.002)
        after = marker.exists()
        with guard:
            events.append("bad" if (before or after) else "started")

    def child(i: int) -> int:
        barrier.wait()
        n = 0
        while not done.is_set():
            n += helper_claim(state, "fc-frienda", "lse-0000100", start=start)
        return n

    def quarantine() -> dict:
        barrier.wait()
        time.sleep(0.02)
        out = helper_reconcile(state, {}, current_boot_id="b")
        with guard:
            events.append("quarantined")
        time.sleep(0.02)
        done.set()
        return out

    with ThreadPoolExecutor(max_workers=9) as pool:
        futures = [pool.submit(child, i) for i in range(8)] + [pool.submit(quarantine)]
        results = [f.result() for f in futures]
    assert sum(results[:8]) == events.count("started") > 0  # the race really admitted children before the quarantine
    assert "bad" not in events  # no admitted child was still starting (or started) once the marker existed
    assert "started" not in events[events.index("quarantined"):]  # nothing started after the quarantine
    assert not helper_claim(state, "fc-frienda", "lse-0000100") and marker.exists()


def test_self_crash_between_claim_removal_and_marker_removal_stays_blocked(tmp_path: Path) -> None:
    state = str(tmp_path)
    helper_claim(state, "fc-frienda", "lse-0000100")
    helper_reconcile(state, {}, current_boot_id="b")
    os.unlink(tmp_path / "fc-frienda.parent")  # simulate: claim-clear crashed after removing the claim
    assert not helper_claim(state, "fc-frienda", "lse-0000200")  # still blocked by the marker
    assert not helper_release(state, "fc-frienda", "lse-0000100", PROOF)  # a friend-side release never lifts it
    assert helper_clear(state, "fc-frienda", "lse-0000100", PROOF, OPERATOR, "opr", 1500)  # the retried clear finishes
    assert helper_claim(state, "fc-frienda", "lse-0000200")


def test_self_work_admission_child_without_any_parent_is_refused() -> None:
    assert work_admission({}, "friend-a", "host-1", "child", "lse-0000100", "human") == ("deny", "account_busy")
    assert work_admission({}, "friend-a", "host-1", "parent", "lse-0000100", "human") == ("allow", None)


# --- (c) token expiry and identity (r4-r6)

ACCTS = [{"principal_id": "agent", "enabled": True, "external_ids": [], "allowed_peers": [{"kind": "tailnet-node", "value": "h0"}]},
         {"principal_id": "off", "enabled": False, "external_ids": [{"kind": "tailnet-login", "value": "off@x"}], "allowed_peers": []}]
PEER = [{"kind": "tailnet-node", "value": "h0"}]


@pytest.mark.parametrize("expires", ["2026-11-01T00:00:00+00:00", "2026-11-01", "not a time", None])
def test_self_unparsable_or_offset_expiry_fails_closed(expires) -> None:
    token = {"principal_id": "agent", "revoked_at": None, "expires_at": expires}
    assert auth_decision(PEER, ACCTS, token, "2026-10-02T12:00:00Z") == ("deny", None)


def test_self_disabled_principal_and_token_for_unknown_principal_deny() -> None:
    assert auth_decision([{"kind": "tailnet-login", "value": "off@x"}], ACCTS, None, "2026-10-02T12:00:00Z") == ("deny", None)
    token = {"principal_id": "ghost", "revoked_at": None, "expires_at": "2027-01-01T00:00:00Z"}
    assert auth_decision(PEER, ACCTS, token, "2026-10-02T12:00:00Z") == ("deny", None)


# --- (d) migration gate (r4-r5)

ROW = [{"v1_test": "tests/a/test_old.py::test_old", "packet": "P", "action": "rewrite", "replacement": "test_new", "reason": "-"}]


def test_self_gate_name_prefix_or_suffix_does_not_count() -> None:
    touched = {"tests/a/test_old.py": "M"}
    for nid in ("tests/a/test_old.py::test_new_extra", "tests/a/test_old.py::test_newer", "tests/a/test_old.py::xtest_new"):
        assert migration_gate.check(touched, {nid}, ROW, "P", {"tests/a/test_old.py"}), nid
    assert migration_gate.check(touched, {"tests/a/test_old.py::TestX::test_new[p1]"}, ROW, "P", {"tests/a/test_old.py"}) == []


def test_self_gate_replacement_in_an_unchanged_file_with_same_basename_does_not_count() -> None:
    touched = {"tests/a/test_old.py": "M"}
    assert migration_gate.check(touched, {"tests/b/test_old.py::test_new"}, ROW, "P", {"tests/a/test_old.py"})


# --- (e) ceiling grants (r4-r7)

def test_self_ceiling_reply_before_any_reserve_never_grants() -> None:
    ceiling_first = reply(kind="ceiling", controller_request_id="creq-2", observed_state="reserved")
    assert grant_after_reserve([ceiling_first], 1_000.0, EXPECT) == "pending ceiling-unconfirmed"
    late_reserve = reply(remaining=5_000.0)  # real reserve whose ceiling is too late
    ok_ceiling = reply(kind="ceiling", controller_request_id="creq-2", observed_state="running", remaining=500.0)
    assert grant_after_reserve([late_reserve, ok_ceiling], 1_000.0, EXPECT) == "grant"


def test_self_ceiling_reply_reporting_free_or_quarantined_never_grants() -> None:
    reserve = reply(remaining=5_000.0)  # real fenced reserve, ceiling too late on its own
    control = reply(kind="ceiling", controller_request_id="creq-2", observed_state="running", remaining=10.0)
    assert grant_after_reserve([reserve, control], 1_000.0, EXPECT) == "grant"
    checked = 0
    for state in ("free", "quarantined", "unknown", "stopping"):
        probe = with_fence(reply(kind="ceiling", controller_request_id="creq-2", observed_state=state, remaining=10.0), "running")
        if errors(probe["reply"], "executor", "reply"):
            continue  # the schema already refuses this shape for ok=True
        checked += 1
        assert grant_after_reserve([reserve, probe], 1_000.0, EXPECT) == "pending ceiling-unconfirmed", state
    assert checked == 2  # free and stopping reach the grant rule with a valid persisted fence


def test_self_ceiling_boundary_equal_is_ok_and_one_ms_late_is_not() -> None:
    assert grant_after_reserve([{"received_t": 900.0, "reply": reply(remaining=100.0)["reply"]}], 1_000.0, EXPECT) == "grant"
    assert grant_after_reserve([{"received_t": 900.001, "reply": reply(remaining=100.0)["reply"]}], 1_000.0, EXPECT) == "pending ceiling-unconfirmed"


# --- (f) output containment and staging (r3-r4)

def test_self_staging_rejects_fifo_and_symlinked_subdirectory(tmp_path: Path) -> None:
    staging, store = tmp_path / "s", tmp_path / "d"
    staging.mkdir()
    store.mkdir()
    os.mkfifo(staging / "pipe")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "secret").write_text("x")
    (staging / "dirlink").symlink_to(tmp_path / "elsewhere")
    sfd, dfd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY), os.open(store, os.O_RDONLY | os.O_DIRECTORY)
    try:
        result = stage_outputs(sfd, dfd, os.getuid())
    finally:
        os.close(sfd)
        os.close(dfd)
    assert result["accepted"] == {} and set(result["rejected"]) == {"pipe", "dirlink"}


@pytest.mark.parametrize("bad", ["a/../b", "a\x00b", "a/./b", "a/b/"])
def test_self_containment_rejects_tricky_relpaths(tmp_path: Path, bad: str) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b").write_text("x")
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(PermissionError):
            contained_open(fd, bad)
    finally:
        os.close(fd)


# --- (g) sudo audit (r4-r7)

def test_self_sudo_duplicate_grant_or_helper_with_arguments_fails() -> None:
    dup = SUDO + "    (root) NOPASSWD: /usr/local/libexec/flightctl-helper\n"
    assert sudoers_audit(dup, "runner")
    args = SUDO.replace("/usr/local/libexec/flightctl-helper", "/usr/local/libexec/flightctl-helper --anything")
    assert sudoers_audit(args, "runner")


def test_self_sudo_env_keep_ld_preload_and_quoted_secure_path() -> None:
    keep = SUDO.replace("env_reset, ", "env_reset, env_keep+=\"LD_PRELOAD\", ")
    assert sudoers_audit(keep, "runner")
    quoted = SUDO.replace("secure_path=/usr/sbin\\:/usr/bin\\:/sbin\\:/bin", "secure_path=\"/usr/sbin\\:/usr/bin\"")
    assert sudoers_audit(quoted, "runner") == []
    quoted_empty = SUDO.replace("secure_path=/usr/sbin\\:/usr/bin\\:/sbin\\:/bin", "secure_path=\"\"")
    assert sudoers_audit(quoted_empty, "runner")


def test_self_sudo_grant_for_another_user_does_not_count_as_ours() -> None:
    other = SUDO.replace("User runner may run", "User someone may run")
    assert sudo_l_grants(other, "runner") == [] and sudoers_audit(other, "runner")


# --- (h) split replay store (r4-r7)

def test_self_split_store_busy_grant_leaves_no_orphan_replay_row(tmp_path: Path) -> None:
    s = SplitStore(str(tmp_path / "m.db"), str(tmp_path / "r.db"))
    s.grant("req-1", "lane-1", "lse-1", "t" * 43, deadline=100.0, principal="pa", fingerprint="a" * 64, now=0.0)
    assert s.grant("req-2", "lane-1", "lse-2", "u" * 43, deadline=100.0, principal="pa", fingerprint="a" * 64, now=1.0) == {"status": 409, "code": "busy"}
    assert s.replay.execute("SELECT count(*) FROM replay WHERE request_id = 'req-2'").fetchone()[0] == 0
    assert s.recover() == 0  # recovery never deletes a live, committed replay row
    s.close()


def test_self_split_store_replay_at_exact_deadline_is_null_token(tmp_path: Path) -> None:
    s = SplitStore(str(tmp_path / "m.db"), str(tmp_path / "r.db"))
    s.grant("req-1", "lane-1", "lse-1", "t" * 43, deadline=100.0, principal="pa", fingerprint="a" * 64, now=0.0)
    assert s.replay_or_record("req-1", principal="pa", fingerprint="a" * 64, now=99.999)["token"] == "t" * 43
    assert s.replay_or_record("req-1", principal="pa", fingerprint="a" * 64, now=100.0)["token"] is None
    s.close()


def test_self_grant_requires_an_explicit_retry_time(tmp_path: Path) -> None:
    s = SplitStore(str(tmp_path / "m.db"), str(tmp_path / "r.db"))
    with pytest.raises(TypeError):
        s.grant("req-1", "lane-1", "lse-1", "t" * 43, deadline=100.0)  # no default 'now' to fall back on
    s.close()


# --- (i) DynamicUser isolation wording (r5-r7)

def test_self_isolation_wording_is_consistent_everywhere() -> None:
    cfg = json.loads((ROOT / "contracts/v2/helper-config.schema.json").read_text(encoding="utf-8"))
    assert cfg["properties"]["agent_isolation"]["const"] == "dynamic-user"
    assert "DynamicUser=yes" in cfg["x-subcommands"]["unit-start"]
    slices = (ROOT / "docs/v2/SLICES.md").read_text(encoding="utf-8")
    c7b1 = slices.split("| C7b1 |", 1)[1].split("\n", 1)[0]
    assert "DynamicUser" in c7b1
    conformance = (ROOT / "docs/v2/CONFORMANCE.tsv").read_text(encoding="utf-8")
    iso = [l for l in conformance.splitlines() if "shared fc-svc" in l]
    assert iso and all("\tspecified\t" in l for l in iso)  # never claimed proven without the root-only onlab test


def test_self_unit_start_never_names_uid_for_agents() -> None:
    sub = json.loads((ROOT / "contracts/v2/helper-config.schema.json").read_text(encoding="utf-8"))["x-subcommands"]["unit-start"]
    agents, _, friends = sub.partition("friends:")
    assert "--uid" not in agents and "--uid" in friends


# --- (j) plan order (r5)

def plan_problems(text: str) -> list[str]:
    section = text.split("## Packet index", 1)[1].split("**Assembly prerequisites**", 1)[0]
    rows = []
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 8 and cells[0] not in {"ID", "---"} and not set(cells[0]) <= {"-"}:
            rows.append((cells[0].strip("*"), cells[2]))
    ids = [r[0] for r in rows]
    problems = []
    order = {"A": 0, "B": 1, "C": 2, "D": 3}  # D: session enablement after R1 (Amendment 3)
    if [order[m] for _, m in rows] != sorted(order[m] for _, m in rows):
        problems.append("milestone column not monotone")
    cw = text.split("Crosswalk to ROADMAP.tsv.", 1)[1].split("\n## ", 1)[0]
    listed = []
    for line in cw.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 2 and cells[0] not in {"Packet", "---"}:
            listed += [p.strip() for p in cells[0].split(",")]
    if sorted(listed) != sorted(ids):
        problems.append("crosswalk coverage")
    if [ids.index(p) for p in listed if p in ids] != sorted(ids.index(p) for p in listed if p in ids):
        problems.append("crosswalk order")
    return problems


def test_self_plan_checker_accepts_the_real_plan_and_catches_swaps() -> None:
    slices = (ROOT / "docs/v2/SLICES.md").read_text(encoding="utf-8")
    assert plan_problems(slices) == []
    # Amendment 12: the row lists A11b between A11 and A12 (index order)
    swapped = slices.replace("| A11, A11b, A12 | S09 (now delivered before the A-ASM assembly) |\n| A-ASM | S08 (live flip) |",
                             "| A-ASM | S08 (live flip) |\n| A11, A11b, A12 | S09 (now delivered before the A-ASM assembly) |")
    assert swapped != slices and "crosswalk order" in plan_problems(swapped)
    duplicated = slices.replace("| A8 | S07 |", "| A8, A8 | S07 |")
    assert "crosswalk coverage" in plan_problems(duplicated)
    milestone = slices.replace("| B2 | Holder liveness", "| B2 | Holder liveness").replace("heartbeat op, client heartbeat, stale-holder handling | B |", "heartbeat op, client heartbeat, stale-holder handling | C |", 1)
    assert "milestone column not monotone" in plan_problems(milestone)
