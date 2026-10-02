"""Targeted contract rules of set v2: each test flips ONE field of a valid example and checks the
specific rule rejects it (so an invalid example cannot pass for an unrelated reason)."""

import copy

import pytest

from .validation import (
    ContractError,
    adapters_semantics,
    assert_invalid,
    assert_valid,
    examples,
    executor_semantics,
    inventory_semantics,
    quota_eval_semantics,
)


def first_valid(name: str, index: int = 0) -> dict:
    return copy.deepcopy(examples(name)["valid"][index])


# --- executor: G02 durations, Grok 1 identity, stop proof, definite refusal --------------------------------

def test_executor_deadlines_are_durations_not_foreign_boot_anchors() -> None:
    reserve = first_valid("executor", 0)
    assert_valid(reserve, "executor")
    reserve["deadlines"][0] = {"kind": "expiry", "boot_id": "boot-a", "deadline_s": 100, "utc_anchor": "2026-09-27T20:00:00Z", "monotonic_anchor_s": 10}
    assert_invalid(reserve, "executor")


def test_executor_identity_is_complete_before_reserve_and_carries_no_raw_token() -> None:
    reserve = first_valid("executor", 0)
    assert executor_semantics(reserve) == []
    no_unit = copy.deepcopy(reserve)
    no_unit["identity"]["unit"] = None
    assert_invalid(no_unit, "executor")
    raw = copy.deepcopy(reserve)
    raw["identity"]["token"] = "5f815964f2cbc5d6"
    assert_invalid(raw, "executor")
    wrong_name = copy.deepcopy(reserve)
    wrong_name["identity"]["unit"] = "flightctl-lane-gpu1-g8.service"
    assert executor_semantics(wrong_name), "unit must encode lane and generation"


def test_executor_stop_success_requires_occupancy_proof() -> None:
    reply = copy.deepcopy(examples("executor")["invalid"][1])
    assert_invalid(reply, "executor")
    reply["occupancy"] = first_valid("gpu-probe", 2)
    assert_valid(reply, "executor")
    reply["occupancy"]["tenants"] = [reply["occupancy"]["processes"][0] | {"attribution": "external", "used_memory_mib": 900}]
    assert_invalid(reply, "executor")


def test_executor_definite_reserve_refusal_wrote_no_fence() -> None:
    refusal = first_valid("executor", 2)
    assert_valid(refusal, "executor")
    refusal["observed_state"] = "reserved"
    assert_invalid(refusal, "executor")


def test_executor_error_reason_is_typed_not_a_string() -> None:
    refusal = first_valid("executor", 2)
    refusal["error"] = "executor reserve was not accepted"
    assert_invalid(refusal, "executor")


# --- probes and units: Grok 3/4/5 -------------------------------------------------------------------------

def test_occupancy_unknown_never_reads_as_empty() -> None:
    unknown = copy.deepcopy(examples("gpu-probe")["invalid"][0])
    assert_invalid(unknown, "gpu-probe")
    unknown["reason"] = "nvidia-smi exited 9"
    assert_valid(unknown, "gpu-probe")


def test_v1_fake_probe_shape_is_not_a_v2_observation() -> None:
    assert_invalid(examples("gpu-probe")["invalid"][2], "gpu-probe")


def test_absent_unit_has_empty_cgroup_and_unknown_is_never_empty() -> None:
    absent = first_valid("unit", 0)
    assert_valid(absent, "unit")
    absent["cgroup_pids"] = [77]
    assert_invalid(absent, "unit")
    unknown = copy.deepcopy(examples("unit")["invalid"][1]["observation"])
    assert_valid(unknown, "unit")
    unknown["cgroup_empty"] = True
    assert_invalid(unknown, "unit")


# --- lease, tokens, RPC ------------------------------------------------------------------------------------

@pytest.mark.parametrize("token,ok", [("5f815964f2cbc5d6", True), ("A" * 43, True), ("token-abcdefghijklmnop", False), ("5F815964F2CBC5D6", False)])
def test_lease_token_formats(token: str, ok: bool) -> None:
    request = first_valid("rpc-ops", 1)
    request["args"]["token"] = token
    (assert_valid if ok else assert_invalid)(request, "rpc-ops")


def test_lease_read_form_never_carries_a_token() -> None:
    read = first_valid("lease", 1)
    assert_valid(read, "lease")
    read["token"] = "5f815964f2cbc5d6"
    assert_invalid(read, "lease")


def test_preempt_addresses_target_by_public_lease_id() -> None:
    request = copy.deepcopy(examples("rpc-ops")["invalid"][0])
    assert_invalid(request, "rpc-ops")
    request["args"] = {"target_lease_id": "lse-0000100", "beneficiary": {"kind": "queue", "id": "q-0009"}}
    assert_valid(request, "rpc-ops")


def test_caller_cannot_supply_identity() -> None:
    request = copy.deepcopy(examples("rpc-ops")["invalid"][1])
    assert_invalid(request, "rpc-ops")
    del request["admission"]["ingress"]
    assert_valid(request, "rpc-ops")


def test_renew_is_ttl_based_not_additive() -> None:
    request = first_valid("rpc-ops", 1)
    request["args"] = {"token": "5f815964f2cbc5d6", "extend_s": 600}
    assert_invalid(request, "rpc-ops")


def test_pending_only_with_202_and_waking_names_the_attempt() -> None:
    bad = copy.deepcopy(examples("rpc-envelope")["invalid"][0])
    assert_invalid(bad, "rpc-envelope")
    bad["status"] = 202
    assert_valid(bad, "rpc-envelope")
    assert_invalid(examples("rpc-envelope")["invalid"][2], "rpc-envelope")


def test_errors_keep_their_cause_chain() -> None:
    assert_invalid(examples("rpc-envelope")["invalid"][1], "rpc-envelope")
    assert_valid(examples("rpc-envelope")["valid"][2], "rpc-envelope")


# --- policy, quota, accounts -------------------------------------------------------------------------------

def test_policy_renew_mode_and_quota_ref() -> None:
    policy = first_valid("policy")
    assert_valid(policy, "policy")
    additive = copy.deepcopy(policy)
    additive["lease"]["renew_mode"] = "additive-to-max"
    assert_invalid(additive, "policy")
    no_quota = copy.deepcopy(policy)
    del no_quota["quotas"]
    assert_invalid(no_quota, "policy")


def test_quota_denial_names_its_limit() -> None:
    deny = first_valid("quota", 1)
    assert quota_eval_semantics(deny) == []
    deny["limit"] = None
    assert_invalid(deny, "quota")


def test_agent_principal_requires_a_sponsor() -> None:
    agent = first_valid("accounts", 0)["principals"][1]
    assert_valid(agent, "accounts", "principal_record")
    agent["sponsor"] = None
    assert_invalid(agent, "accounts", "principal_record")


# --- inventory, templates ----------------------------------------------------------------------------------

def test_inventory_v2_has_no_identity_mapping_and_asleep_hosts_are_wakeable() -> None:
    assert_invalid(examples("inventory")["invalid"][0], "inventory")
    assert_invalid(examples("inventory")["invalid"][1], "inventory")


def test_inventory_enabled_lane_needs_card_uuid() -> None:
    inventory = first_valid("inventory")
    assert inventory_semantics(inventory) == []
    inventory["hosts"][1]["devices"][0]["uuid"] = None
    inventory["hosts"][1]["devices"][0]["unknown_reasons"]["uuid"] = "probe returned no uuid"
    assert_valid(inventory, "inventory")
    assert any("no uuid" in p for p in inventory_semantics(inventory))


def test_template_params_are_whole_argv_tokens() -> None:
    assert_invalid(examples("template")["invalid"][0], "template")
    assert_invalid(examples("template")["invalid"][1], "template")


# --- adapters: fail-closed selection ----------------------------------------------------------------------

def test_adapters_example_is_clean() -> None:
    config = first_valid("adapters")
    assert_valid(config, "adapters")
    assert adapters_semantics(config) == []


@pytest.mark.parametrize("mutate,expect", [
    (lambda c: c["ports"].__setitem__("occupancy_probe", "fake"), "lane lane-gpu1 is live but occupancy_probe is fake"),
    (lambda c: c["ports"].__setitem__("executor_transport", "fake"), "live profile refuses fake port executor_transport"),
    (lambda c: c["lanes"]["lane-gpu0"]["ports"].__setitem__("workload_runner", "real"), "lane lane-gpu0 is shadow but mutating port workload_runner is real"),
    (lambda c: c["lanes"]["lane-gpu0"].__setitem__("ports", {"workload_runner": "dryrun", "inhibitor": "dryrun", "occupancy_probe": "fake"}), "lane lane-gpu0 is shadow but occupancy_probe is fake"),
    (lambda c: c["allow_fake"].append("inhibitor"), "allow_fake may not list lane-critical port inhibitor"),
    (lambda c: c.__setitem__("profile", "shadow"), "lane lane-gpu1 is live but the site profile is shadow"),
    (lambda c: c["ports"].__setitem__("peer_identity", "dryrun"), "port peer_identity is read-only and has no dryrun twin"),
    # round 2 (Sol 6 B3)
    (lambda c: c["features"].__setitem__("endpoints", True), "port health_probe is required by an enabled feature and is fake"),
    (lambda c: c["features"].__setitem__("jobs", True), "allow_fake may not list model_cache: an enabled feature requires it"),
    (lambda c: c["ports"].__setitem__("command_runner", "fake"), "lane lane-gpu1 is live but command_runner is fake"),
    (lambda c: c["lanes"]["lane-gpu0"]["ports"].__setitem__("model_cache", "real"), "lane lane-gpu0 is shadow but mutating port model_cache is real"),
    (lambda c: c["lanes"]["lane-gpu0"]["ports"].__setitem__("notifier", "record"), "lane lane-gpu0 is shadow but mutating port notifier is record"),
    (lambda c: (c.__setitem__("profile", "shadow"), c["lanes"].__setitem__("lane-gpu1", {"mode": "off"}), c["ports"].__setitem__("model_cache", "real")), "shadow profile: mutating port model_cache is real"),
])
def test_adapters_fail_closed(mutate, expect: str) -> None:
    config = first_valid("adapters")
    mutate(config)
    problems = adapters_semantics(config)
    assert any(expect in p for p in problems), problems


def test_legacy_shim_vectors_validate_and_keep_the_legacy_token_contract() -> None:
    import json
    import re
    from pathlib import Path

    vectors = json.loads((Path(__file__).parent / "vectors" / "legacy-shim.json").read_text(encoding="utf-8"))
    assert_valid(vectors, "legacy-compat")
    for vector in vectors["vectors"]:
        if vector["argv"][0] in {"acquire", "wait"} and vector["exit"] == 0:
            out = vector["stdout"]
            assert out is None or re.fullmatch(r"[0-9a-f]{16}\n", out), vector["name"]
        for call in vector["rpc"]:
            if call["op"] == "acquire":
                assert call["args_subset"].get("token_format", "hex16") == "hex16", vector["name"]
    assert any(v["argv"][:1] == ["wait"] and any(c["args_subset"].get("max_wait_s") == 7200 for c in v["rpc"]) for v in vectors["vectors"])


@pytest.mark.parametrize("example,schema", [
    ("adapters-v2", "adapters"), ("policy-v2", "policy"), ("inventory-v2", "inventory"), ("quotas-v2", "quota"),
    ("accounts-v2", "accounts"), ("storage-v2", "storage"), ("templates-v2", "template"), ("legacy-shim-v2", "legacy-compat"),
])
def test_v2_config_examples_validate(example: str, schema: str) -> None:
    import json
    from pathlib import Path

    document = json.loads((Path(__file__).parents[2] / "config" / f"{example}.json.example").read_text(encoding="utf-8"))
    assert_valid(document, schema)
    if schema == "adapters":
        assert adapters_semantics(document) == []
    if schema == "inventory":
        assert inventory_semantics(document) == []


def test_beat_cannot_move_the_host_ceiling() -> None:
    """N1: max-end is set once at reserve; only 'extend' with an approval moves it."""
    assert_invalid(examples("executor")["invalid"][-1], "executor")
    extend = {"schema_version": 2, "kind": "extend", "controller_request_id": "creq-4", "controller_id": "controller-a", "sent_at": "2026-10-02T11:00:00Z",
              "identity": first_valid("executor", 1)["identity"], "approval_id": "apr-0002", "max_end": {"kind": "max-end", "in_s": 7200, "sender_utc": "2026-10-02T11:00:00Z"}}
    assert_valid(extend, "executor")
    del extend["approval_id"]
    assert_invalid(extend, "executor")


def test_present_cache_entry_requires_sha256_verification() -> None:
    """N2: size-only verification never makes a shared model 'present'."""
    assert_invalid(examples("storage")["invalid"][-1], "storage")


@pytest.mark.parametrize("quota_max,cls,lane_max,want", [
    (None, "batch", 43200, 14400),      # default: batch class ceiling 4 h
    (43200, "batch", 43200, 43200),     # reviewed 12 h unattended workload via its quota entry (N3)
    (86400, "batch", 43200, 43200),     # never above the absolute maximum or lane cap
    (43200, "batch", 21600, 21600),
])
def test_lease_ceiling_quota_replaces_class_default(quota_max, cls, lane_max, want) -> None:
    from .validation import lease_ceiling
    assert lease_ceiling(first_valid("policy"), lane_max, quota_max, cls) == want


def test_shadow_flip_needs_path_coverage_and_mirror_errors_block() -> None:
    """N4."""
    policy = first_valid("policy")
    assert policy["shadow"]["mirror_errors_block"] is True and "adapter-error" in policy["shadow"]["divergence_classes_blocking"]
    del policy["shadow"]["min_path_counts"]
    assert_invalid(policy, "policy")


def test_closed_session_requires_proof_of_ended_processes() -> None:
    """B5: removing the key is not enough; the user slice and the lane must be proven empty."""
    assert_invalid(examples("session")["invalid"][-1], "session")


def test_contract_error_is_raised_for_unexpected_acceptance() -> None:
    with pytest.raises(ContractError):
        assert_invalid(first_valid("adapters"), "adapters")
