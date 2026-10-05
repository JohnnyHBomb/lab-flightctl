"""Amendment 8: the A6 linger probe reads loginctl with -p per property and skips manager sessions; A6b owns the fix."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SLICES = (ROOT / "docs/v2/SLICES.md").read_text(encoding="utf-8")
MAP = [l.split("\t") for l in (ROOT / "docs/v2/migration-map.tsv").read_text(encoding="utf-8").splitlines() if "\t" in l]
PROBE = "tests/runner/test_a6_runner.py::test_unit_and_inhibitor_survive_logout"


def _section(header: str) -> str:
    return SLICES.split(f"### {header}", 1)[1].split("\n### ", 1)[0]


def test_a6b_owns_the_probe_rewrite() -> None:
    rows = [r for r in MAP if r[0] == PROBE]
    assert len(rows) == 1 and rows[0][1:4] == ["A6b", "rewrite", "test_unit_and_inhibitor_survive_logout"]


def test_a6b_specifies_the_working_reads() -> None:
    a6b = _section("A6b:")
    assert "-p Linger`" in a6b and "list-sessions --json=short" in a6b and "`manager`" in a6b
    assert "background" not in a6b
    assert "`unreadable`" in a6b and "`inconclusive`" in a6b and "no other login session" in a6b


def test_linger_owner_actions_name_a6b() -> None:
    assert "A6's logout probe" not in (ROOT / "docs/v2/OPEN-QUESTIONS.txt").read_text(encoding="utf-8")
    assert "Answer the linger probe (A6b, Amendment 8)" in SLICES and "linger only if A6 shows" not in SLICES


def test_a6_defers_the_linger_decision_to_a6b() -> None:
    assert "the decision waits for A6b's G3" in _section("A6:")
