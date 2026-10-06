"""Amendment 5 (races R-A2 and R-A3 follow-ups): the A3 split, CommandResult.error, one-string command lines in the
noise allow-list, the per-card PIDS query, an oracle that never returns an ok observation the schema refuses, and the
G3 rule that strict real-twin evidence needs a status-ok observation."""

import json
import random
from pathlib import Path

import pytest

from .test_v2_plan import POS, ROWS
from .validation import argv0_matches, errors, noise_identity_ok, occupancy_from_capture

ROOT = Path(__file__).parents[2]
SLICES = (ROOT / "docs/v2/SLICES.md").read_text(encoding="utf-8")
BY_ID = {r["id"]: r for r in ROWS}


def brief(pid: str) -> str:
    return SLICES.split(f"\n### {pid}:", 1)[1].split("\n### ", 1)[0]


# ---------------------------------------------------------------- 1. the A3 split
def test_a3_is_split_into_occupancy_part_1_and_inventory_part_2() -> None:
    assert BY_ID["A3"]["ports"].strip() == "occupancy_probe" and BY_ID["A3i"]["ports"].strip() == "inventory_probe"
    assert POS["A3"] < POS["A3i"] < POS["A3b"] < POS["A-ASM"]
    assert "A3" in BY_ID["A3i"]["depends"] and "A3i" in BY_ID["A3b"]["depends"] and "A3" in BY_ID["A4u"]["depends"]
    acc3 = brief("A3").split("- ACCEPTANCE", 1)[1].split("\n- PROOF", 1)[0]
    for name in ("test_production_parser_agrees_with_oracle_on_all_captures", "test_small_unlisted_cuda_process_is_a_tenant",
                 "test_noise_needs_allowlisted_identity_and_cap", "test_probe_real_process_timeout"):
        assert f"`{name}`" in acc3, name
    assert "Moved by Amendment 5: `test_golden_captures_all_card_models` to A3i; `test_expected_uuids_come_from_confirmed_inventory` to A4u" in acc3
    assert "`test_golden_captures_all_card_models`" in brief("A3i").split("- ACCEPTANCE", 1)[1]
    assert "`test_expected_uuids_come_from_confirmed_inventory`" in brief("A4u").split("- ACCEPTANCE", 1)[1]
    assert "flightctl/siteconfig.py" in brief("A4u")
    from tests.conformance import registry
    assert registry.OWED_BY["occupancy_probe"] == "A3" and registry.OWED_BY["inventory_probe"] == "A3i"
    rows = [l.split("\t") for l in (ROOT / "docs/v2/migration-map.tsv").read_text(encoding="utf-8").splitlines() if l.startswith("tests/fakes/fixtures/")]
    # Amendment 9: the amd.json delete row moved from A3b to A3c (A3b part 1 touches no migration row)
    assert {r[0]: r[1] for r in rows if r[1] not in {"A3b", "A3c"}} == {f"tests/fakes/fixtures/{n}.json": "A3i" for n in ("titan-rtx", "rtx-8000", "t4")}


# ---------------------------------------------------------------- 2. CommandResult.error
def test_command_result_has_the_typed_error_key_a2_returns(tmp_path: Path) -> None:
    from contracts.v2.interfaces import CommandResult
    from flightctl.commands import LocalCommandRunner
    from .validation import assert_valid
    keys = set(CommandResult.__annotations__)
    assert keys == {"argv", "host_id", "returncode", "stdout", "stderr", "timed_out", "duration_s", "error"}
    runner = LocalCommandRunner()
    ok = runner.run(["true"], timeout_s=5)
    assert set(ok) == keys and ok["error"] is None
    failed = runner.run([str(tmp_path / "no-such-program")], timeout_s=5)
    assert set(failed) == keys and failed["error"] is not None
    assert_valid(failed["error"], "common", "typed_error")
    slow = runner.run(["sleep", "5"], timeout_s=0.2)
    assert slow["timed_out"] is True and slow["error"]["code"] == "timeout"


# ---------------------------------------------------------------- 3. one-string command lines
BROWSER_817 = "/usr/lib/browser/browser --type=gpu-process " + "--flag=x " * 85  # one string, like the measured 817 bytes
SPACED_912 = "/usr/local/Some App/some-app --type=gpu-process --ozone-platform=wayland"  # measured shape: a space in the path


@pytest.mark.parametrize("argv0,entry,expect", [
    ("/usr/lib/browser/browser", "/usr/lib/browser/browser", True),           # normal argv: equality
    (BROWSER_817, "/usr/lib/browser/browser", True),                         # rewritten one string: entry + space
    (SPACED_912, "/usr/local/Some App/some-app", True),                            # executable path with a space
    (SPACED_912, "/usr/local/Some App/some", False),                              # a prefix must end at a space boundary
    ("/usr/lib/browser/browser-evil --x", "/usr/lib/browser/browser", False),  # a longer name is not a match
    ("/usr/lib/browser/browserx", "/usr/lib/browser/browser", False),
    ("browser", "/usr/lib/browser/browser", False),
    ("/usr/lib/browser/browser --x", "", False),                              # an empty entry matches nothing
    (" leading-space argv0", "", False),
    ("/usr/lib/browser/browser --x", None, False),
])
def test_allowlist_matches_rewritten_one_string_command_lines(argv0, entry, expect) -> None:
    assert argv0_matches(argv0, entry) is expect


def test_one_string_noise_keeps_root_dynamicuser_and_cap_rules() -> None:
    allow = [{"argv0": "/usr/lib/browser/browser", "uid": 1000}]
    good = {"uid": 1000, "argv0": BROWSER_817, "context_type": "C+G"}
    assert noise_identity_ok(good, allow)
    assert not noise_identity_ok(dict(good, context_type="C"), allow)
    assert not noise_identity_ok(dict(good, uid=0), [{"argv0": "/usr/lib/browser/browser", "uid": 0}])
    assert not noise_identity_ok(dict(good, uid=61300), [{"argv0": "/usr/lib/browser/browser", "uid": 61300}])
    assert not noise_identity_ok(dict(good, argv0=None), allow)
    u = "GPU-11111111-2222-3333-4444-555555555555"
    kw = dict(returncode=0, lane_id="lane-t", host_id="host-1", observed_at="2026-10-04T00:00:00Z", lane_uuids=[u],
              noise_allowlist=allow, identities={4242: {"uid": 1000, "argv0": BROWSER_817}}, context_types={(u, 4242): "C+G"})
    gpus = f"{u}, 7, 24576, 0, 40, 25.5, 280.0, Not Active, Not Active, 0"
    obs = occupancy_from_capture(gpus, f"{u}, 4242, {BROWSER_817}, 7", noise_cap_mib=64, **kw)
    assert obs["status"] == "ok" and obs["empty"] is True and [p["attribution"] for p in obs["processes"]] == ["noise"]
    over = occupancy_from_capture(gpus, f"{u}, 4242, {BROWSER_817}, 7", noise_cap_mib=5, **kw)
    assert over["empty"] is False  # the cap still holds
    assert errors(obs, "gpu-probe", "occupancy_observation") == []


@pytest.mark.parametrize("length,expect", [(4096, "noise"), (4097, "external")])
def test_oracle_nulls_an_argv0_over_4096_like_the_probe(length, expect) -> None:  # revision 3 (review finding 1)
    entry = "/usr/lib/browser/browser"
    long_line = entry + " " + "a" * (length - len(entry) - 1)
    u = "GPU-11111111-2222-3333-4444-555555555555"
    obs = occupancy_from_capture(f"{u}, 7, 24576, 0, 40, 25.5, 280.0, Not Active, Not Active, 0", f"{u}, 4242, browser, 7",
                                 returncode=0, lane_id="lane-t", host_id="host-1", observed_at="2026-10-04T00:00:00Z",
                                 lane_uuids=[u], noise_allowlist=[{"argv0": entry, "uid": 1000}], noise_cap_mib=64,
                                 identities={4242: {"uid": 1000, "argv0": long_line}}, context_types={(u, 4242): "C+G"})
    assert [p["attribution"] for p in obs["processes"]] == [expect]
    assert errors(obs, "gpu-probe", "occupancy_observation") == []


def test_oracle_refuses_a_non_ascii_digit_pid() -> None:  # revision 3 (review finding 3): the probe refuses it too
    u = "GPU-11111111-2222-3333-4444-555555555555"
    obs = occupancy_from_capture(f"{u}, 7, 24576, 0, 40, 25.5, 280.0, Not Active, Not Active, 0", f"{u}, \u0664\u0662, browser, 5",
                                 returncode=0, lane_id="lane-t", host_id="host-1", observed_at="2026-10-04T00:00:00Z", lane_uuids=[u])
    assert obs["status"] == "unknown"


# ---------------------------------------------------------------- 4. per-card PIDS query
def test_x_real_commands_name_the_per_card_pids_query() -> None:
    x = json.loads((ROOT / "contracts/v2/gpu-probe.schema.json").read_text(encoding="utf-8"))["x-real-commands"]
    assert x["occupancy_process_types"].startswith("nvidia-smi -q -d PIDS -i <uuid>")
    assert "PCI bus id" in x["occupancy_process_types"]
    assert "`-q -d PIDS -i <uuid>`" in brief("A3") or "nvidia-smi -q -d PIDS -i <uuid>" in brief("A3")


# ---------------------------------------------------------------- 5. the oracle never returns an ok the schema refuses
U = "GPU-11111111-2222-3333-4444-555555555555"
GOOD = [U, "100", "24576", "3", "40", "25.5", "280.0", "Not Active", "Not Active", "0"]


def _gpus(**over) -> str:
    idx = {"used": 1, "total": 2, "util": 3, "temp": 4, "power": 5, "limit": 6, "ecc": 9}
    cells = list(GOOD)
    for k, v in over.items():
        cells[idx[k]] = v
    return ", ".join(cells)


def _obs(gpus, procs="", ctype="C"):
    return occupancy_from_capture(gpus, procs, returncode=0, lane_id="lane-t", host_id="host-1", observed_at="2026-10-04T00:00:00Z",
                                  lane_uuids=[U], identities={4242: {"uid": 1000, "argv0": "python3"}}, context_types={(U, 4242): ctype})


@pytest.mark.parametrize("label,gpus,procs", [
    ("fractional utilization", _gpus(util="3.5"), ""), ("NaN utilization", _gpus(util="nan"), ""),
    ("temperature above 130", _gpus(temp="131"), ""), ("temperature below 0", _gpus(temp="-1"), ""),
    ("memory.total 0", _gpus(total="0"), ""), ("negative power draw", _gpus(power="-1.5"), ""),
    ("negative ECC count", _gpus(ecc="-2"), ""), ("pid 0", _gpus(), f"{U}, 0, python3, 5"),
    ("negative memory.used", _gpus(used="-5"), ""), ("infinite power limit", _gpus(limit="inf"), ""),
    ("negative process memory", _gpus(), f"{U}, 4242, python3, -5"),
])
def test_oracle_maps_values_nvidia_smi_does_not_print_to_unknown(label, gpus, procs) -> None:
    obs = _obs(gpus, procs)
    assert obs["status"] == "unknown" and obs["empty"] is False, label
    assert errors(obs, "gpu-probe", "occupancy_observation") == [], label


def test_oracle_maps_an_unknown_context_type_to_null() -> None:
    obs = _obs(_gpus(), f"{U}, 4242, python3, 5", ctype="X")
    assert obs["status"] == "ok" and obs["processes"][0]["context_type"] is None and obs["empty"] is False
    assert errors(obs, "gpu-probe", "occupancy_observation") == []


def test_oracle_output_is_always_schema_valid_on_a_seeded_corpus() -> None:
    rng = random.Random(20261004)
    values = ["0", "1", "100", "101", "-1", "3.5", "nan", "inf", "[N/A]", "N/A", "", "130", "131", "24576", "0.0", "1e3"]
    for _ in range(600):
        gpus = _gpus(**{k: rng.choice(values) for k in ("used", "total", "util", "temp", "power", "limit", "ecc")})
        procs = f"{U}, {rng.choice(['0', '1', '4242', '12'])}, python3, {rng.choice(values)}"
        obs = _obs(gpus, procs, ctype=rng.choice(["C", "G", "C+G", "X", None]))
        assert errors(obs, "gpu-probe", "occupancy_observation") == [], (gpus, procs, obs)


# ---------------------------------------------------------------- 6. G3 needs a status-ok observation
def test_g3_needs_a_status_ok_observation_from_the_real_host() -> None:
    gates = (ROOT / "docs/v2/GATES.txt").read_text(encoding="utf-8")
    g3 = gates.split("  G3 REAL TWIN", 1)[1].split("\n  G4", 1)[0]
    assert "at least one `status: ok` observation from the real host" in g3 and "vacuous" in g3
