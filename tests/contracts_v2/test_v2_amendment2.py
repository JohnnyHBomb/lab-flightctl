"""Amendment 2 (Sol 6.1 cold review, REVIEW-sol61-cold.md): each finding reproduced as a failing case first (scratch
repro_amd2.py on ea14534), then closed here. P1-1 noise by identity; P1-2 namespace-aware output ownership; P1-3
explicit execution identity and timeouts; P1-4 a legal G1b route for the carried freeze cases and A0c repros; P1-5 the
named T04 split; P2 per-lane GPU job-path acceptance."""

import copy
import inspect
import json
import os
import re
from pathlib import Path

import pytest

from tools import migration_gate

from .validation import (
    assert_invalid,
    assert_valid,
    collect_job_outputs,
    errors,
    examples,
    noise_identity_ok,
    occupancy_from_capture,
    occupancy_semantics,
    output_owner_expected,
    stage_outputs,
)

ROOT = Path(__file__).parents[2]
SLICES = (ROOT / "docs/v2/SLICES.md").read_text(encoding="utf-8")
U = "GPU-00000000-0000-0000-0000-000000000011"
BROWSER = {"uid": 1000, "argv0": "/usr/lib/browser/browser", "context_type": "C+G"}
ALLOW = [{"argv0": "/usr/lib/browser/browser", "uid": 1000}]


def section(title: str) -> str:
    return SLICES.split(title, 1)[1].split("\n### ", 1)[0]


def occ(used: int, procs: str, identities=None, allow=ALLOW, cap=64):
    gpus = f"{U}, {used}, 15360, 0, 40, 30.00, 70.00, Not Active, Not Active, [N/A]\n"
    identities = identities or {}
    # rev 2: uid/argv0 per pid, context type per (gpu_uuid, pid)
    per_pid = {pid: {k: v for k, v in (i or {}).items() if k != "context_type"} for pid, i in identities.items()}
    types = {(U, pid): (i or {}).get("context_type") for pid, i in identities.items() if (i or {}).get("context_type")}
    return occupancy_from_capture(gpus, procs, returncode=0, lane_id="l", host_id="h", observed_at="2026-10-02T00:00:00Z",
                                  lane_uuids=[U], noise_allowlist=allow, noise_cap_mib=cap, identities=per_pid, context_types=types)


# ================================================================ P1-1 noise by identity, never by size

def test_p1_1_sols_counterexamples_are_tenants() -> None:
    one = occ(312, f"{U}, 4000, python3 cuInit_holder.py, 300\n", {4000: {"uid": 1000, "argv0": "python3", "context_type": "C"}})
    assert one["empty"] is False and [p["pid"] for p in one["tenants"]] == [4000]       # T10: ~300 MiB stray CUDA context
    eight = occ(2412, "".join(f"{U}, {4000 + i}, python3 stray.py, 300\n" for i in range(8)))
    assert eight["empty"] is False and len(eight["tenants"]) == 8                      # 2,400 MiB of unrelated contexts
    for obs in (one, eight):
        assert occupancy_semantics(obs) == []


def test_p1_1_small_unlisted_process_is_a_tenant_and_listed_desktop_is_noise() -> None:
    tiny = occ(10, f"{U}, 4001, python3 x.py, 5\n", {4001: {"uid": 1000, "argv0": "python3", "context_type": "C"}})
    assert tiny["empty"] is False and tiny["tenants"]                                  # 5 MiB unlisted: still a tenant
    desk = occ(10, f"{U}, 555655, /usr/lib/browser/browser --type=gpu-process, 5\n", {555655: BROWSER})
    assert desk["empty"] is True and [p["pid"] for p in desk["noise"]] == [555655] and occupancy_semantics(desk) == []


@pytest.mark.parametrize("label,identity", [
    ("pure compute context under an allow-listed name", dict(BROWSER, context_type="C")),
    ("allow-listed name, other uid", dict(BROWSER, uid=1001)),
    ("root", dict(BROWSER, uid=0)),
    ("DynamicUser uid", dict(BROWSER, uid=61200)),
    ("unreadable identity", {"uid": None, "argv0": None, "context_type": None}),
    ("no identity at all", None),
])
def test_p1_1_noise_needs_the_full_identity(label, identity) -> None:
    allow = ALLOW + [{"argv0": "/usr/lib/browser/browser", "uid": 61200}]  # even an entry naming a DynamicUser uid never counts
    obs = occ(10, f"{U}, 555655, /usr/lib/browser/browser --type=gpu-process, 5\n", {555655: identity} if identity else {}, allow=allow)
    assert obs["empty"] is False and obs["tenants"], label


def test_p1_1_noise_cap_and_semantics() -> None:
    over = occ(200, "".join(f"{U}, {7000 + i}, /usr/lib/browser/browser, 40\n" for i in range(3)), {7000 + i: BROWSER for i in range(3)}, cap=64)
    assert over["tenants"] == [] and over["empty"] is False and occupancy_semantics(over) == []  # 120 MiB of noise > cap 64
    forged = copy.deepcopy(occ(10, f"{U}, 4001, python3 x.py, 5\n", {4001: {"uid": 1000, "argv0": "python3", "context_type": "C"}}))
    forged["processes"][0]["attribution"] = "noise"
    forged["noise"], forged["tenants"], forged["empty"] = forged["processes"], [], True
    forged["unexplained_mib"] = 5
    assert any("not an allow-listed desktop identity" in p for p in occupancy_semantics(forged))
    assert noise_identity_ok(BROWSER, ALLOW) and not noise_identity_ok(dict(BROWSER, context_type="C"), ALLOW)


def test_p1_1_schema_carries_identity_and_allowlist() -> None:
    gp = json.loads((ROOT / "contracts/v2/gpu-probe.schema.json").read_text(encoding="utf-8"))["$defs"]
    assert {"uid", "argv0", "context_type"} <= set(gp["gpu_process"]["required"])
    assert set(gp["occupancy_observation"]["properties"]["thresholds"]["required"]) == {"lane_noise_mib", "noise_cap_mib", "noise_allowlist"}
    assert "process_noise_mib" not in json.dumps(gp)
    inv = json.loads((ROOT / "contracts/v2/inventory.schema.json").read_text(encoding="utf-8"))
    assert "process_noise_mib" not in json.dumps(inv) and "noise_allowlist" in json.dumps(inv)
    obs = occ(10, f"{U}, 555655, /usr/lib/browser/browser, 5\n", {555655: BROWSER})
    assert_valid(obs, "gpu-probe", "occupancy_observation")
    bad = copy.deepcopy(obs)
    bad["noise"][0]["context_type"] = "C"
    bad["processes"][0]["context_type"] = "C"
    assert_invalid(bad, "gpu-probe", "occupancy_observation")  # the schema itself refuses a C-context noise row


# ================================================================ P1-2 namespace-aware output ownership

def test_p1_2_owner_rule() -> None:
    assert output_owner_expected(61184, 61184, 65534) == 61184   # no id-mapping: host sees the dynamic uid
    assert output_owner_expected(65534, 61184, 65534) == 65534   # id-mapped StateDirectory: host sees the overflow uid
    assert output_owner_expected(1000, 61184, 65534) is None     # someone else's directory: collect nothing


def _dirs(tmp_path: Path):
    src, dst = tmp_path / "s", tmp_path / "d"
    src.mkdir()
    dst.mkdir()
    return src, dst


def test_p1_2_genuine_id_mapped_output_is_collected(tmp_path: Path) -> None:
    src, dst = _dirs(tmp_path)
    (src / "result.txt").write_text("ok")
    me = os.getuid()
    sfd, dfd = os.open(src, os.O_RDONLY | os.O_DIRECTORY), os.open(dst, os.O_RDONLY | os.O_DIRECTORY)
    try:
        # model: the recorded runtime uid differs from what the host sees (61184), the host sees the overflow uid (= me here)
        got = collect_job_outputs(sfd, dfd, recorded_uid=61184, overflow_uid=me)
    finally:
        os.close(sfd)
        os.close(dfd)
    assert list(got["accepted"]) == ["result.txt"] and got["owner"] == me  # Sol's case now accepted


def test_p1_2_foreign_directory_and_hard_links_stay_rejected(tmp_path: Path) -> None:
    src, dst = _dirs(tmp_path)
    (src / "a.txt").write_text("x")
    os.link(src / "a.txt", src / "b.txt")
    me = os.getuid()
    sfd, dfd = os.open(src, os.O_RDONLY | os.O_DIRECTORY), os.open(dst, os.O_RDONLY | os.O_DIRECTORY)
    try:
        foreign = collect_job_outputs(sfd, dfd, recorded_uid=me + 1, overflow_uid=me + 2)
        linked = collect_job_outputs(sfd, dfd, recorded_uid=me, overflow_uid=65534)
    finally:
        os.close(sfd)
        os.close(dfd)
    assert foreign["accepted"] == {} and foreign["owner"] is None
    assert linked["accepted"] == {} and all("hard link" in r for r in linked["rejected"].values())


def test_p1_2_contract_text_and_onlab_test() -> None:
    cfg = json.loads((ROOT / "contracts/v2/helper-config.schema.json").read_text(encoding="utf-8"))
    assert "namespace-aware" in cfg["x-subcommands"]["output-collect"] and "overflow uid" in cfg["x-subcommands"]["output-collect"]
    assert "`test_dynamicuser_write_stop_collect`" in section("### C7b1:")  # rev 2: collect at C7b1, fetch at C7c, end to end at C-ASM
    assert "`test_dynamicuser_output_authenticated_fetch`" in section("### C7c:") and "`test_job_output_end_to_end`" in section("### C-ASM")


# ================================================================ P1-3 explicit identity and timeouts

def _ports():
    ns: dict = {}
    exec(compile((ROOT / "contracts/v2/interfaces.py").read_text(encoding="utf-8"), "interfaces.py", "exec"), ns)
    return ns


def test_p1_3_identity_travels_through_the_ports() -> None:
    ns = _ports()
    start = inspect.signature(ns["WorkloadRunner"].start).parameters
    assert {"work_id", "lease_id", "lane_id", "parent_lease_id"} <= set(start)
    opened = inspect.signature(ns["SessionGateway"].open).parameters
    assert {"parent_lease_id", "lane_id"} <= set(opened)
    assert "parent_lease_id" in inspect.signature(ns["SessionGateway"].close).parameters


def test_p1_3_every_port_method_has_an_explicit_timeout() -> None:
    ns = _ports()
    missing = []
    for name, obj in ns.items():
        if not (inspect.isclass(obj) and getattr(obj, "_is_protocol", False)) or name == "Clock":
            continue
        for meth, fn in vars(obj).items():
            if callable(fn) and not meth.startswith("_") and "timeout_s" not in inspect.signature(fn).parameters:
                missing.append(f"{name}.{meth}")
    assert missing == []


def test_p1_3_wire_and_helper_carry_the_work_id() -> None:
    ex = json.loads((ROOT / "contracts/v2/executor.schema.json").read_text(encoding="utf-8"))
    assert "work_id" in ex["$defs"]["workload"]["required"]
    reserve = examples("executor")["valid"][0]
    start = {k: copy.deepcopy(reserve[k]) for k in ("schema_version", "controller_request_id", "controller_id", "sent_at", "identity", "execution_policy")}
    start["kind"] = "start"
    start["workload"] = {"kind": "argv-unit", "template_ref": {"template_id": "cuda-smoke", "version": "1.0.0"}, "argv": ["python3", "smoke.py"],
                         "env": {}, "run_as": "flightctl-job", "workdir_ref": {"root": "scratch", "relpath": "job-0000001"},
                         "cards": [reserve["identity"]["lane"].get("uuid", "GPU-00000000-0000-0000-0000-000000000011")],
                         "log_capture": "journal", "health": None, "work_id": "job-0000001"}
    assert_valid(start, "executor")
    no_work = copy.deepcopy(start)
    no_work["workload"].pop("work_id")
    assert errors(no_work, "executor")
    unit_start = json.loads((ROOT / "contracts/v2/helper-config.schema.json").read_text(encoding="utf-8"))["x-subcommands"]["unit-start"]
    assert "--job <public id> --lease <lease_id> --lane <lanes key>" in unit_start


# ================================================================ P1-4 a legal G1b route

def test_p1_4_freeze_cases_assert_against_the_production_interface() -> None:
    text = (ROOT / "tests/contracts_v2/test_v2_freeze.py").read_text(encoding="utf-8")
    assert 'PRODUCTION_AUDIT = "flightctl.install_audit"' in text
    assert "def test_carried_exempt_group_defeats_fresh_auth(install_audit)" in text
    assert "def test_carried_per_command_tags_after_a_comma(install_audit)" in text
    assert "flightctl/install_audit.py" in section("### C7h")


def test_p1_4_migration_rows_authorise_removing_the_marks() -> None:
    rows = migration_gate.load_rows(ROOT / "docs/v2/migration-map.tsv")
    freeze = "tests/contracts_v2/test_v2_freeze.py"
    touched = {freeze: "M"}
    collected = {f"{freeze}::test_carried_exempt_group_defeats_fresh_auth", f"{freeze}::test_carried_per_command_tags_after_a_comma",
                 f"{freeze}::test_carried_obligations_are_recorded"}
    assert migration_gate.check(touched, collected, rows, "C7h", {freeze}) == []
    assert migration_gate.check(touched, collected, [r for r in rows if freeze not in r["v1_test"]], "C7h", {freeze})  # without: refused
    for test, packet in (("test_repro_cross_host_reserve", "A5a"), ("test_repro_beat_and_renew", "A5b2"),
                         ("test_repro_release_identity_and_reason", "A5b1"), ("test_repro_restart_freeze", "A8")):
        path = f"tests/sim/{test}.py"
        assert f"`{path}`" in section("### A0c:")
        assert migration_gate.check({path: "M"}, {f"{path}::{test}"}, rows, packet, {path}) == [], packet


# ================================================================ P1-5 named T04 split; P2 GPU job path

def test_p1_5_t04_split_is_named() -> None:
    assert "| T04a TTL expiry and renewal, HOLDER variant" in SLICES and "| T04b TTL expiry, MANAGED-UNIT variant" in SLICES
    a_asm = section("### A-ASM")
    assert "T04a" in a_asm and "T04 is SPLIT by name" in a_asm and "T04," not in a_asm.split("ACCEPTANCE (G6):", 1)[1].split("\n", 1)[0]
    assert "`T04b`" in section("### C7b1:")
    assert "T04a, T04b" in section("### C-ASM")


def test_p2_gpu_job_path_acceptance_per_lane() -> None:
    assert "`test_cuda_job_production_path`" in section("### C7b1:")
    c7b2 = section("### C7b2:")
    for name in ("test_cancel_and_preempt_with_gpu_memory_allocated", "test_controller_loss_during_gpu_job",
                 "test_no_successor_grant_before_teardown_verified"):
        assert f"`{name}`" in c7b2 and name in section("### C-ASM")
    assert "`review-obligation-no-credentials-outside-window`" in section("### C9:")


# ================================================================ rev 2 (Sol 6.1 amd2): prerequisites delivered earlier

from .test_v2_plan import IDS, POS  # noqa: E402

# Capabilities named in acceptance text that only a later packet delivers. Rev 3 (Sol 6.1 amd2r2): derived for the
# RPC ops (op_owners) and hand-listed only for what the schema cannot say (T04b).
HAND_CAPABILITIES = {"T04b": "C7b1"}


def op_owners(text: str) -> dict[str, str]:
    """Owner of each HYPHENATED rpc op (contracts/v2/rpc-ops.schema.json x-ops) = the earliest packet whose SCOPE or GOAL
    names it. BOUNDED: plain-word ops (acquire, release, status, ...) are too common in prose to check, ops that no
    SCOPE/GOAL names have no derivable owner and are not checked, and a same-named helper subcommand (session-open in
    C7h) counts as delivery."""
    ops = [r[0] for r in json.loads((ROOT / "contracts/v2/rpc-ops.schema.json").read_text(encoding="utf-8"))["x-ops"][1:] if "-" in r[0]]
    owners: dict[str, str] = {}
    for sec in re.split(r"\n### ", text.split("## Briefs", 1)[1])[1:]:
        head = sec.split(":", 1)[0].strip()
        if head not in POS:
            continue
        scope = " ".join(l for l in sec.splitlines() if l.startswith(("- SCOPE", "- GOAL")) or (l.startswith("  - ") and "ACCEPTANCE" not in l))
        for op in ops:
            if re.search(rf"(?<![\w-]){re.escape(op)}(?![\w-])", scope) and (op not in owners or POS[head] < POS[owners[op]]):
                owners[op] = head
    return owners


def capability_map(text: str) -> dict[str, str]:
    return {**op_owners(text), **HAND_CAPABILITIES}


def _expand(token: str) -> list[str]:
    token = token.strip()
    m = re.fullmatch(r"([A-C][0-9][0-9a-z]*)-([A-C][0-9][0-9a-z]*)", token)
    if m and m.group(1) in POS and m.group(2) in POS:
        return IDS[POS[m.group(1)]: POS[m.group(2)] + 1]
    return [token] if token in POS else []


def plan_prerequisite_problems(text: str) -> list[str]:
    problems: list[str] = []
    table = text.split("| Test (ACCEPTANCE-TESTS) | Needs | Runs at |", 1)[1].split("\n\n", 1)[0]
    rows: dict[str, dict] = {}
    for line in table.splitlines()[2:]:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 3:
            continue
        ids = re.findall(r"\bT[0-9]{2}[a-z]?(?:-[a-z])?\b", cells[0].split(" ", 1)[0]) or [cells[0].split(" ", 1)[0]]
        variant = "current" if "current rule" in cells[0] else "target" if "target rule" in cells[0] else None
        needs = [p for tok in re.split(r"[,;]\s*", re.sub(r"\(.*?\)", "", cells[1])) for p in _expand(tok)]
        runs = [r for r in re.findall(r"\b[A-C](?:[0-9][0-9a-z]*|-ASM)\b", cells[2]) if r in POS]
        for tid in ids:  # rev 3: a test id can have NAMED variants (T05 current rule / target rule)
            rows.setdefault(tid, []).append({"needs": needs, "runs": runs, "variant": variant})
        if needs and runs and max(POS[n] for n in needs) > min(POS[r] for r in runs):
            problems.append(f"table row {cells[0][:40]!r}: runs at {runs} before its prerequisite {max(needs, key=POS.get)}")
    sections = re.split(r"\n### ", text.split("## Briefs", 1)[1])
    for sec in sections[1:]:
        head = sec.split(":", 1)[0].strip()
        if head not in POS:
            continue
        acceptance = " ".join(l for l in sec.splitlines() if "ACCEPTANCE" in l or l.startswith("  - On every") or l.startswith("    - "))
        for m in re.finditer(r"\bT[0-9]{2}[a-z]?(?:-[a-z])?\b", acceptance):
            ref = m.group(0)
            keys = [k for k in rows if k == ref or (ref == "T11a-d" and k in {"T11a-c", "T11d"})]
            if not keys:
                problems.append(f"{head}: acceptance names {ref}, which is not a row of the acceptance table (split or renamed?)")
                continue
            for k in keys:
                variants = rows[k]
                if len(variants) > 1:  # rev 3: the reference must name its variant, and THAT variant is checked
                    q = re.match(r"\s*\((current|target)", acceptance[m.end():])
                    if not q:
                        problems.append(f"{head}: acceptance names {ref} without its variant (current/target)")
                        continue
                    variants = [v for v in variants if v["variant"] == q.group(1)]
                for v in variants:
                    if v["needs"] and max(POS[n] for n in v["needs"]) > POS[head]:
                        problems.append(f"{head}: acceptance needs {ref}, whose prerequisite {max(v['needs'], key=POS.get)} comes later")
        for cap, packet in capability_map(text).items():
            if re.search(rf"(?<![\w-]){re.escape(cap)}(?![\w-])", acceptance) and POS[head] < POS[packet]:
                problems.append(f"{head}: acceptance uses {cap}, delivered only by the later packet {packet}")
    return problems


def test_r2_every_acceptance_prerequisite_is_delivered_earlier() -> None:
    """BOUNDED guard (rev 3, wording per Sol 6.1 amd2r2): checks the acceptance-table schedule, every T-number (with its
    named variant) in packet and assembly acceptance text, the hyphenated RPC ops whose owner a SCOPE/GOAL names
    (op_owners) and the hand-listed T04b. It does NOT see plain-word ops, ops no SCOPE/GOAL names, or capabilities
    described in free prose."""
    assert plan_prerequisite_problems(SLICES) == []
    owners = op_owners(SLICES)
    assert owners["job-logs"] == "C7c" and owners["job-output-get"] == "C7c" and owners["job-submit"] == "C7b1"


@pytest.mark.parametrize("label,old,new", [
    ("bare T04 at B-ASM (Sol's case)", "re-runs of T01, T04a (the holder variant)", "re-runs of T01, T04 (the holder variant)"),
    ("authenticated fetch back in C7b1", "and output-collect copies it into the executor-owned store;", "and the owner fetches it with job-output-get;"),
    ("T04b scheduled at A-ASM", "| C7b1, A5a | C7b1 (onlab); per lane at C-ASM |", "| C7b1, A5a | A-ASM; per lane at C-ASM |"),
    ("job-logs in C7b1 acceptance (Sol 6.1 amd2r2)", "`test_job_state_machine_success_transitions`;", "`test_job_state_machine_success_transitions`; job-logs streamed to the owner;"),
    ("A-ASM T05 switched to the target variant (Sol 6.1 amd2r2)", "T05 (current rule), T06a, T06b, T07, T08, live on the pilot lane.", "T05 (target rule), T06a, T06b, T07, T08, live on the pilot lane."),
    ("A-ASM T05 without its variant", "T05 (current rule), T06a, T06b, T07, T08, live on the pilot lane.", "T05, T06a, T06b, T07, T08, live on the pilot lane."),
])
def test_r2_plan_check_catches_prerequisite_inversions(label, old, new) -> None:
    assert old in SLICES, label
    assert plan_prerequisite_problems(SLICES.replace(old, new, 1)), label


def test_r2_no_bare_t04_anywhere_active() -> None:
    for name in ("docs/v2/SLICES.md", "docs/v2/GATES.txt", "docs/v2/CONFORMANCE.tsv"):
        lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
        for line in lines:
            if not re.search(r"\bT04\b(?![ab])", line):
                continue
            # allowed: the split notes themselves and the historical finding row that motivated the split
            assert "SPLIT NOTE" in line or line.startswith(("sol61-P1-5\t", "sol61r2-4\t")), f"{name}: bare T04 in {line[:120]!r}"


# ================================================================ rev 2: per-card context type; conformance binding

def test_r2_same_pid_mixed_context_types_card_b_is_a_tenant() -> None:
    A, B = U, "GPU-00000000-0000-0000-0000-000000000012"
    gpus = "".join(f"{u}, 10, 15360, 0, 40, 30.00, 70.00, Not Active, Not Active, [N/A]\n" for u in (A, B))
    procs = f"{A}, 44, /usr/lib/browser/browser, 5\n{B}, 44, /usr/lib/browser/browser, 5\n"
    o = occupancy_from_capture(gpus, procs, returncode=0, lane_id="l", host_id="h", observed_at="2026-10-02T00:00:00Z", lane_uuids=[A, B],
                               noise_allowlist=ALLOW, noise_cap_mib=64, identities={44: {"uid": 1000, "argv0": "/usr/lib/browser/browser"}},
                               context_types={(A, 44): "C+G", (B, 44): "C"})
    assert o["empty"] is False and [(p["gpu_uuid"], p["pid"]) for p in o["tenants"]] == [(B, 44)]
    assert [(p["gpu_uuid"], p["context_type"]) for p in o["noise"]] == [(A, "C+G")] and occupancy_semantics(o) == []


# receivers in the conformance skeletons -> the port they are bound to (per file); anything else must be a fixture helper
CONFORMANCE_RECEIVERS = {
    "test_clock_and_commands.py": {"clock": "Clock", "runner": "CommandRunner"},
    "test_identity_and_signing.py": {"ident": "PeerIdentity", "signer": "Signer"},
    "test_occupancy_probe.py": {"probe": ("OccupancyProbe", "InventoryProbe")},
    "test_power.py": {"inh": "Inhibitor", "waker": "Waker"},
    "test_work_support.py": {"backend": "ReleaseBackend", "cache": "ModelCache", "gw": "SessionGateway", "notifier": "Notifier",
                             "obs": "LegacyObserver", "probe": "HealthProbe"},
    "test_workload_runner.py": {"runner": "WorkloadRunner"},
    "test_session_gateway_c9.py": {"gw": "SessionGateway"},  # Amendment 3 rev 2: C9's new file (its touch set), mapped in R1
    "test_executor_transport.py": {"rig.transport": "ExecutorTransport"},  # rev 3: dotted receivers are bound too
}
NOT_PORTS = {"registry", "rig", "re", "time", "boot", "reply", "sb", "UNIT"}


def conformance_binding_problems(sources: dict[str, str] | None = None) -> list[str]:
    import ast

    ns = _ports()
    port_methods = {name: {m for m, f in vars(cls).items() if callable(f) and not m.startswith("_")}
                    for name, cls in ns.items() if inspect.isclass(cls) and getattr(cls, "_is_protocol", False)}
    all_methods = set().union(*port_methods.values())
    problems = []
    folder = ROOT / "tests/conformance"
    sources = sources or {p.name: p.read_text(encoding="utf-8") for p in sorted(folder.glob("test_*.py"))}
    for fname, src in sources.items():
        mapping = CONFORMANCE_RECEIVERS.get(fname)
        if mapping is None:
            problems.append(f"{fname}: conformance file with no receiver mapping")
            continue
        for node in ast.walk(ast.parse(src)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            # rev 3 (Sol 6.1 amd2r2): resolve dotted receivers (rig.transport.call) to their full path
            parts, cur = [], node.func.value
            while isinstance(cur, ast.Attribute):
                parts.insert(0, cur.attr)
                cur = cur.value
            if not isinstance(cur, ast.Name):
                continue
            recv, meth = ".".join([cur.id] + parts), node.func.attr
            if recv in NOT_PORTS or recv.startswith("_"):
                continue
            ports = mapping.get(recv)
            if ports is None:
                if meth in all_methods:
                    problems.append(f"{fname}:{node.lineno}: {recv}.{meth}() looks like a port call but {recv} is not mapped")
                continue
            ports = (ports,) if isinstance(ports, str) else ports
            owner = [p for p in ports if meth in port_methods[p]]
            if not owner:
                continue  # a fixture helper on the implementation (script_next, test_* attributes, ...)
            if any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords):
                problems.append(f"{fname}:{node.lineno}: {recv}.{meth}() uses *args/**kwargs: cannot be checked")
                continue
            fn = getattr(ns[owner[0]], meth)
            try:
                inspect.signature(fn).bind(None, *[None] * len(node.args), **{k.arg: None for k in node.keywords})
            except TypeError as exc:
                problems.append(f"{fname}:{node.lineno}: {owner[0]}.{meth}(): {exc}")
    return problems


def test_r2_conformance_skeleton_calls_bind_to_the_declared_signatures() -> None:
    """Runs in CI now (not skipped): the port calls in the (currently skipped) conformance skeletons must bind to the
    declared Protocol signature, so signature drift fails before a packet un-skips them (Sol 6.1 amd2 regression).
    BOUNDED (rev 3): it checks arity and keyword names of calls on mapped receivers, simple or dotted (rig.transport),
    and flags unmapped receivers that call a port-method name; it does NOT check argument values, nor typos of
    method names on a mapped receiver (they read as fixture helpers)."""
    assert conformance_binding_problems() == []


def test_r2_binding_check_catches_sols_close_regression() -> None:
    src = (ROOT / "tests/conformance/test_work_support.py").read_text(encoding="utf-8")
    assert ", parent_lease_id=gw.test_parent_lease, timeout_s" in src
    broken = src.replace("gw.close(", "gw.close(", 1)
    broken = re.sub(r"(gw\.close\([^)]*?), parent_lease_id=gw\.test_parent_lease", r"\1", broken, count=1)
    assert broken != src
    assert any("parent_lease_id" in p for p in conformance_binding_problems({"test_work_support.py": broken}))


def test_r2_missing_per_card_type_never_borrows_another_cards_type() -> None:
    A, B = U, "GPU-00000000-0000-0000-0000-000000000012"
    gpus = "".join(f"{u}, 10, 15360, 0, 40, 30.00, 70.00, Not Active, Not Active, [N/A]\n" for u in (A, B))
    procs = f"{A}, 44, /usr/lib/browser/browser, 5\n{B}, 44, /usr/lib/browser/browser, 5\n"
    o = occupancy_from_capture(gpus, procs, returncode=0, lane_id="l", host_id="h", observed_at="2026-10-02T00:00:00Z", lane_uuids=[A, B],
                               noise_allowlist=ALLOW, noise_cap_mib=64, identities={44: {"uid": 1000, "argv0": "/usr/lib/browser/browser"}},
                               context_types={(A, 44): "C+G"})  # no type reported for card B
    assert o["empty"] is False and [(p["gpu_uuid"], p["context_type"]) for p in o["tenants"]] == [(B, None)]


def test_r3_binding_check_catches_a_dotted_receiver_regression() -> None:
    """Sol 6.1 amd2r2: removing the required timeout from rig.transport.call(...) was accepted by the simple-name guard."""
    src = (ROOT / "tests/conformance/test_executor_transport.py").read_text(encoding="utf-8")
    good = "rig.transport.call(rig.host_id, rig.reserve_request(generation=1, expiry_in_s=120), timeout_s=20)"
    assert good in src and conformance_binding_problems({"test_executor_transport.py": src}) == []
    broken = src.replace(good, "rig.transport.call(rig.host_id, rig.reserve_request(generation=1, expiry_in_s=120))", 1)
    assert any("timeout_s" in p for p in conformance_binding_problems({"test_executor_transport.py": broken}))
    renamed = src.replace("rig.transport.call(", "rig.channel.call(", 1)  # an unmapped dotted receiver calling a port method
    assert any("not mapped" in p for p in conformance_binding_problems({"test_executor_transport.py": renamed}))
