"""Amendment 6: executor_transport conformance is owed by A5a (the v2 executor), not A4. Amendment 9: by A5a3, the wire
part of the A5a split."""

from pathlib import Path

from tests.conformance.registry import OWED_BY

ROOT = Path(__file__).resolve().parents[2]
SLICES = (ROOT / "docs/v2/SLICES.md").read_text(encoding="utf-8")


def _row(packet: str) -> list[str]:
    line = next(l for l in SLICES.splitlines() if l.startswith(f"| {packet} |"))
    return [c.strip() for c in line.strip("|").split("|")]


def _acceptance(header: str) -> str:
    section = SLICES.split(f"### {header}", 1)[1].split("\n### ", 1)[0]
    return next(l for l in section.splitlines() if l.startswith("- ACCEPTANCE:"))


def test_a5a_owes_executor_transport_conformance() -> None:
    assert OWED_BY["executor_transport"] == "A5a3"  # Amendment 9
    assert _row("A4")[6] == "-" and _row("A5a")[6] == "-" and _row("A5a3")[6] == "executor_transport"
    assert "executor_transport conformance [strict]" in _acceptance("A5a3:")
    assert "executor_transport conformance" not in _acceptance("A5a:")


def test_a4_acceptance_names_the_typed_failure_test_not_conformance() -> None:
    a4 = _acceptance("A4:")
    assert "`test_transport_failures_are_typed_and_bounded`" in a4
    assert "executor_transport conformance [strict];" not in a4


def test_a5a_owns_the_a4_transport_rows() -> None:
    rows = [l.split("\t") for l in (ROOT / "docs/v2/migration-map.tsv").read_text(encoding="utf-8").splitlines()
            if l.startswith("tests/transport/test_a4_transport.py::")]
    assert {(r[1], r[2], r[3]) for r in rows} == {("A5a3", "rewrite", n) for n in (  # Amendment 9: A5a3
        "test_one_shot_invocations_keep_state", "test_garbage_is_unparsable_not_ok",
        "test_transport_failures_are_typed_and_bounded", "test_wrong_key_denied")}
