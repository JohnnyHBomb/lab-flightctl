"""Amendment 12 (wave-3 staging of A5a2 and A11): request validation moves from A5a2 to a new A5a2b; A11 keeps the
Inhibitor twins and strict conformance, and a new A11b gets the executor reconcile and the on-lab guard test."""

from pathlib import Path

from tests.conformance.registry import OWED_BY
from tools import migration_gate

from .test_v2_plan import POS, _crosswalk_order

ROOT = Path(__file__).resolve().parents[2]
SLICES = (ROOT / "docs/v2/SLICES.md").read_text(encoding="utf-8")
MAP = migration_gate.load_rows(ROOT / "docs/v2/migration-map.tsv")


def _row(packet: str) -> list[str]:
    line = next(l for l in SLICES.splitlines() if l.startswith(f"| {packet} |"))
    return [c.strip() for c in line.strip("|").split("|")]


def _section(header: str) -> str:
    return SLICES.split(f"\n### {header}", 1)[1].split("\n### ", 1)[0]


def _field(header: str, name: str) -> str:
    return next(l for l in _section(header).splitlines() if l.startswith(f"- {name}"))


def _named(header: str) -> list[str]:
    acceptance = _field(header, "ACCEPTANCE")
    return [part.split("`")[1] for part in acceptance.split(";") if "`" in part]


def test_a5a2_is_split_and_a5a2b_owns_validation() -> None:
    # A5a2's three named tests are the ones its race was judged on and merged with
    assert _named("A5a2:") == ["test_definite_refusal_leaves_no_fence_and_no_inhibitor",
                               "test_ceiling_shortens_only_and_extend_needs_approval", "test_inspect_reports_holder_unit_absent"]
    assert "`test_ceiling_shortens_only_and_extend_needs_approval` [realtime]" in _field("A5a2:", "ACCEPTANCE")
    assert _named("A5a2b:") == ["test_invalid_requests_are_definite_and_write_nothing"]
    assert "validat" not in _field("A5a2:", "GOAL") + _field("A5a2:", "SCOPE")
    assert "released only after the verified release" in _field("A5a2:", "GOAL")
    assert (_row("A5a2b")[4], _row("A5a2b")[6]) == ("A5a2", "-")
    assert POS["A5a2"] + 1 == POS["A5a2b"] < POS["A5a3"] < POS["A5b1"]
    assert _row("A5a3")[4] == "A5a2, A4u" and _row("A5b1")[4] == "A5a3, A5a2b"
    assert not any(r["packet"] in {"A5a2", "A5a2b"} for r in MAP)  # neither part owns a migration row


def test_a11_is_split_and_a11b_owns_reconcile_and_the_guard_test() -> None:
    assert OWED_BY["inhibitor"] == "A11" and _row("A11")[6] == "inhibitor" and _row("A11b")[6] == "-"
    assert (_row("A11")[4], _row("A11b")[4], _row("A12")[4]) == ("A5a2, A6", "A11", "A11, A7b, A10")
    assert POS["A11"] + 1 == POS["A11b"] < POS["A12"] < POS["A-ASM"]
    assert _named("A11:") == ["test_inhibitor_failure_is_definite_refusal", "test_real_twin_runs_the_contract_commands",
                              "test_dryrun_twin_lists_for_real_and_records_the_rest"]
    assert _field("A11:", "ACCEPTANCE").startswith("- ACCEPTANCE: inhibitor conformance [strict];")
    assert _named("A11b:") == ["test_reconcile_recreates_missing_and_removes_orphan_inhibitors", "test_guard_sees_inhibitor"]
    assert "`test_guard_sees_inhibitor` [realtime, onlab" in _field("A11b:", "ACCEPTANCE")
    assert "executor" not in _field("A11:", "SCOPE") and "flightctl/executor.py" in _field("A11b:", "SCOPE")
    order = _crosswalk_order()
    assert order.index("A11") < order.index("A11b") < order.index("A12") < order.index("A-ASM")
    assert not any(r["packet"] in {"A11", "A11b"} for r in MAP)
