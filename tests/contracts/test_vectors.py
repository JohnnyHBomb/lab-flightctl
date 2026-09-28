import copy
import json
from pathlib import Path

import pytest

from .validation import (
    ContractError,
    assert_invalid,
    pipeline_admission,
    validate_discovery,
    validate_definition,
    validate_inventory,
    validate_instance,
    validate_rpc,
)


VECTOR_DIR = Path(__file__).parent / "vectors"
ROOT = VECTOR_DIR.parents[2]


def _load(name: str):
    return json.loads((VECTOR_DIR / name).read_text(encoding="utf-8"))


def _rpc_defaults(vector: dict, request: dict, index: int) -> dict:
    decorated = copy.deepcopy(vector["request_defaults"])
    decorated.update(copy.deepcopy(request))
    decorated["idempotency_scope"] = copy.deepcopy(vector["request_defaults"]["idempotency_scope"])
    decorated["request_fingerprint"] = f"{index + 1:064x}"
    decorated["admission"] = copy.deepcopy(vector["request_defaults"]["admission"])
    op = decorated["op"]
    args = decorated["args"]
    if op in {"approve", "preempt"}:
        approval_id = args.get("approval_id")
        decorated["admission"]["approval"] = {"approval_id": approval_id, "required": True, "consume_atomically": True}
    if op in {"acquire", "chat-load"} and args.get("pipeline_ref"):
        decorated["admission"]["pipeline"] = {
            "pipeline_id": args["pipeline_ref"],
            "version": "1.0.0",
            "revision": 1,
            "purpose": args["purpose"],
            "policy_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        }
    return decorated


def test_rpc_operation_vectors() -> None:
    vector = _load("rpc.json")
    assert len(vector["requests"]) == 19
    assert {request["op"] for request in vector["requests"]} == {"acquire", "renew", "release", "claim", "queue", "book", "cancel", "approval-request", "approve", "preempt", "chat-load", "chat-unload", "cal", "free", "report", "status"}
    assert {request["args"].get("action") for request in vector["requests"] if request["op"] == "queue"} == {"add", "refresh", "remove", "list"}
    assert {item["op"] for item in vector["operation_expectations"]} == {request["op"] for request in vector["requests"]}
    assert all(item["result_kind"] and item["state"] for item in vector["operation_expectations"])
    for index, request in enumerate(vector["requests"]):
        validate_rpc(_rpc_defaults(vector, request, index))
    for response in vector["responses"]:
        validate_instance(response, "rpc-envelope-v1.schema.json")
    validate_instance(vector["idempotency"]["record"], "rpc-envelope-v1.schema.json")
    for index, request in enumerate(vector["negative_requests"]):
        assert_invalid(_rpc_defaults(vector, request, 100 + index), "rpc-ops-v1.schema.json")
    assert vector["idempotency"]["persisted_result"] == ["request_id", "idempotency_scope", "request_fingerprint", "status", "data", "error", "stored_at"]


def test_cli_golden_vectors() -> None:
    vector = _load("cli.json")
    exits = set()
    for index, case in enumerate(vector["cases"]):
        for trace_index, request in enumerate(case["expected_rpc"]):
            decorated = {
                "schema": 1,
                "request_id": f"cli-{index}-{trace_index}",
                **request,
            }
            rpc_vector = {"request_defaults": _load("rpc.json")["request_defaults"]}
            validate_rpc(_rpc_defaults(rpc_vector, decorated, index * 10 + trace_index))
        validate_instance(case["response"], "rpc-envelope-v1.schema.json")
        assert case["response"]["data"] is None or case["response"]["data"]["kind"] in {"mutation", "pending", "projection", "status", "grant"}
        mapping_key = case.get("exit_key", str(case["response"]["status"]))
        assert case["exit"] == vector["exit_mapping"][mapping_key]
        exits.add(case["exit"])
        if case.get("trace"):
            assert case["trace"] == [request["op"] for request in case["expected_rpc"]]
    assert exits == {0, 1, 2, 3, 4, 5}
    assert vector["cases"][0]["argv"][1] == "lane-gpu0"
    assert vector["cases"][1]["argv"] == ["release", "lane-gpu0", "token-abcdefghijklmnop"]
    assert vector["cases"][2]["expected_rpc"][0]["args"]["extend_s"] == 900
    assert vector["cases"][3]["expected_rpc"][0]["args"]["max_wait_s"] == 300
    assert vector["cases"][4]["expected_rpc"][0]["args"]["max_wait_s"] == 600
    assert vector["cases"][1]["lookup"]["principal_and_lane_checked"]
    grant = vector["cases"][0]["response"]["data"]
    assert grant["kind"] == "grant" and grant["token"] and grant["generation"] == 7
    assert grant["adoption"]["mode"] == "fresh-acquire"
    assert vector["cases"][10]["trace"] == ["acquire", "release"]
    assert vector["cases"][11]["trace"] == ["claim", "release"]
    for index, case in enumerate(vector.get("negative_cases", [])):
        for trace_index, request in enumerate(case["expected_rpc"]):
            decorated = {"schema": 1, "request_id": f"cli-negative-{index}-{trace_index}", **request}
            rpc_vector = {"request_defaults": _load("rpc.json")["request_defaults"]}
            assert_invalid(_rpc_defaults(rpc_vector, decorated, 100 + index * 10 + trace_index), "rpc-ops-v1.schema.json")


def test_executor_vectors() -> None:
    vector = _load("executor.json")
    for request in vector["requests"]:
        validate_instance(request, "executor-v1.schema.json")
    for reply in vector["replies"]:
        validate_instance(reply, "executor-v1.schema.json")
    for item in vector["negative"]:
        assert_invalid(item, "executor-v1.schema.json")
    assert vector["controller_rule"].startswith("start is reserved")
    contradictory = copy.deepcopy(vector["replies"][0])
    contradictory["uncertain"] = True
    contradictory["observed_state"] = "running"
    with pytest.raises(ContractError):
        validate_instance(contradictory, "executor-v1.schema.json")


def test_discovery_vectors() -> None:
    vector = _load("discovery.json")
    for case in vector["cases"]:
        validate_discovery(case["proposal"])
        assert case["expected"]
    cases_by_name = {case["name"]: case for case in vector["cases"]}
    for negative in vector.get("negative_cases", []):
        proposal = copy.deepcopy(cases_by_name[negative["base"]]["proposal"])
        for mutation in negative["mutations"]:
            target = proposal
            for part in mutation["path"][:-1]:
                target = target[part]
            target[mutation["path"][-1]] = mutation["value"]
        with pytest.raises(ContractError, match=negative["error"]):
            validate_discovery(proposal)
    inventory = json.loads((ROOT / "config" / "inventory.json.example").read_text(encoding="utf-8"))
    for case in vector.get("inventory_cases", []):
        candidate = copy.deepcopy(inventory)
        candidate["stage"] = case["stage"]
        candidate["hosts"][0]["reachability"] = case["reachability"]
        candidate["hosts"][0]["observed_at"] = None
        candidate["hosts"][0]["observation_error"] = "probe timed out"
        candidate["lanes"][0]["enabled"] = case["lane_enabled"]
        if case["expected"] == "allow":
            validate_inventory(candidate)
        else:
            with pytest.raises(ContractError, match="enabled lane uses unavailable host"):
                validate_inventory(candidate)
    assert vector["projection"]["mapped"] == ["schema_version", "site_id", "revision", "controller", "timezone", "identity_mapping", "chat_lane_order", "hosts", "lanes"]
    assert "hosts[].admissible" in vector["projection"]["dropped"]
    assert "lanes[].action" in vector["projection"]["dropped"]
    dangling = copy.deepcopy(vector["cases"][0]["proposal"])
    dangling["lanes"][0]["device_ids"] = ["missing-device"]
    with pytest.raises(ContractError, match="dangling discovery device"):
        validate_discovery(dangling)
    unsorted = copy.deepcopy(vector["cases"][0]["proposal"])
    unsorted["diff"].insert(0, {"sort_key": "zzz", "kind": "unknown", "id": "zzz", "change": "unknown", "before": None, "after": None, "review_required": True})
    with pytest.raises(ContractError, match="deterministic"):
        validate_discovery(unsorted)


def test_policy_vectors() -> None:
    vector = _load("policy.json")
    assert {item["availability"] for item in vector["pipelines"]} == {"available", "unavailable", "approval_required"}
    assert "approval replay" in vector["rejections"]
    config = json.loads((ROOT / "config" / "pipelines.json.example").read_text(encoding="utf-8"))
    pipelines = {item["pipeline_id"]: item for item in config["pipelines"]}
    aliases = {"available": "batch", "unavailable": "retired", "approval": "interactive"}
    requester = {"site_id": "site-a", "tenant_id": "tenant-a", "issuer": "issuer-a", "subject": "subject-a"}
    for case in vector["decision_vectors"]:
        pipeline = pipelines[aliases[case["pipeline_id"]]]
        if case["name"] == "empty approval is never admission":
            approval = {}
        elif case["name"] == "unsigned approval is never admission":
            approval = {"consumed_at": None}
        else:
            approval = None
        result = pipeline_admission(
            pipeline,
            case["purpose"],
            approval=approval,
            policy_hash=case.get("policy_hash", pipeline["policy_hash"]),
            destination_site=case["destination_site"],
            content_labels=case.get("content_labels"),
            requester=requester,
            payload_hash="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            manifest_hash=None,
        )
        assert result is case["expected"], case["name"]


def test_valid_and_invalid_vector_files() -> None:
    valid = _load("valid.json")
    assert valid["rules"]["idempotency"]["same_request_changed_args"] == "409 conflict"
    assert valid["rules"]["unknown_measurements"]["reason_required"]
    assert valid["rules"]["signed_manifest"]["image_digest_required"]
    assert valid["rules"]["event_states"]["approval"] == ["issued", "approved", "consumed", "revoked"]
    assert valid["rules"]["event_states"]["booking"] == ["recovery"]
    invalid = _load("invalid.json")
    for case in invalid["cases"]:
        assert case["expected"] == "deny"
    assert any(case["id"] == "manifest-without-image-digest" for case in invalid["cases"])
    assert_invalid({"schema_version": 2, "principal": {}, "clock": {}}, "common.schema.json")
    with pytest.raises(ContractError):
        validate_definition("2026-09-27T20:00:00+01:00", "common.schema.json", "utc_time")
    with pytest.raises(ContractError):
        validate_definition({"certainty": "unknown", "reason": None}, "common.schema.json", "measurement")
    inventory = json.loads((ROOT / "config" / "inventory.json.example").read_text(encoding="utf-8"))
    inventory["lanes"][0]["host_id"] = "missing-host"
    with pytest.raises(ContractError):
        validate_inventory(inventory)
    rpc = _rpc_defaults(_load("rpc.json"), _load("rpc.json")["requests"][3], 200)
    rpc["args"]["extend_s"] = 0
    with pytest.raises(ContractError):
        validate_rpc(rpc)
