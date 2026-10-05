"""Amendment 9 (wave-2 staging): A4u, A3b and A5a split; the v1-executor rows go to A5r; A6b keeps its v1 rows; the
branch-wide migration test accepts a packet's own rows."""

from pathlib import Path

import pytest

from tests.conformance.registry import OWED_BY
from tools import migration_gate

from .test_v2_plan import POS
from .test_v2_round3 import branch_migration_problems

ROOT = Path(__file__).resolve().parents[2]
SLICES = (ROOT / "docs/v2/SLICES.md").read_text(encoding="utf-8")
MAP = migration_gate.load_rows(ROOT / "docs/v2/migration-map.tsv")
A3B = "tests/discovery/test_a3b_discovery.py::"
A5A = "tests/executor/test_a5a_executor_v2.py::"


def _row(packet: str) -> list[str]:
    line = next(l for l in SLICES.splitlines() if l.startswith(f"| {packet} |"))
    return [c.strip() for c in line.strip("|").split("|")]


def _section(header: str) -> str:
    return SLICES.split(f"\n### {header}", 1)[1].split("\n### ", 1)[0]


def _field(header: str, name: str) -> str:
    return next(l for l in _section(header).splitlines() if l.startswith(f"- {name}"))


def _rows(packet: str) -> dict[str, tuple[str, str]]:
    return {r["v1_test"]: (r["action"], r["replacement"]) for r in MAP if r["packet"] == packet}


def test_a4u_is_split_and_a4ub_follows_a5a3() -> None:
    assert _row("A4u")[4] == "A4, A3" and _row("A4u")[7] == "G24"
    assert _row("A4ub")[4] == "A4u, A5a3" and _row("A4ub")[7] == "G04 timer"
    assert POS["A4u"] < POS["A5a3"] < POS["A4ub"] < POS["A-ASM"]
    a4u, a4ub = _field("A4u:", "ACCEPTANCE"), _field("A4ub:", "ACCEPTANCE")
    for name in ("test_config_loader_rejects_hash_mismatch", "test_local_copy_used_when_store_host_asleep",
                 "test_expected_uuids_come_from_confirmed_inventory"):
        assert f"`{name}`" in a4u and name not in a4ub, name
    for name in ("test_templates_render_without_site_strings", "test_timer_unit_runs_one_shot"):
        assert f"`{name}`" in a4ub and name not in a4u, name
    assert "units/host/*" in _field("A4ub:", "SCOPE") and "units/" not in _field("A4u:", "SCOPE")
    assert "G3" not in _field("A4u:", "PROOF") and "G3 on the pilot host" in _field("A4ub:", "PROOF")
    assert "A5a3" in _field("A4ub:", "GOAL") and "A4ub's timer" in _field("A5a3:", "GOAL")
    assert _row("B5")[4] == "A4u"  # B5 needs the deploy-dir layout, not the unit templates


def test_a3b_is_split_and_a3c_retires_v1_discovery() -> None:
    assert _row("A3b")[7] == "G07" and _row("A3c")[4] == "A3b" and _row("A3c")[5] == "lead"
    assert "Grok 4" in _row("A3c")[7] and "D-amd-1" in _row("A3c")[7]
    assert POS["A3b"] < POS["A3c"] < POS["A4"]
    assert "No migration row" in _field("A3b:", "SCOPE") and _rows("A3b") == {}
    assert "v1 discovery stays until A3c" in _field("A3b:", "GOAL")
    assert "test_b7_migration_gate_on_this_branchs_real_diff" in _field("A3c:", "PRECONDITION")
    disc = "tests/discovery/test_discovery.py::"
    assert _rows("A3c") == {
        "tests/fakes/fixtures/amd.json": ("delete", "-"),
        disc + "test_raw_discovery_parsing_and_multidevice_lane": ("rewrite", A3B + "test_discover_against_replayed_real_captures"),
        disc + "test_raw_transport_inputs_use_production_parsers_and_projection": ("rewrite", A3B + "test_v2_projection_takes_embedded_inventory"),
        **{disc + n: ("delete", "-") for n in (
            "test_new_device_preserves_existing_identity_lane_and_exact_diff",
            "test_driver_only_change_preserves_custom_ids_lanes_and_exact_diff",
            "test_discovery_mutations_regression_matrix",
            "test_device_ids_are_order_independent_and_peer_addition_stable",
            "test_long_host_collision_allocation_is_bounded")},
    }
    for _, replacement in _rows("A3c").values():  # exact nodes, because A3c adds none of A3b's tests
        assert replacement == "-" or replacement.split("::")[1] in _field("A3b:", "ACCEPTANCE")


def test_a5a_is_split_in_three() -> None:
    assert OWED_BY["executor_transport"] == "A5a3"
    assert [_row(p)[6] for p in ("A5a", "A5a2", "A5a3")] == ["-", "-", "executor_transport"]
    assert (_row("A5a2")[4], _row("A5a3")[4], _row("A5b1")[4], _row("A11")[4]) == ("A5a", "A5a2", "A5a3", "A5a2, A6")
    assert POS["A5a"] < POS["A5a2"] < POS["A5a3"] < POS["A5b1"]
    a5a, a5a2, a5a3 = _field("A5a:", "ACCEPTANCE"), _field("A5a2:", "ACCEPTANCE"), _field("A5a3:", "ACCEPTANCE")
    for name in ("test_relative_deadline_anchored_on_host_clock", "test_beat_extends_expiry_never_max_end",
                 "test_stop_requires_reserve_identity_and_empty_proof", "test_enforcer_real_seconds", "test_repro_cross_host_reserve"):
        assert f"`{name}`" in a5a, name
    assert "`test_definite_refusal_leaves_no_fence_and_no_inhibitor`" in a5a2 and "test_definite_refusal" not in a5a
    assert "executor_transport conformance [strict]" in a5a3 and "executor_transport" not in a5a
    assert "`test_enforcer_one_shot_entry_point` [realtime]" in a5a3
    assert _rows("A5a") == {"tests/sim/test_repro_cross_host_reserve.py::test_repro_cross_host_reserve": ("rewrite", "test_repro_cross_host_reserve")}
    assert {k for k in _rows("A5a3")} == {f"tests/transport/test_a4_transport.py::{n}" for n in (
        "test_one_shot_invocations_keep_state", "test_garbage_is_unparsable_not_ok",
        "test_transport_failures_are_typed_and_bounded", "test_wrong_key_denied")}


def test_v1_executor_rows_go_to_a5r() -> None:
    assert _row("A5r")[4] == "A5b2" and _row("A5r")[5] == "lead" and POS["A5b2"] < POS["A5r"] < POS["A-ASM"]
    ex = "tests/executor/test_executor.py::"
    stop, deadline = A5A + "test_stop_requires_reserve_identity_and_empty_proof", A5A + "test_relative_deadline_anchored_on_host_clock"
    assert _rows("A5r") == {
        ex + "test_reserve_start_fence": ("rewrite", stop),
        ex + "test_clock_reboot_and_same_boot_deadline": ("rewrite", deadline),
        ex + "test_reboot_reconcile_rejects_partial_and_reanchors_deadline": ("rewrite", deadline),
        ex + "test_repeated_reboots_preserve_absolute_deadline": ("rewrite", deadline),
        ex + "test_deadline_enforcement_calls_before_and_at_boundaries": ("rewrite", A5A + "test_enforcer_real_seconds"),
        "tests/executor/systemd_adapter.py": ("delete", "-"),
        "tests/executor/test_p01.py": ("rewrite", stop),
        "tests/executor/test_review3.py": ("rewrite", stop),
        "tests/executor/test_review4.py": ("delete", "-"),
        "tests/executor/test_mutations.py": ("delete", "-"),
    }
    for _, replacement in _rows("A5r").values():  # exact nodes of A5a's named tests, because A5r adds none of them
        assert replacement == "-" or replacement.split("::")[1] in _field("A5a:", "ACCEPTANCE")
    assert "the owner may fold it into the end of A5b2" in _section("A5r:").split("\n", 1)[0]


def test_a6b_keeps_its_v1_rows() -> None:
    owner = {r["v1_test"]: r["packet"] for r in MAP}
    assert owner["tests/executor/test_executor.py::test_fake_isolation_guard"] == "A6b"
    assert owner["tests/contracts/test_fakes.py::test_fake_interfaces"] == "A6b"
    scope = _field("A6b:", "SCOPE")
    assert "A6b's lead merge, which executes them once Amendment 9 is on main" in scope
    assert "background" not in _section("A6b:")


@pytest.mark.parametrize("label,touched,collected,files,ok", [
    ("nothing touched", {}, set(), set(), True),
    ("A6b's delete row executed", {"tests/executor/test_executor.py": "M"}, set(), set(), True),
    ("A6b's rewrite with its replacement in a branch test file", {"tests/contracts/test_fakes.py": "M"},
     {"tests/runner/test_a6b_runner.py::test_fake_runner_per_unit_fail_closed"}, {"tests/runner/test_a6b_runner.py"}, True),
    ("A6b's rewrite without its replacement", {"tests/contracts/test_fakes.py": "M"}, set(), set(), False),
    ("a bare replacement outside the branch's test files", {"tests/contracts/test_fakes.py": "M"},
     {"tests/runner/test_a6b_runner.py::test_fake_runner_per_unit_fail_closed"}, set(), False),
    ("A3c's whole-file delete executed", {"tests/fakes/fixtures/amd.json": "D"}, set(), set(), True),
    ("A3c's whole-file delete row, file only modified", {"tests/fakes/fixtures/amd.json": "M"}, set(), set(), False),
    ("A3c's rewrites with A3b's exact nodes", {"tests/discovery/test_discovery.py": "M"},
     {A3B + "test_discover_against_replayed_real_captures", A3B + "test_v2_projection_takes_embedded_inventory"}, set(), True),
    ("A3c's rewrites without them", {"tests/discovery/test_discovery.py": "M"}, set(), set(), False),
    ("A5r's rows executed", {"tests/executor/test_executor.py": "M", "tests/executor/systemd_adapter.py": "D",
                             "tests/executor/test_p01.py": "M", "tests/executor/test_review3.py": "M",
                             "tests/executor/test_review4.py": "D", "tests/executor/test_mutations.py": "D"},
     {A5A + n for n in ("test_stop_requires_reserve_identity_and_empty_proof", "test_relative_deadline_anchored_on_host_clock",
                        "test_enforcer_real_seconds")}, set(), True),
    ("A5r's whole-file delete row, file only modified", {"tests/executor/test_mutations.py": "M"}, set(), set(), False),
    ("the base's contracts-v2 rows", {"tests/integration/test_p6_scaffold.py": "M", "tests/roster/shims.py": "M"},
     {"tests/integration/test_p6_scaffold.py::test_ci_security_configuration",
      "tests/roster/test_roster_scripts.py::test_arm_fresh_lifecycle_uses_stateful_owner_and_injected_hooks"}, set(), True),
    ("an unmapped pre-existing test edited", {"tests/authority/test_authority.py": "M"}, set(), set(), False),
    ("one mapped and one unmapped", {"tests/fakes/fixtures/amd.json": "D", "tests/authority/test_authority.py": "M"}, set(), set(), False),
])
def test_branch_rule_accepts_a_packets_own_rows_and_refuses_the_rest(label, touched, collected, files, ok) -> None:
    assert (branch_migration_problems(touched, collected, MAP, files) == []) is ok, label
