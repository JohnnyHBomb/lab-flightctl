"""Freeze (Sol 6 round 9, REVIEW-sol6-r9.md): the two install-audit counterexamples carried to C7h as acceptance
obligations. Amendment 2 (Sol 6.1 cold review P1-4): they assert against the PRODUCTION audit interface through the
install_audit fixture (the module C7h delivers, flightctl.install_audit), with the frozen reference oracle standing in
until it exists. They are STRICT xfail today; once C7h's production audit makes them pass the suite reports XPASS
(strict), and C7h removes the two marks under its migration-map rows (G1b) and flips CONFORMANCE sol6r9-new /
sol6r8-B5. The assertions themselves never change."""

import importlib
from pathlib import Path

import pytest

from .validation import CLEAR_PATH, operator_sudo_audit

PRODUCTION_AUDIT = "flightctl.install_audit"  # delivered by C7h (Amendment 2)


@pytest.fixture
def install_audit():
    """The production install audit when C7h has delivered it, else the frozen reference oracle."""
    try:
        module = importlib.import_module(PRODUCTION_AUDIT)
    except ModuleNotFoundError:
        return operator_sudo_audit
    return module.operator_sudo_audit

ROOT = Path(__file__).parents[2]
FRESH = f"Runas and Command-specific defaults for opr:\n    Defaults!{CLEAR_PATH} timestamp_timeout=0\n\n"


def exempt_group_case(audit) -> bool:
    text = ("Matching Defaults entries for opr on host-a:\n    env_reset, !setenv, timestamp_timeout=0, exempt_group=wheel\n\n"
            + FRESH + f"User opr may run the following commands on host-a:\n    (root) {CLEAR_PATH}\n")
    return bool(audit(text, "opr", groups=["wheel"]))


def per_command_tags_case(audit) -> bool:
    effective = ("Matching Defaults entries for opr on host-a:\n    env_reset, !setenv\n\n" + FRESH
                 + f"User opr may run the following commands on host-a:\n    (root) /usr/bin/true, NOPASSWD: {CLEAR_PATH}\n")
    static = f"Defaults!{CLEAR_PATH} timestamp_timeout=0\nopr ALL=(root) /usr/bin/true, NOPASSWD: {CLEAR_PATH}\n"
    return (any("NOPASSWD" in p for p in audit(effective, "opr"))
            and any("NOPASSWD" in p for p in audit(effective, "opr", sudoers_text=static)))


@pytest.mark.xfail(strict=True, reason="owed by C7h: effective exempt_group containing the operator skips the prompt (Sol 6 r9); C7h removes this mark (migration-map row)")
def test_carried_exempt_group_defeats_fresh_auth(install_audit) -> None:
    assert exempt_group_case(install_audit)


@pytest.mark.xfail(strict=True, reason="owed by C7h: per-command NOPASSWD after a comma is read as command text (Sol 6 r9); C7h removes this mark (migration-map row)")
def test_carried_per_command_tags_after_a_comma(install_audit) -> None:
    assert per_command_tags_case(install_audit)


def test_carried_obligations_are_recorded(install_audit) -> None:
    """Amendment 2 rev 2 (Sol 6.1 amd2 P1): the ledger accepts exactly two reviewed states.
    PENDING: both sol6r9-new rows 'carried' and sol6r8-B5 'specified'.
    COMPLETED (C7h): all three 'contract-fixed', allowed ONLY when the production audit exists and both carried
    assertions pass against it. Any mixed state fails."""
    slices = (ROOT / "docs/v2/SLICES.md").read_text(encoding="utf-8")
    assert "CARRIED OBLIGATIONS (freeze, Sol 6 round 9" in slices and "exempt_group" in slices
    frozen = (ROOT / "docs/v2/FROZEN.md").read_text(encoding="utf-8")
    assert "APPROVE WITH CHANGES" in frozen and "needs a new review" in frozen
    rows = [l.split("\t") for l in (ROOT / "docs/v2/CONFORMANCE.tsv").read_text(encoding="utf-8").splitlines() if l.startswith("sol6r")]
    carried = [r for r in rows if r[0] == "sol6r9-new"]
    b5 = [r[3] for r in rows if r[0] == "sol6r8-B5"]
    assert len(carried) == 2 and all("C7h" in r[4] and "strict xfail" in r[4] for r in carried) and len(b5) == 1
    statuses = ([r[3] for r in carried], b5[0])
    pending = statuses == (["carried", "carried"], "specified")
    completed = statuses == (["contract-fixed", "contract-fixed"], "contract-fixed")
    assert pending or completed, f"ledger is in neither reviewed state: {statuses}"
    if completed:
        assert install_audit is not operator_sudo_audit, "completed requires the production audit (flightctl.install_audit)"
        assert exempt_group_case(install_audit) and per_command_tags_case(install_audit)
