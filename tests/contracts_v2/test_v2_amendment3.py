"""Amendment 3 (owner's C9 decision): R1 delivers every pathway C9 uses; C9 (milestone D) builds only bodies behind them
and may touch only its declared new paths plus three named documentation extension points.

Rev 2 (Sol 6.1 amd3): the touch set and the pathway table are PINNED here (this file is an R1 file outside C9's touch
set, so C9 cannot widen either); the new paths are checked ABSENT AT THE R1 BASE (the release tag v2-r1), not in the
current checkout, so a legal C9 build passes this suite; the extension points are checked line by line by
tools/touch_set_gate.py; the cleanup bodies are declared real and exercised on seeded state.
BOUNDED: whether C9's prose inside its own brief, its CONFORMANCE notes and its migration reasons is accurate is review."""

import os
import re
import subprocess
from pathlib import Path

import pytest

from tools import touch_set_gate

from .test_v2_plan import POS, ROWS

ROOT = Path(__file__).parents[2]
SLICES = (ROOT / "docs/v2/SLICES.md").read_text(encoding="utf-8")
CONFORMANCE = (ROOT / "docs/v2/CONFORMANCE.tsv").read_text(encoding="utf-8")
MIGRATION = (ROOT / "docs/v2/migration-map.tsv").read_text(encoding="utf-8")
MILESTONE = {r["id"]: r["milestone"] for r in ROWS}
R1 = {pid for pid, m in MILESTONE.items() if m in {"A", "B", "C"}}
R1_BASE_TAG = "v2-r1"

# ---- the reviewed baseline (rev 2). Changing any of these is an R1 contract change, never a C9 change.
PINNED_NEW = ("flightctl/c9/", "helper/c9/", "tools/c9_runbook/", "tests/c9/", "tests/conformance/test_session_gateway_c9.py")
PINNED_EXTENSIONS = {
    "docs/v2/CONFORMANCE.tsv": "(only the status and note of rows homed at C9)",
    "docs/v2/migration-map.tsv": "(append rows owned by C9; earlier rows unchanged)",
    "docs/v2/SLICES.md": "(the C9 brief only, except its TOUCH SET and R1 PATHWAYS USED blocks)",
}
PINNED_PATHWAYS = frozenset({
    ("session RPC ops", "C9w", "`session-open`"),
    ("key enrolment and fingerprint resolution", "C9w", "`resolve_session_key`"),
    ("SessionGateway port and real wire", "C9w", "`tests/conformance/impl_session_gateway.py`"),
    ("executor session lifecycle", "C9w", "`executor_session_closes`"),
    ("session-to-parent attribution", "C9w", "`session_attribution`"),
    ("authority friend gates", "C9w", "`authority_admits_friend_work`"),
    ("global / per-host / conjunction flags", "C9w", "`friend_sessions_consistent`"),
    ("disable ordering", "C9w", "`friend_sessions_disable`"),
    ("helper dispatch points", "C9w", "registered dispatch points"),
    ("probe sweep", "C9w", "`flightctl-session-probe-sweep.timer`"),
    ("behaviour seams", "C9w", "`flightctl/c9_seams.py`"),
    ("verified seam loading", "C9w", "`c9_seam_load`"),
    ("enablement window (probe and measurement before enablement)", "C9w", "`c9_window_open`"),
    ("C9 package absence and stale removal", "C9w", "`release_stale_removals`"),
    ("claim store (read, reconcile, release)", "C7h", "`claim-reconcile`"),
    ("claim-clear program and operator rule", "C7h", "`flightctl-claim-clear`"),
    ("session-probe program and operator rule", "C7h", "`flightctl-session-probe`"),
    ("production install audit", "C7h", "`flightctl/install_audit.py`"),
    ("device isolation", "C7h", "DeviceAllow"),
    ("friend unit-start branch", "C7h", "`--parent-lease`"),
    ("job-output collection for friend jobs", "C7h", "`output-collect`"),
})
# cleanup body -> (delivering R1 packet, the seeded R1 test that exercises it)
CLEANUP_BODIES = {
    "`session-close`": ("C7h", "test_seeded_session_close_proof"),
    "`claim-release`": ("C7h", "test_seeded_claim_release_and_reconcile"),
    "`claim-reconcile`": ("C7h", "test_seeded_claim_release_and_reconcile"),
    "the probe `--sweep`": ("C9w", "test_probe_sweep_requires_real_monotonic_clock"),
}
CLEANUP_DECLARATION = ("CLEANUP BODIES ARE REAL, NOT STUBS: `session-close`, `claim-release`, `claim-reconcile` (C7h) and the probe "
                       "`--sweep` (C9w), each proven on seeded state by a named R1 test")


def brief(packet: str, text: str = SLICES) -> str:
    return text.split(f"\n### {packet}:", 1)[1].split("\n### ", 1)[0]


def touch_block_entries(text: str) -> tuple[list[str], dict[str, str]]:
    block = brief("C9", text).split("- TOUCH SET", 1)[1].split("\n- ", 1)[0]
    new = re.findall(r"^\s+- new: `([^`]+)`\s*$", block, flags=re.M)
    ext = dict(re.findall(r"^\s+- extension: `([^`]+)` (.*)$", block, flags=re.M))
    return new, ext


def touch_set_problems(text: str = SLICES) -> list[str]:
    """The declared touch set equals the pinned baseline; no R1 brief names a new path; the extension files exist."""
    problems = []
    new, ext = touch_block_entries(text)
    lines = [l for l in brief("C9", text).split("- TOUCH SET", 1)[1].split("\n- ", 1)[0].splitlines()[1:] if l.strip()]
    if len(lines) != len(new) + len(ext):
        problems.append("the TOUCH SET block has entries that are neither 'new' nor 'extension'")
    if sorted(new) != sorted(PINNED_NEW):
        problems.append(f"new paths {sorted(set(new) ^ set(PINNED_NEW))} differ from the pinned baseline")
    if ext != PINNED_EXTENSIONS:
        problems.append("extension points or their scopes differ from the pinned baseline")
    for path in new:
        for pid in R1:
            if pid in POS and f"\n### {pid}:" in text and path in brief(pid, text):
                problems.append(f"new path {path} is named by R1 packet {pid}")
    for path in ext:
        if not (ROOT / path).exists():
            problems.append(f"extension point {path} does not exist")
    return problems


def _files_under(root: Path, entry: str) -> list[str]:
    target = root / entry.rstrip("/")
    if target.is_file():
        return [entry]
    if not target.is_dir():
        return []
    return sorted(p.relative_to(root).as_posix() for p in target.rglob("*") if p.is_file() and "__pycache__" not in p.parts)


def r1_base_problems(root: Path = ROOT, new_paths=PINNED_NEW, tag: str = R1_BASE_TAG) -> list[str]:
    """P1-1: a new path may exist in the checkout only as an addition AFTER the R1 base: the tag must exist and hold no
    file under that path. Before R1 is tagged, any file under a new path would be an R1 file and is refused."""
    present = {e: _files_under(root, e) for e in new_paths}
    if not any(present.values()):
        return []

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=60, check=False)

    if git("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}").returncode != 0:
        first = next(e for e, f in present.items() if f)
        return [f"{first} has files but the R1 base tag {tag} is absent: before R1 is tagged these would be R1 files "
                f"(fetch the tag if this is a C9 checkout)"]
    problems = []
    for entry, files in present.items():
        if not files:
            continue
        at_base = git("ls-tree", "-r", "--name-only", tag, "--", entry.rstrip("/")).stdout.split()
        if at_base:
            problems.append(f"{entry} existed at the R1 base {tag} ({at_base[0]}); it is an R1 path, not a C9 addition")
    return problems


def pathway_rows(text: str) -> list[tuple[str, str, str]]:
    table = brief("C9", text).split("| Capability | R1 deliverer | Pathway |", 1)[1].split("\n\n", 1)[0]
    rows = [tuple(c.strip() for c in l.strip().strip("|").split("|")) for l in table.splitlines()[2:] if l.strip().startswith("|")]
    return rows


def pathway_problems(text: str = SLICES) -> list[str]:
    """The pathway table equals the pinned canonical set (count AND identity, distinct capabilities and pathways), and
    every deliverer is an R1 packet whose brief names its pathway."""
    problems = []
    rows = pathway_rows(text)
    if any(len(r) != 3 for r in rows):
        return ["a pathway row does not have three cells"]
    if len(rows) != len(PINNED_PATHWAYS) or set(rows) != PINNED_PATHWAYS:
        problems.append(f"{len(rows)} rows; they differ from the pinned {len(PINNED_PATHWAYS)}-row baseline: "
                        f"{sorted(set(rows) ^ PINNED_PATHWAYS)[:3]}")
    if len({r[0] for r in rows}) != len(rows) or len({r[2] for r in rows}) != len(rows):
        problems.append("capabilities and pathways must be distinct")
    for cap, deliverer, pathway in rows:
        if deliverer not in R1:
            problems.append(f"{cap}: deliverer {deliverer} is not an R1 packet")
            continue
        if pathway.strip("`") not in brief(deliverer, text):
            problems.append(f"{cap}: {deliverer}'s brief does not name the pathway {pathway}")
    return problems


def cleanup_problems(text: str = SLICES) -> list[str]:
    """The cleanup bodies are declared real in C9w and each has its seeded R1 test in the delivering packet's brief."""
    problems = []
    if CLEANUP_DECLARATION not in brief("C9w", text):
        problems.append("C9w does not declare the cleanup bodies real (pinned sentence)")
    for body, (packet, test) in CLEANUP_BODIES.items():
        acceptance = brief(packet, text).split("- ACCEPTANCE", 1)[-1]
        if f"`{test}`" not in acceptance:
            problems.append(f"{body}: {packet}'s acceptance does not name the seeded test {test}")
    return problems


def test_a3_c9_is_out_of_r1_and_c9w_is_in() -> None:
    assert MILESTONE["C9"] == "D" and MILESTONE["C9w"] == "C"
    assert POS["C9w"] < POS["C-ASM"] < POS["C9"]
    casm = next(r for r in ROWS if r["id"] == "C-ASM")
    assert "C9w" in casm["depends"]
    from tests.conformance import registry
    assert registry.OWED_BY["session_gateway"] == "C9w"
    assert f"`{R1_BASE_TAG}`" in brief("C-ASM") and f"`{R1_BASE_TAG}`" in brief("C9")


def test_a3_c9_touch_set_is_pinned_and_closed() -> None:
    touch = touch_set_gate.load_touch_set(SLICES, "C9")
    assert sorted(touch["new"]) == sorted(PINNED_NEW) and set(touch["extension"]) == set(PINNED_EXTENSIONS)
    assert touch_set_problems() == []


def test_a3_c9_new_paths_absent_at_the_r1_base() -> None:
    assert r1_base_problems() == []


def _git(cwd: Path, *args: str) -> None:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, timeout=60, env=env)


def _write(root: Path, rel: str, text: str = "x\n") -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text, encoding="utf-8")


def test_a3_r1_base_check_on_simulated_trees(tmp_path: Path) -> None:
    """P1-1 reproduction and fix: the five legal C9 additions after the tag pass; before the tag they are refused; a file
    that already existed at the tag is refused."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _write(repo, "flightctl/authority.py")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "r1")
    assert r1_base_problems(repo) == []
    adds = ["flightctl/c9/sessions.py", "helper/c9/claims.py", "tools/c9_runbook/sshd.py", "tests/c9/test_s.py",
            "tests/conformance/test_session_gateway_c9.py"]
    for rel in adds:
        _write(repo, rel)
    assert r1_base_problems(repo), "files under the new paths before the R1 tag must be refused"
    for rel in adds:
        (repo / rel).unlink()
    _git(repo, "tag", R1_BASE_TAG)
    for rel in adds:
        _write(repo, rel)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c9")
    assert r1_base_problems(repo) == []
    _git(repo, "tag", "-d", R1_BASE_TAG)
    _git(repo, "tag", R1_BASE_TAG)  # now the tag itself carries the C9 files: they would be R1 files
    assert r1_base_problems(repo)


def test_a3_c9_brief_names_no_r1_file_outside_its_touch_set_and_pathways() -> None:
    touch = touch_set_gate.load_touch_set(SLICES, "C9")
    c9 = brief("C9")
    pathways = {r[2].strip("`") for r in pathway_rows(SLICES)}
    for path in re.findall(r"`((?:flightctl|helper|tools|tests)/[^`\s]+)`", c9):
        gates = {"tools/touch_set_gate.py", "tools/migration_gate.py", "tests/contracts_v2/test_v2_amendment3.py"}  # read-only references
        ok = (any(path == e or (e.endswith("/") and path.startswith(e)) or path.startswith(e + "::") for e in touch["new"])
              or path in pathways or path in touch["extension"] or path in gates)
        assert ok, f"C9 brief names {path}, which is neither in its touch set nor a listed R1 pathway"


def test_a3_every_c9_capability_resolves_to_an_r1_pathway() -> None:
    assert pathway_problems() == []


FIRST_FIVE = "".join(f"  | {c} | {d} | {p} |\n" for c, d, p in [
    ("session RPC ops", "C9w", "`session-open`"), ("key enrolment and fingerprint resolution", "C9w", "`resolve_session_key`"),
    ("SessionGateway port and real wire", "C9w", "`tests/conformance/impl_session_gateway.py`"),
    ("executor session lifecycle", "C9w", "`executor_session_closes`"), ("session-to-parent attribution", "C9w", "`session_attribution`")])


@pytest.mark.parametrize("label,old,new", [
    ("deliverer is C9 itself", "| claim-clear program and operator rule | C7h |", "| claim-clear program and operator rule | C9 |"),
    ("pathway the deliverer does not name", "| disable ordering | C9w | `friend_sessions_disable` |", "| disable ordering | C9w | `friend_sessions_disable_v2` |"),
    ("Sol amd3: drop the first five capabilities", FIRST_FIVE, ""),
    ("Sol amd3: seams pathway replaced by a duplicate", "| behaviour seams | C9w | `flightctl/c9_seams.py` |", "| behaviour seams | C9w | `tests/conformance/impl_session_gateway.py` |"),
    ("an extra capability", "| device isolation | C7h | DeviceAllow |", "| device isolation | C7h | DeviceAllow |\n  | friend admin tool | C9w | `session-open` |"),
])
def test_a3_pathway_check_catches(label, old, new) -> None:
    assert old in SLICES, label
    assert pathway_problems(SLICES.replace(old, new, 1)), label


@pytest.mark.parametrize("label,old,new", [
    ("an R1 code file as an extension point", "  - extension: `docs/v2/SLICES.md` (the C9 brief only",
     "  - extension: `flightctl/authority.py` (friend admission)\n  - extension: `docs/v2/SLICES.md` (the C9 brief only"),
    ("an existing R1 path as 'new'", "  - new: `tests/c9/`", "  - new: `tests/c9/`\n  - new: `flightctl/`"),
    ("an existing path no R1 brief names, as 'new'", "  - new: `tests/c9/`", "  - new: `tests/c9/`\n  - new: `ondemand/`"),
    ("Sol amd3: whitelist expanded with another absent path", "  - new: `tests/c9/`", "  - new: `tests/c9/`\n  - new: `flightctl/friend_admin/`"),
    ("an extension scope widened", "(append rows owned by C9; earlier rows unchanged)", "(any edit)"),
    ("an entry of an unknown kind", "  - new: `tests/c9/`", "  - new: `tests/c9/`\n  - edit: `flightctl/authority.py`"),
])
def test_a3_touch_set_check_catches(label, old, new) -> None:
    assert old in SLICES, label
    assert touch_set_problems(SLICES.replace(old, new, 1)), label


def _c9_homed_row() -> str:
    return next(l for l in CONFORMANCE.splitlines() if l.startswith("amd1r9-review\t"))


def test_a3_touch_set_gate_on_simulated_c9_diffs() -> None:
    touch = touch_set_gate.load_touch_set(SLICES, "C9")
    row = _c9_homed_row()
    cells = row.split("\t")
    conf_ok = CONFORMANCE.replace(row, "\t".join(cells[:3] + ["contract-fixed"] + cells[4:5] + ["C9 review passed"]), 1)
    mig_ok = MIGRATION + "tests/c9/test_s.py::test_x\tC9\trewrite\ttest_x\tC9 body\n"
    c9_text = brief("C9")  # independent of the C9 brief's own prose, which C9 may legally edit
    slices_ok = SLICES.replace(c9_text, c9_text.rstrip("\n") + "\n- NOTE (simulated C9 edit inside its own brief).\n", 1)
    assert slices_ok != SLICES and touch_set_gate._split_brief(slices_ok, "C9")[0] == touch_set_gate._split_brief(SLICES, "C9")[0]
    ok_contents = {"docs/v2/CONFORMANCE.tsv": (CONFORMANCE, conf_ok), "docs/v2/migration-map.tsv": (MIGRATION, mig_ok),
                   "docs/v2/SLICES.md": (SLICES, slices_ok)}
    good = {"flightctl/c9/sessions.py": "A", "helper/c9/claim_clear.py": "A", "tools/c9_runbook/sshd.py": "A", "tests/c9/test_sessions.py": "A",
            "tests/conformance/test_session_gateway_c9.py": "A", "docs/v2/CONFORMANCE.tsv": "M", "docs/v2/migration-map.tsv": "M",
            "docs/v2/SLICES.md": "M"}
    assert touch_set_gate.check(good, touch, ok_contents) == []
    assert touch_set_gate.check(good, touch), "a modified extension without content must be refused (fail closed)"
    for bad in ({"flightctl/authority.py": "M"}, {"flightctl/c9_seams.py": "M"}, {"helper/flightctl-helper": "M"},
                {"tests/conformance/registry.py": "M"}, {"tests/conformance/impl_session_gateway_c9.py": "A"},
                {"docs/v2/CONFORMANCE.tsv": "D"}, {"flightctl/c9/sessions.py": "D"}):
        assert touch_set_gate.check(bad, touch), bad


C9W_ROW = next(l for l in CONFORMANCE.splitlines() if l.startswith("amd3-1\t"))


@pytest.mark.parametrize("label,path,head", [
    ("Sol amd3: a row homed elsewhere", "docs/v2/CONFORMANCE.tsv", CONFORMANCE.replace(C9W_ROW, C9W_ROW.replace("\tamended\t", "\tdropped\t"), 1)),
    ("a C9 row's v2 home rewritten", "docs/v2/CONFORMANCE.tsv", CONFORMANCE.replace(_c9_homed_row(), _c9_homed_row().replace("SLICES C9", "SLICES C9 and C7h"), 1)),
    ("a CONFORMANCE row added", "docs/v2/CONFORMANCE.tsv", CONFORMANCE + "amd9\tx\tx\tkept\tC9\tx\n"),
    ("Sol amd3: an existing migration row edited", "docs/v2/migration-map.tsv", MIGRATION.replace("\tC7h\trewrite\t", "\tC7h\tdelete\t", 1)),
    ("a migration row appended for another packet", "docs/v2/migration-map.tsv", MIGRATION + "tests/x.py\tC7h\tdelete\t-\tx\n"),
    ("Sol amd3: SLICES edited outside the C9 brief", "docs/v2/SLICES.md", SLICES.replace("### C9w: session pathways", "### C9w: session pathways (edited)", 1)),
    ("the C9 TOUCH SET block edited inside the brief", "docs/v2/SLICES.md", SLICES.replace("  - new: `tests/c9/`", "  - new: `tests/c9/`\n  - new: `flightctl/x/`", 1)),
    ("the C9 pathway table edited inside the brief", "docs/v2/SLICES.md", SLICES.replace("| device isolation | C7h | DeviceAllow |", "| device isolation | C9 | DeviceAllow |", 1)),
])
def test_a3_extension_scope_checks_catch(label, path, head) -> None:
    base = {"docs/v2/CONFORMANCE.tsv": CONFORMANCE, "docs/v2/migration-map.tsv": MIGRATION, "docs/v2/SLICES.md": SLICES}[path]
    assert head != base, label
    touch = touch_set_gate.load_touch_set(SLICES, "C9")
    assert touch_set_gate.check({path: "M"}, touch, {path: (base, head)}), label


def test_a3_cleanup_bodies_are_declared_real_and_seeded() -> None:
    assert cleanup_problems() == []
    stubbed = SLICES.replace(CLEANUP_DECLARATION, CLEANUP_DECLARATION.replace("ARE REAL, NOT STUBS", "ARE REFUSING STUBS"), 1)
    assert cleanup_problems(stubbed), "Sol amd3 mutation: cleanup bodies turned into stubs with their names kept"
    unseeded = SLICES.replace("`test_seeded_claim_release_and_reconcile` (release only", "`test_claim_release` (release only", 1)
    assert cleanup_problems(unseeded)


def test_a3_moved_tests_and_the_r1_refusal_tests() -> None:
    c7h, c9w, c9 = brief("C7h"), brief("C9w"), brief("C9")
    moved = ["test_parent_claim_lifecycle", "test_claim_rollback_unlinks_only_its_own_inode", "test_quarantine_and_start_are_serialised",
             "test_claim_clear_requires_operator_sudo_path", "test_session_probe_bounds"]
    moved_block = c9.split("- ACCEPTANCE MOVED FROM C7h", 1)[1].split("\n- ", 1)[0]
    for name in moved:
        assert f"`{name}`" in moved_block, name
    for name in ("test_claim_clear_prompts_every_time", "test_probe_sweep_requires_real_monotonic_clock", "test_seeded_claim_release_and_reconcile"):
        assert f"`{name}`" not in moved_block, f"{name} is R1 (rev 2, Sol 6.1 amd3)"
    c7h_acc = c7h.split("- ACCEPTANCE", 1)[1]
    for name in ("test_friend_subcommands_registered_and_refuse_while_off", "test_friend_flag_off_refuses_creation_allows_cleanup",
                 "test_session_close_order_key_then_terminate_then_slice_stop", "test_claim_clear_prompts_every_time",
                 "test_seeded_claim_release_and_reconcile", "test_quarantine_persists_until_operator_clear", "test_seeded_session_close_proof"):
        assert f"`{name}`" in c7h_acc, name
    assert "`test_claim_clear_prompts_every_time`: MOVED" not in c7h
    c9w_acc = c9w.split("- ACCEPTANCE", 1)[1]
    for name in ("test_authority_refuses_session_open_when_flag_off", "test_authority_refuses_friend_session_on_ineligible_host",
                 "test_helper_refuses_without_both_flags_and_live_binding", "test_disable_ordering_fail_closed",
                 "test_consistency_check_passes_before_activation", "test_c9_seams_refuse_by_default",
                 "test_session_open_refused_while_friend_flags_off", "test_session_close_on_seeded_session_proves_cleanup",
                 "test_session_key_enrol_refused_while_off_list_and_revoke_work", "test_session_open_resolves_only_own_unrevoked_key",
                 "test_session_pathway_end_to_end_through_r1_callers", "test_executor_session_register_refused_while_off_records_nothing",
                 "test_executor_closes_seeded_sessions_on_local_expiry_controller_loss_and_reconcile",
                 "test_session_attribution_by_registered_slice_never_by_uid", "test_probe_sweep_requires_real_monotonic_clock",
                 "test_r1_install_has_no_c9_packages", "test_release_removes_stale_c9_package", "test_seam_loader_policy",
                 "test_enablement_window_lifecycle", "test_loader_state_table_every_cell", "test_probe_add_only_in_the_window",
                 "test_trusted_startup_and_import_guard"):
        assert f"`{name}`" in c9w_acc, name


# ---- P1-2: the R1 session conformance cases are satisfiable by a refusing twin and kill a stub cleanup body
class _Twin:
    test_host, test_user, test_parent_lease, test_lane = "host-1", "fc-conformance", "lse-0000100", "lane-gpu1"
    test_public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIDR/yEIlkVI/wd5ar9zgYtW/HDPIPv9JwHjHwDY+kqRH"
    test_fingerprint = "SHA256:LA11xymOOE7XYYMCs390mZIYwTCX7SIWRqpUwai0uMY"
    test_minors, test_cards = [1], ["GPU-0"]

    def __init__(self, *, flags_on=False, open_works=False, close_works=True):
        self.flags_on, self.open_works, self.close_works, self.keys = flags_on, open_works, close_works, set()

    def in_minutes(self, m):
        return f"2026-10-02T10:{m:02d}:00Z"

    def friend_flags_on(self):
        return self.flags_on

    def host_state(self):
        return tuple(sorted(self.keys))

    def key_present(self):
        return bool(self.keys)

    def seed_session(self, host, user, key, *, parent_lease_id, lane_id):
        self.keys.add((user, parent_lease_id))

    def open(self, host, user, key, **kw):
        if self.open_works:
            self.keys.add((user, kw["parent_lease_id"]))
            return {"ok": True, "error": None}
        return {"ok": False, "error": {"code": "unavailable", "message": "friend_sessions is off"}}

    def close(self, host, user, fpr, *, parent_lease_id, timeout_s):
        if not self.close_works:
            return {"ok": False, "error": {"code": "unavailable", "message": "stub"}, "close_proof": None}
        self.keys.discard((user, parent_lease_id))
        return {"ok": True, "error": None, "close_proof": {"user_slice_empty": True, "occupancy_empty": True, "key_removed": True}}


def _conformance_module():
    import warnings
    with warnings.catch_warnings():  # the conformance markers are registered by its own conftest, not loaded here
        warnings.simplefilter("ignore", pytest.PytestUnknownMarkWarning)
        from tests.conformance import test_work_support
    return test_work_support


def _run(case, twin) -> bool:
    tws = _conformance_module()
    saved = tws._impl
    tws._impl = lambda kind, factory: factory(None)
    try:
        getattr(tws, case)("real", lambda target: twin)
        return True
    except AssertionError:
        return False
    finally:
        tws._impl = saved


def test_a3_session_conformance_against_stand_ins() -> None:
    refusal, cleanup = "test_session_open_refused_while_friend_flags_off", "test_session_close_on_seeded_session_proves_cleanup"
    assert _run(refusal, _Twin()) and _run(cleanup, _Twin()), "the refusing real twin with real cleanup must pass the R1 cases"
    assert not _run(cleanup, _Twin(close_works=False)), "a stub cleanup body must fail the seeded case"
    assert not _run(refusal, _Twin(open_works=True)), "an open that succeeds while off must fail"
    assert not _run(refusal, _Twin(flags_on=True, open_works=False)), "a flags-on target cannot pass the flags-off case"
    assert not hasattr(_conformance_module(), "test_session_window_opens_and_closes"), "the success proof is C9's (test_session_gateway_c9.py)"


def test_a3_conformance_loads_impl_files_and_declares_friend_flags(tmp_path: Path, monkeypatch) -> None:
    import sys
    from tests.conformance import conftest
    pkg = tmp_path / "implpkg_a3"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "impl_one.py").write_text("LOADED = True\n", encoding="utf-8")
    (pkg / "helper_two.py").write_text("raise RuntimeError('must not be imported')\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert conftest.load_implementations(pkg, "implpkg_a3") == ["impl_one"] and sys.modules["implpkg_a3.impl_one"].LOADED
    monkeypatch.delenv("FLIGHTCTL_CONFORMANCE_FRIEND_FLAGS", raising=False)
    assert conftest.friend_flags_state() == "off"
    monkeypatch.setenv("FLIGHTCTL_CONFORMANCE_FRIEND_FLAGS", "on")
    assert conftest.friend_flags_state() == "on"
    monkeypatch.setenv("FLIGHTCTL_CONFORMANCE_FRIEND_FLAGS", "maybe")
    with pytest.raises(pytest.UsageError):
        conftest.friend_flags_state()
