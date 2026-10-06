"""Amendment 7: A6 is split into A6 (real twin) and A6b (dryrun twin, FakeRunner, v1 fake retirement)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SLICES = (ROOT / "docs/v2/SLICES.md").read_text(encoding="utf-8")
MAP = [l.split("\t") for l in (ROOT / "docs/v2/migration-map.tsv").read_text(encoding="utf-8").splitlines() if "\t" in l]


def _section(header: str) -> str:
    return SLICES.split(f"### {header}", 1)[1].split("\n### ", 1)[0]


def test_a6_is_the_real_twin_and_a6b_the_fake_and_dryrun() -> None:
    a6, a6b = _section("A6:"), _section("A6b:")
    assert "`test_real_twin_reads_systemd_captures`" in a6 and "`test_fake_runner_per_unit_fail_closed`" not in a6
    assert "`test_fake_runner_per_unit_fail_closed`" in a6b and "`test_dryrun_never_starts`" in a6b
    assert 'sh -c "exit 3"' in a6
    row = next(l for l in SLICES.splitlines() if l.startswith("| A6b |"))
    assert [c.strip() for c in row.strip("|").split("|")][4] == "A6"


def test_migration_rows_follow_the_split() -> None:
    owner = {r[0]: r[1] for r in MAP}
    assert owner["tests/executor/systemd_adapter.py"] == "A5r"  # Amendment 9: A5r retires executor v1 (was A5a)
    assert owner["tests/executor/test_executor.py::test_fake_isolation_guard"] == "A6b"
    assert owner["tests/contracts/test_fakes.py::test_fake_interfaces"] == "A6b"


def test_a5a_owns_the_files_the_adapter_deletion_touches() -> None:
    owner = {r[0]: r[1] for r in MAP}
    # Amendment 9: they move with the adapter delete to A5r (was A5a)
    assert owner["tests/executor/test_p01.py"] == "A5r" and owner["tests/executor/test_review3.py"] == "A5r"
