"""Amendment 13 (site refresh, Daybreak must-fix gates before A7, state-machine definition of done): the new packets
A3c2, AD1-AD7 and AS1-AS8, A7's dependency on them, the D5 seats, the G0b/G7/G8 gate text, the 14-machine table and
the C13-1/C13-2 contract additions."""

import json
from pathlib import Path

from tests.conformance.registry import OWED_BY

from .test_v2_plan import POS, _crosswalk_order, _expand
from .validation import assert_invalid, assert_valid

ROOT = Path(__file__).resolve().parents[2]
SLICES = (ROOT / "docs/v2/SLICES.md").read_text(encoding="utf-8")
GATES = (ROOT / "docs/v2/GATES.txt").read_text(encoding="utf-8")
AMENDMENT = (ROOT / "docs/v2/AMENDMENT-13.md").read_text(encoding="utf-8") if (ROOT / "docs/v2/AMENDMENT-13.md").exists() else ""

AD = [f"AD{n}" for n in range(1, 8)]
AS = [f"AS{n}" for n in range(1, 9)]
NEW = ["A3c2", *AD, *AS]
DEPENDS = {"A3c2": "A3c", "AD1": "A5a3", "AD2": "AD1", "AD3": "A2, A4", "AD4": "A5b1", "AD5": "A5b1s, AD4",
           "AD6": "A5b1c, A5r, AD2", "AD7": "AD6", "AS1": "AD6", "AS2": "AD5", "AS3": "A6b", "AS4": "A5b1", "AS5": "A3c2",
           "AS6": "A7b, A10", "AS7": "A4u", "AS8": "AD1"}
SIZES = {"A3c2": "S", "AD2": "M", "AD5": "M", "AD6": "M"}
REPRICED = {"A5a4": "S", "A4ub": "S", "A5b2": "M", "A5b1s": "M", "A5b1c": "S", "A7": "M", "A7b": "M", "A8": "M", "A9": "M",
            "A10": "S", "A12": "M"}
MACHINES = {1: "AD6", 2: "A5r", 3: "AD2", 4: "A8", 5: "AS7", 6: "AD7", 7: "AS1", 8: "AD5", 9: "AS2", 10: "AS8", 11: "AS3",
            12: "AS4", 13: "AS5", 14: "AS6"}


def _row(packet: str) -> list[str]:
    line = next((l for l in SLICES.splitlines() if l.startswith(f"| {packet} |")), None)
    assert line is not None, f"no index row for {packet}"
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _section(header: str) -> str:
    assert f"\n### {header}" in SLICES, f"no brief {header}"
    return SLICES.split(f"\n### {header}", 1)[1].split("\n### ", 1)[0]


def _field(header: str, name: str) -> str:
    return next(l for l in _section(header).splitlines() if l.startswith(f"- {name}"))


def test_new_packet_rows_dependencies_and_index_placement() -> None:
    for packet in NEW:
        row = _row(packet)
        assert row[2] == "A" and row[6] == "-", packet  # milestone A, no new port
        assert row[4] == DEPENDS[packet], (packet, row[4])
        assert row[3] == SIZES.get(packet, "S"), packet
        assert row[5] == ("lead" if packet == "A3c2" else "O, Sn" if row[3] == "M" else "O or Sn, BR"), packet
        assert f"### {packet}: " in SLICES, packet
    assert POS["A3c"] + 1 == POS["A3c2"] < POS["A4"]
    assert [POS[p] for p in AD] == list(range(POS["A6b"] + 1, POS["A6b"] + 8)) and POS["AD7"] + 1 == POS["A7"]
    assert [POS[p] for p in AS] == list(range(POS["A10"] + 1, POS["A10"] + 9)) and POS["AS8"] + 1 == POS["A11"]
    assert set(NEW) <= set(_expand(_row("**A-ASM**")[4]))  # inside A-ASM's A0a-A12 range
    assert not set(NEW) & set(OWED_BY.values())
    order = _crosswalk_order()
    assert order.index("A3c") + 1 == order.index("A3c2") and order.index("A6b") + 1 == order.index("AD1")


def test_a7_waits_for_every_must_fix_packet() -> None:
    assert _row("A7")[4] == "A5b2, A3c2, AD1-AD7"
    assert {"A3c2", *AD} <= set(_expand(_row("A7")[4]))
    gate = {"G7-POW": ["AD1", "A5b1c"], "G7-SM": ["AD2", "AD6", "AD7"], "G7-CMD": ["AD3"], "G7-DB": ["AD4", "AD5"],
            "G7-BOOK": ["AD7"], "G7-DISC": ["A3c2"]}
    g7 = GATES.split("G7 MUST-FIX", 1)[1].split("\n\n", 1)[0]
    assert "A7 does not start until every row below is PROVEN" in g7
    for name, owners in gate.items():
        assert all(o in g7.split(name, 1)[1].split(")", 1)[0] for o in owners), name
        table_row = next(l for l in AMENDMENT.splitlines() if l.strip().startswith(f"| {name} |"))
        assert all(o in table_row for o in owners), name


def test_seats_follow_d5_in_cells_and_briefs() -> None:
    for packet, size in REPRICED.items():
        expected = "O, Sn" if size == "M" else "O or Sn, BR"
        assert _row(packet)[3] == size and _row(packet)[5] == expected, packet
        assert f"SEATS: {expected} (Amendment 13)." in _section(f"{packet}: "), packet
    assert "**Sn** = Sonnet, **BR** = a blind review" in SLICES
    assert "supersedes the" not in SLICES  # the brief SEATS lines are rewritten, not overridden
    assert "until a local model meets" not in SLICES + GATES


def test_regression_guard_and_state_machine_gates() -> None:
    g0b = GATES.split("G0b REGRESSION", 1)[1].split("\n  G1 ", 1)[0]
    for text in ("exempt from fail-on-base only when", "passes on the base commit", "reworks the", "protocol-2 stop"):
        assert text in " ".join(g0b.split()), text
    assert "regression guard for the v1-shaped protocol-2 stop" in _field("A5b2:", "PROOF")
    g8 = " ".join(GATES.split("G8 STATE", 1)[1].split("\n\n", 1)[0].split())
    for text in ("14 state machines", "one enum", "transition table", "one transition function", "test_<machine>_machine",
                 "retired machine meets G8 by deletion"):
        assert text in g8, text
    assert "EXIT (Amendment 13, G8)" in _section("A-ASM: ")


def test_the_fourteen_machines_have_owners_and_tests() -> None:
    table = {}
    for line in AMENDMENT.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 5 and cells[0].isdigit() and line.startswith("   |"):
            table[int(cells[0])] = cells[4]
    assert table == MACHINES
    for number, owner in MACHINES.items():
        if owner == "A5r":
            assert "machine 2 (the executor v1 record) is retired here" in _field("A5r:", "PROOF")
            continue
        brief = _section(f"{owner}: ")
        assert f"machine {number}" in brief and "_machine`" in brief, owner
    assert "`test_recovery_machine`" in _field("A8:", "ACCEPTANCE")


def test_contract_additions_c13_1_and_c13_2() -> None:
    booking = json.loads((ROOT / "contracts/booking-v1.schema.json").read_text(encoding="utf-8"))
    states = booking["$defs"]["booking"]["properties"]["state"]["enum"]
    assert states.index("blocked") + 1 == states.index("claiming") == states.index("claimed") - 1
    schema = json.loads((ROOT / "contracts/v2/policy.schema.json").read_text(encoding="utf-8"))
    limits = schema["properties"]["limits"]
    values = {"max_request_bytes": 1048576, "max_open_queue_entries_per_principal": 64,
              "max_mutations_per_principal_per_min": 60, "terminal_retention_s": 604800}
    assert "limits" in schema["required"] and sorted(limits["required"]) == sorted(values)
    assert {k: v["const"] for k, v in limits["properties"].items()} == values
    example = json.loads((ROOT / "config/policy-v2.json.example").read_text(encoding="utf-8"))
    assert example["limits"] == values
    assert_valid(example, "policy")
    for key in values:
        bad = json.loads(json.dumps(example))
        bad["limits"][key] += 1
        assert_invalid(bad, "policy")
    del example["limits"]
    assert_invalid(example, "policy")
    assert "WIRE CHANGE" in _section("AD7: ") and "flightctl/client.py" in _field("AD7:", "SCOPE")
