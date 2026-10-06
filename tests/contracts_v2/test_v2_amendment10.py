"""Amendment 10: an allow-list argv0 entry may not begin or end with ASCII whitespace (U+0020, U+0009-U+000D). The two
schemas, the occupancy probe, the site-config lane binding and the oracle agree on one table; whitespace inside an entry
and non-ASCII whitespace stay legal (the owner's decision, 6 Oct 2026)."""

import copy
import hashlib
import json
import re
from pathlib import Path

import pytest

from flightctl.gpu import NvidiaOccupancyProbe
from tests.fakes.clock import FakeClock

from .validation import argv0_matches, errors, examples, schema_path

ASCII_WS = " \t\n\v\f\r"
REFUSED = ["browser ", " browser", "browser\t", "\tbrowser", "x\n", "\nx", "x\r", "\rx", "\vx", "x\v", "\fx", "x\f",
           " x ", " ", "\t", "\n"]
ACCEPTED = ["browser", "a", "a b", "/usr/local/My App/bin", "a\nb", "a\tb", "x" * 4096,
            "\u00a0x", "x\u00a0", "\u2003x", "x\u3000", "\u0085x", "x\u001f"]  # non-ASCII and U+001C-U+001F: not refused
SCHEMAS = ("gpu-probe", "inventory")
ROOT = Path(__file__).resolve().parents[2]
UUID = "GPU-00000000-0000-0000-0000-000000000002"


def _with_entry(name: str, argv0: str) -> dict:
    """The schema's own valid example with every noise_allowlist entry's argv0 replaced."""
    instance = copy.deepcopy(next(e for e in examples(name)["valid"] if "noise_allowlist" in json.dumps(e)))

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "noise_allowlist":
                    for entry in value:
                        entry["argv0"] = argv0
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(instance)
    return instance


def _schema_ok(name: str, argv0: str) -> bool:
    return not errors(_with_entry(name, argv0), name)


def _probe_ok(argv0: str) -> bool:
    class NoRun:
        def run(self, argv, **kwargs):  # never reached for a refused entry; an accepted one fails the first query
            return {"argv": argv, "host_id": None, "returncode": 1, "stdout": "", "stderr": "", "timed_out": False,
                    "duration_s": 0.0, "error": None}
    try:
        NvidiaOccupancyProbe(NoRun(), clock=FakeClock()).occupancy(
            "host-1", "lane-t", [UUID], noise_allowlist=[{"argv0": argv0, "uid": 1000}], noise_cap_mib=0, lane_noise_mib=1024,
            timeout_s=10)
    except ValueError as exc:
        assert "argv0" in str(exc)
        return False
    return True


@pytest.mark.parametrize("name", SCHEMAS)
def test_schema_refuses_edge_whitespace_and_keeps_inner_and_non_ascii(name) -> None:
    for argv0 in REFUSED:
        assert not _schema_ok(name, argv0), repr(argv0)
    for argv0 in ACCEPTED:
        assert _schema_ok(name, argv0), repr(argv0)


def _pattern(name: str) -> str:
    found = re.findall(r'"pattern": "(\^\[\^ \\\\t-\\\\r\][^"]*)"', schema_path(name).read_text(encoding="utf-8"))
    assert len(found) == 1, name  # one allow-list argv0 item per schema
    return json.loads(f'"{found[0]}"')


def test_schema_pattern_end_anchor_is_not_dollar() -> None:
    """Python's $ also matches before a final newline: with $ the pattern would accept "x\\n"."""
    for name in SCHEMAS:
        pattern = _pattern(name)
        assert re.search(pattern, "x") and not re.search(pattern, "x\n")
        assert re.search(pattern.replace("(?![\\s\\S])", "$"), "x\n")  # the mutant: what the anchor is there for


def test_probe_refuses_edge_whitespace_and_keeps_inner_and_non_ascii() -> None:
    for argv0 in REFUSED:
        assert not _probe_ok(argv0), repr(argv0)
    for argv0 in ACCEPTED:
        assert _probe_ok(argv0), repr(argv0)


def test_oracle_never_matches_an_edge_whitespace_entry() -> None:
    assert argv0_matches("browser ", "browser ") is False  # True before Amendment 10
    assert argv0_matches(" x", " x") is False and argv0_matches("x\n", "x\n") is False
    assert argv0_matches("browser x", "browser") is True and argv0_matches("/usr/local/My App/bin", "/usr/local/My App/bin") is True
    for argv0 in ACCEPTED:
        assert argv0_matches(argv0, argv0), repr(argv0)


def test_schemas_probe_and_oracle_agree_on_every_edge_character() -> None:
    """One table: an entry 'a'+c or c+'a' for every character up to U+3100 is accepted by both schemas, the probe and the
    oracle alike, and refused exactly when c is ASCII whitespace."""
    patterns = [_pattern(name) for name in SCHEMAS]
    assert patterns[0] == patterns[1]
    for code in range(0x3100):
        c = chr(code)
        for argv0 in ("a" + c, c + "a"):
            want = c not in ASCII_WS
            assert bool(re.search(patterns[0], argv0)) is want, hex(code)
            assert _probe_ok(argv0) is want, hex(code)
            assert argv0_matches(argv0, argv0) is want, hex(code)
    from flightctl.gpu import ARGV0_EDGE_WHITESPACE  # the probe's and the loader's one constant
    assert ARGV0_EDGE_WHITESPACE == ASCII_WS


@pytest.mark.parametrize("argv0", ["browser ", " browser", "browser\t", "x\n", "\rx", ""])
def test_lane_binding_refuses_an_edge_whitespace_entry_naming_the_lane(argv0) -> None:
    from flightctl.siteconfig import SiteConfigRefused, lane_occupancy
    inventory = json.loads((ROOT / "config/inventory-v2.json.example").read_text(encoding="utf-8"))
    inventory["lanes"][0]["rules"]["external_tenant"]["noise_allowlist"] = [{"argv0": "browser", "uid": 1000},
                                                                            {"argv0": argv0, "uid": 1000}]
    raw = json.dumps(inventory)
    site = {"source": "deploy", "files": {"inventory.json": raw.encode()},
            "sha256": {"inventory.json": hashlib.sha256(raw.encode()).hexdigest()}}

    class Probe:
        calls = 0

        def occupancy(self, *args, **kwargs):
            Probe.calls += 1

    with pytest.raises(SiteConfigRefused, match="lane-gpu1"):
        lane_occupancy(site, Probe(), "lane-gpu1", timeout_s=10)
    assert Probe.calls == 0  # refused at load time, before the probe is asked


def test_amendment_10_is_recorded() -> None:
    text = (ROOT / "docs/v2/AMENDMENT-10.md").read_text(encoding="utf-8")
    assert text.startswith("# Amendment 10: ")
    rows = {l.split("\t")[0]: l.split("\t") for l in (ROOT / "docs/v2/CONFORMANCE.tsv").read_text(encoding="utf-8").splitlines()}
    assert rows["amd10-1"][3] == "contract-fixed" and "test_v2_amendment10.py" in rows["amd10-1"][4]
    assert "amd10-1" in "\t".join(rows["amd5-3"])
