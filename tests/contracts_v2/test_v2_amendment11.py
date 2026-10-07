"""Amendment 11 (brief prep for A5a3, A4ub, A5b1 and A5b2): A5a3 splits into the wire and A5a4 (executor_transport
conformance); the A4 transport rows go to A5r; A5b1 splits in three (A5b1, A5b1s, A5b1c) with two renamed tests;
A5b2 comes before A5b1c; A5b2's renew rows go to A7b; the max-end margin is a policy constant; the authority units
move from A4ub to A7."""

import ast
import json
from pathlib import Path

from tests.conformance.registry import OWED_BY
from tools import migration_gate

from .test_v2_plan import POS
from .validation import assert_invalid, assert_valid

ROOT = Path(__file__).resolve().parents[2]
SLICES = (ROOT / "docs/v2/SLICES.md").read_text(encoding="utf-8")
MAP = migration_gate.load_rows(ROOT / "docs/v2/migration-map.tsv")
A4_ROWS = ("test_one_shot_invocations_keep_state", "test_garbage_is_unparsable_not_ok",
           "test_transport_failures_are_typed_and_bounded", "test_wrong_key_denied")
RENEW = "test_renew_rolls_within_ceiling_and_refuses_past_it"
RENEW_NODE = "tests/authority/test_a5b2_beat_renew.py::" + RENEW  # the A5b2 brief's file; A7b adds none of its tests


def _row(packet: str) -> list[str]:
    line = next(l for l in SLICES.splitlines() if l.startswith(f"| {packet} |"))
    return [c.strip() for c in line.strip("|").split("|")]


def _section(header: str) -> str:
    return SLICES.split(f"\n### {header}", 1)[1].split("\n### ", 1)[0]


def _field(header: str, name: str) -> str:
    return next(l for l in _section(header).splitlines() if l.startswith(f"- {name}"))


def _rows(packet: str) -> dict[str, tuple[str, str]]:
    return {r["v1_test"]: (r["action"], r["replacement"]) for r in MAP if r["packet"] == packet}


def _test_names(path: str) -> set[str]:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    return {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")}


def test_a5a3_is_split_and_a5a4_owns_conformance() -> None:
    assert OWED_BY["executor_transport"] == "A5a4"
    assert (_row("A5a3")[4], _row("A5a3")[6]) == ("A5a2, A4u", "-")
    assert (_row("A5a4")[4], _row("A5a4")[6], _row("A5a4")[7]) == ("A5a3", "executor_transport", "G04 (conformance)")
    assert POS["A5a3"] + 1 == POS["A5a4"] < POS["A4ub"] < POS["A5b1"] < POS["A-ASM"]
    a5a3, a5a4 = _field("A5a3:", "ACCEPTANCE"), _field("A5a4:", "ACCEPTANCE")
    assert a5a4 == "- ACCEPTANCE: executor_transport conformance [strict]."
    assert "executor_transport" not in a5a3
    for name in ("test_stdio_serves_v2_through_executor_v2", "test_enforcer_one_shot_entry_point"):
        assert f"`{name}` [realtime]" in a5a3, name
    assert "impl_executor_transport.py" in _field("A5a4:", "SCOPE") and "impl_executor_transport" not in _field("A5a3:", "SCOPE")
    assert "G3" in _field("A5a4:", "PROOF") and "G3" not in _field("A5a3:", "PROOF")
    assert _row("A5b1")[4] == "A5a3" and _row("A4ub")[4] == "A4u, A5a3"  # they need the wire, not the rig


def test_a4_transport_rows_go_to_a5r() -> None:
    rows = {k: v for k, v in _rows("A5r").items() if k.startswith("tests/transport/")}
    assert rows == {f"tests/transport/test_a4_transport.py::{n}": ("rewrite", n) for n in A4_ROWS}
    assert _rows("A5a3") == {} and _rows("A5a4") == {}
    assert "tests/transport/test_a4_transport.py" in _field("A5r:", "SCOPE")
    for name in A4_ROWS:  # same names: A5r modifies the file in place, so the bare name counts (G1b)
        assert name in _test_names("tests/transport/test_a4_transport.py"), name


def test_a5b1_is_split_in_three_with_renamed_tests() -> None:
    assert [_row(p)[4] for p in ("A5b1", "A5b1s", "A5b1c")] == ["A5a3", "A5b1", "A5b2, A5b1s, A5a2"]
    assert all(_row(p)[2] == "A" and _row(p)[6] == "-" for p in ("A5b1", "A5b1s", "A5b1c"))
    expected = {
        "A5b1:": ["test_release_identity_matches_reserve", "test_definite_refusal_cancels_lease_lane_stays_free",
                  "test_executor_cause_reaches_rpc_error", "test_reserve_stop_real_executor_process"],
        "A5b1s:": ["test_token_scrub_storage_level", "test_split_store_crash_and_restore",
                   "test_store_replay_checks_request_fingerprint"],
        "A5b1c:": ["test_grant_withheld_until_ceiling_acknowledged", "test_grant_needs_persisted_fence"],
    }
    for header, names in expected.items():
        acceptance = _field(header, "ACCEPTANCE")
        for name in names:
            assert f"`{name}`" in acceptance, (header, name)
            others = [h for h in expected if h != header]
            assert all(f"`{name}`" not in _field(h, "ACCEPTANCE") for h in others), name
        assert len(names) <= 5
    # the renamed tests no longer collide with the frozen oracle tests of the same round
    oracle = _test_names("tests/contracts_v2/test_v2_round8.py")
    assert {"test_ceiling_needs_persisted_fence", "test_replay_checks_request_fingerprint"} <= oracle
    for names in expected.values():
        assert not set(names) & oracle
    assert "store.py" not in _field("A5b1:", "SCOPE") and "flightctl/store.py" in _field("A5b1s:", "SCOPE")
    assert _rows("A5b1") == {"tests/sim/test_repro_release_identity_and_reason.py::test_repro_release_identity_and_reason":
                             ("rewrite", "test_repro_release_identity_and_reason")}


def test_a5b2_comes_before_a5b1c() -> None:
    assert _row("A5b2")[4] == "A5b1"
    assert POS["A5b1"] < POS["A5b2"] < POS["A5b1s"] < POS["A5b1c"] < POS["A5r"] < POS["A7"]
    assert "A5b2's margin" in _field("A5b1c:", "GOAL")


def test_renew_rows_go_to_a7b() -> None:
    assert _rows("A5b2") == {"tests/sim/test_repro_beat_and_renew.py::test_repro_beat_and_renew": ("rewrite", "test_repro_beat_and_renew")}
    a7b = _rows("A7b")
    assert {k.split(" (", 1)[0] for k in a7b} == {"tests/authority/test_revision2.py", "tests/client/test_vectors.py",
                                                  "tests/contracts/vectors/cli.json", "tests/authority/test_review3.py"}
    assert set(a7b.values()) == {("rewrite", RENEW_NODE)} and f"`{RENEW}`" in _field("A5b2:", "ACCEPTANCE")
    assert "protocol-2" in _field("A5b2:", "GOAL") and "A7b" in _field("A5b2:", "SCOPE")
    assert f"`{RENEW_NODE}`" in _field("A7b:", "SCOPE")


def test_max_end_margin_is_a_contract_constant() -> None:
    schema = json.loads((ROOT / "contracts/v2/policy.schema.json").read_text(encoding="utf-8"))
    timing = schema["properties"]["timing"]
    assert timing["properties"]["max_end_margin_s"]["const"] == 30 and "max_end_margin_s" in timing["required"]
    example = json.loads((ROOT / "config/policy-v2.json.example").read_text(encoding="utf-8"))
    assert example["timing"]["max_end_margin_s"] == 30
    assert_valid(example, "policy")
    for value in (0, 29, 31, 30.5, "30"):
        bad = json.loads(json.dumps(example))
        bad["timing"]["max_end_margin_s"] = value
        assert_invalid(bad, "policy")
    missing = json.loads(json.dumps(example))
    del missing["timing"]["max_end_margin_s"]
    assert_invalid(missing, "policy")
    assert "policy.timing.max_end_margin_s" in _field("A5b2:", "GOAL")


def test_authority_units_move_to_a7() -> None:
    assert "units/authority" not in _field("A4ub:", "SCOPE") and "units/host/*" in _field("A4ub:", "SCOPE")
    assert "authority service" not in _row("A4ub")[1] and _row("A4ub")[7] == "G04 timer"
    assert "units/authority/flightctl-authority.service.template" in _field("A7:", "SCOPE")
    assert "test_templates_render_without_site_strings" in _field("A7:", "SCOPE")
    # A7 edits A4ub's exact-list test under its own row (G1 FROZEN / G1b), same name in a file A7 modifies
    assert _rows("A7") == {"tests/siteconfig/test_a4ub_units.py::test_templates_render_without_site_strings":
                           ("rewrite", "test_templates_render_without_site_strings")}
    assert "`test_templates_render_without_site_strings`" in _field("A4ub:", "ACCEPTANCE")

