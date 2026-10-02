"""Mechanical checks of the slice plan (Sol 6 B1, B7, B8): no dependency cycle, every assembly's ports are
delivered by earlier packets, migration rows name real packets, the conformance registry agrees with the plan."""

import csv
import re
from pathlib import Path

from tests.conformance import registry

DOCS = Path(__file__).parents[2] / "docs" / "v2"
SLICES = (DOCS / "SLICES.md").read_text(encoding="utf-8")
MILESTONE_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3}  # D = session enablement, after R1 (Amendment 3)


def _index() -> list[dict]:
    section = SLICES.split("## Packet index", 1)[1].split("**Assembly prerequisites**", 1)[0]
    rows = []
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 8 or cells[0] in {"ID", "---"} or set(cells[0]) <= {"-"}:
            continue
        rows.append({"id": cells[0].strip("*"), "milestone": cells[2], "depends": cells[4], "ports": cells[6]})
    return rows


ROWS = _index()
IDS = [r["id"] for r in ROWS]
POS = {pid: i for i, pid in enumerate(IDS)}


def _expand(dep: str) -> list[str]:
    out: list[str] = []
    for token in (t.strip() for t in dep.split(",")):
        if token in {"", "-"}:
            continue
        if "-" in token and not token.endswith("ASM"):
            lo, hi = token.split("-", 1)
            out += IDS[POS[lo]: POS[hi] + 1]
        else:
            out.append(token)
    return out


def test_index_parsed_and_ids_unique() -> None:
    assert len(IDS) >= 40 and len(IDS) == len(set(IDS))
    assert {"A-ASM", "B-ASM", "C-ASM"} <= set(IDS)


def test_dependencies_exist_and_point_backwards() -> None:
    for row in ROWS:
        for dep in _expand(row["depends"]):
            assert dep in POS, f"{row['id']} depends on unknown {dep}"
            assert POS[dep] < POS[row["id"]], f"{row['id']} depends on later packet {dep} (cycle risk)"


def test_assembly_prerequisite_ports_are_delivered_before_the_assembly() -> None:
    table = SLICES.split("**Assembly prerequisites**", 1)[1].split("\n\n", 2)[1]
    delivered: dict[str, str] = {}
    for row in ROWS:
        for port in (p.strip() for p in row["ports"].split(",")):
            if port and port != "-":
                delivered[port] = row["id"]
    needs = {}
    for line in table.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 2 and cells[0].endswith("ASM"):
            needs[cells[0]] = [m.group(0) for m in re.finditer(r"\b[a-z]+(?:_[a-z]+)+\b|\b(clock|waker|inhibitor|signer|notifier)\b", cells[1])]
    assert set(needs) == {"A-ASM", "B-ASM", "C-ASM"}
    for asm, ports in needs.items():
        for port in ports:
            assert port in delivered, f"{asm} needs {port} but no packet delivers it"
            assert POS[delivered[port]] < POS[asm], f"{asm} needs {port}, delivered only by later packet {delivered[port]} (Sol B1 cycle)"


def _crosswalk_order() -> list[str]:
    section = SLICES.split("Crosswalk to ROADMAP.tsv.", 1)[1].split("\n## ", 1)[0]
    order: list[str] = []
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 2 and cells[0] not in {"Packet", "---"}:
            order += [p.strip() for p in cells[0].split(",")]
    return order


def test_crosswalk_follows_the_schedule_and_covers_every_packet() -> None:
    """Round 5 (Sol 6 r4 B1 regression): the ROADMAP crosswalk is a scope map listed in schedule (packet-index) order;
    it may not place any packet, in particular A-ASM, ahead of a packet it depends on (A11/A12 included)."""
    assert "scope map, not a schedule" in SLICES
    order = _crosswalk_order()
    assert sorted(order) == sorted(IDS), set(IDS) ^ set(order)
    assert [POS[p] for p in order] == sorted(POS[p] for p in order), "crosswalk is not in packet-index order"
    assert order.index("A11") < order.index("A-ASM") and order.index("A12") < order.index("A-ASM")


def test_milestone_column_is_monotone_and_dependencies_never_point_to_a_later_milestone() -> None:
    order = {"A": 0, "B": 1, "C": 2, "D": 3}  # D: session enablement after R1 (Amendment 3)
    last = 0
    for row in ROWS:
        m = order[row["milestone"]]
        assert m >= last, f"{row['id']} is milestone {row['milestone']} after a later milestone"
        last = m
        for dep in _expand(row["depends"]):
            dep_row = next(r for r in ROWS if r["id"] == dep)
            assert order[dep_row["milestone"]] <= m, f"{row['id']} ({row['milestone']}) depends on {dep} ({dep_row['milestone']})"




def test_usage_precedes_quotas() -> None:
    assert POS["C5a"] < POS["C5b"]
    assert "Usage" in SLICES.split("| C5a |", 1)[1].split("\n", 1)[0] and "Quotas" in SLICES.split("| C5b |", 1)[1].split("\n", 1)[0]


def test_registry_owed_by_matches_plan_ports() -> None:
    delivered = {}
    for row in ROWS:
        for port in (p.strip() for p in row["ports"].split(",")):
            if port and port != "-":
                delivered[port] = row["id"]
    assert registry.OWED_BY == delivered


def test_migration_map_rows_name_real_packets() -> None:
    lines = [l for l in (DOCS / "migration-map.tsv").read_text(encoding="utf-8").splitlines() if l and not l.startswith("#")]
    rows = list(csv.DictReader(lines, delimiter="\t"))
    assert rows and set(rows[0]) == {"v1_test", "packet", "action", "replacement", "reason"}
    for row in rows:
        # Amendment 4 rev 2: "contracts-v2" = the lead's contract/amendment stream, whose real diff is gated by
        # test_v2_round3.py::test_b7_migration_gate_on_this_branchs_real_diff with --packet contracts-v2
        assert row["packet"] in POS or row["packet"] == "contracts-v2", row
        assert row["action"] in {"rewrite", "delete"}, row
        if row["action"] == "rewrite":
            assert row["replacement"] != "-" and row["replacement"] in SLICES, f"replacement {row['replacement']} is not a named test in SLICES.md"
