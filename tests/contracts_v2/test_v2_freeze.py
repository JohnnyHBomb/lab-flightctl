"""Freeze (Sol 6 round 9, REVIEW-sol6-r9.md): the two install-audit counterexamples carried to C7h as acceptance
obligations. They are STRICT xfail reference tests: they fail today, and the suite errors (XPASS strict) as soon as
the C7h audit catches them, so the implementer must remove the mark and flip CONFORMANCE sol6r9-new / sol6r8-B5."""

from pathlib import Path

import pytest

from .validation import CLEAR_PATH, operator_sudo_audit

ROOT = Path(__file__).parents[2]
FRESH = f"Runas and Command-specific defaults for opr:\n    Defaults!{CLEAR_PATH} timestamp_timeout=0\n\n"


@pytest.mark.xfail(strict=True, reason="owed by C7h: effective exempt_group containing the operator skips the prompt (Sol 6 r9)")
def test_carried_exempt_group_defeats_fresh_auth() -> None:
    text = ("Matching Defaults entries for opr on host-a:\n    env_reset, !setenv, timestamp_timeout=0, exempt_group=wheel\n\n"
            + FRESH + f"User opr may run the following commands on host-a:\n    (root) {CLEAR_PATH}\n")
    assert operator_sudo_audit(text, "opr", groups=["wheel"])


@pytest.mark.xfail(strict=True, reason="owed by C7h: per-command NOPASSWD after a comma is read as command text (Sol 6 r9)")
def test_carried_per_command_tags_after_a_comma() -> None:
    effective = ("Matching Defaults entries for opr on host-a:\n    env_reset, !setenv\n\n" + FRESH
                 + f"User opr may run the following commands on host-a:\n    (root) /usr/bin/true, NOPASSWD: {CLEAR_PATH}\n")
    static = f"Defaults!{CLEAR_PATH} timestamp_timeout=0\nopr ALL=(root) /usr/bin/true, NOPASSWD: {CLEAR_PATH}\n"
    assert any("NOPASSWD" in p for p in operator_sudo_audit(effective, "opr"))
    assert any("NOPASSWD" in p for p in operator_sudo_audit(effective, "opr", sudoers_text=static))


def test_carried_obligations_are_recorded() -> None:
    slices = (ROOT / "docs/v2/SLICES.md").read_text(encoding="utf-8")
    assert "CARRIED OBLIGATIONS (freeze, Sol 6 round 9" in slices and "exempt_group" in slices
    frozen = (ROOT / "docs/v2/FROZEN.md").read_text(encoding="utf-8")
    assert "APPROVE WITH CHANGES" in frozen and "needs a new review" in frozen
    rows = [l.split("\t") for l in (ROOT / "docs/v2/CONFORMANCE.tsv").read_text(encoding="utf-8").splitlines() if l.startswith("sol6r")]
    carried = [r for r in rows if r[3] == "carried"]
    assert len(carried) == 2 and all("C7h" in r[4] and "strict xfail" in r[4] for r in carried)
    assert [r[3] for r in rows if r[0] == "sol6r8-B5"] == ["specified"]
