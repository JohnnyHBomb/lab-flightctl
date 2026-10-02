"""B2 (Sol 6): GPU emptiness requires an aggregate-memory check and treats unknown process memory as a tenant.
Fixed against real nvidia-smi captures (sanitised) plus review counterexamples."""

import copy
import json
from pathlib import Path

import pytest

from .validation import assert_invalid, assert_valid, examples, occupancy_from_capture, occupancy_semantics

CAPTURES = json.loads((Path(__file__).parent / "captures" / "gpu-occupancy.json").read_text(encoding="utf-8"))
CASES = {case["name"]: case for case in CAPTURES["cases"]}


@pytest.mark.parametrize("name", sorted(CASES))
def test_capture_verdicts(name: str) -> None:
    case = CASES[name]
    obs = occupancy_from_capture(case["gpus"], case["procs"], returncode=case["returncode"], lane_id="lane-x", host_id="host-x",
                                 observed_at="2026-10-02T00:00:00Z", lane_uuids=CAPTURES["lane_cards"][case["lane"]],
                                 attributed_pids=frozenset(case.get("attributed_pids", [])))
    assert_valid(obs, "gpu-probe", "occupancy_observation")
    assert occupancy_semantics(obs) == []
    want = case["expect"]
    assert obs["status"] == want["status"]
    assert [p["pid"] for p in obs["tenants"]] == want["tenant_pids"]
    assert [p["pid"] for p in obs["noise"]] == want["noise_pids"]
    assert obs["lane_memory_used_mib"] == want["lane_memory_used_mib"]
    assert obs["unexplained_mib"] == want["unexplained_mib"]
    assert obs["empty"] is want["empty"]
    if "process_name" in want:
        assert obs["processes"][0]["process_name"] == want["process_name"]
    if "thermal_slowdown" in want:
        assert obs["gpus"][0]["thermal_slowdown"] is want["thermal_slowdown"]


def test_sol6_counterexample_19gib_cannot_claim_empty() -> None:
    obs = copy.deepcopy(examples("gpu-probe")["valid"][2])
    obs["gpus"][0]["memory_used_mib"] = 19456
    obs["lane_memory_used_mib"] = 19456
    obs["unexplained_mib"] = 19451
    obs["processes"] = obs["noise"]
    assert_valid(obs, "gpu-probe")  # shape alone cannot compare two fields...
    assert any("contradicts" in p for p in occupancy_semantics(obs))  # ...the semantic oracle refuses it


def test_unknown_memory_tenant_cannot_be_empty_by_schema() -> None:
    assert_invalid(examples("gpu-probe")["invalid"][2], "gpu-probe")


def test_stop_success_requires_empty_true() -> None:
    reply = copy.deepcopy(examples("executor")["invalid"][1])
    reply["occupancy"] = copy.deepcopy(examples("gpu-probe")["valid"][2])
    assert_valid(reply, "executor")
    reply["occupancy"]["empty"] = False
    assert_invalid(reply, "executor")
