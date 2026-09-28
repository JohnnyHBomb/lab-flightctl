"""Executable positive/negative vectors for the four P0.1 review gaps."""

import copy
import hashlib
import json

import pytest

from .test_vectors import ROOT, _load
from .validation import (
    ContractError,
    assert_invalid,
    load_schema,
    pipeline_admission,
    validate_approval_binding,
    validate_definition,
    validate_instance,
    validate_rpc,
)


def test_pending_release_vectors():
    cases = _load("p0-1.json")["pending_release"]
    validate_instance(cases["valid"], "rpc-envelope-v1.schema.json")
    for invalid in cases["invalid"]:
        assert_invalid(invalid, "rpc-envelope-v1.schema.json")
    assert _load("cli.json")["exit_mapping"][str(cases["valid"]["status"])] == 5
    record = copy.deepcopy(_load("rpc.json")["idempotency"]["record"])
    for response, valid in [(cases["valid"], True), *[(item, False) for item in cases["invalid"]]]:
        record.update({key: response[key] for key in ("request_id", "status", "data", "error")})
        if valid:
            validate_instance(record, "rpc-envelope-v1.schema.json")
        else:
            assert_invalid(record, "rpc-envelope-v1.schema.json")


def test_owner_release_vectors():
    cases = _load("p0-1.json")["owner_release"]
    validate_instance(cases["valid"], "executor-v1.schema.json")
    for invalid in cases["invalid"]:
        assert_invalid(invalid, "executor-v1.schema.json")
    forced = copy.deepcopy(cases["valid"])
    forced["stop_authority"]["mode"] = "approved-forced-preemption"
    assert_invalid(forced, "executor-v1.schema.json")
    unprotected = copy.deepcopy(cases["valid"])
    unprotected["execution_policy"].update(protected=False, preemptible=True)
    validate_instance(unprotected, "executor-v1.schema.json")


def test_content_label_vectors():
    cases = _load("p0-1.json")["content_labels"]
    pipeline = json.loads((ROOT / "config/pipelines.json.example").read_text())["pipelines"][0]
    for key, expected in (("valid", True), ("policy_denied", False)):
        request = cases[key]
        validate_rpc(request)
        assert pipeline_admission(
            pipeline, request["args"]["purpose"],
            content_labels=request["admission"]["content_labels"],
        ) is expected
    assert_invalid(cases["invalid"], "rpc-ops-v1.schema.json")
    for labels in (None, ["acceptable-use", "acceptable-use"], [1], [""]):
        invalid = copy.deepcopy(cases["valid"])
        invalid["admission"]["content_labels"] = labels
        assert_invalid(invalid, "rpc-ops-v1.schema.json")


def _projection(request, case):
    validate_rpc(request)
    admission = request["admission"]
    if admission["delegation"] is not None or request["args"].get("signed_manifest") is not None:
        raise ContractError("local action cannot silently discard a security hook")
    result = {key: copy.deepcopy(request[key]) for key in ("schema", "op", "lane", "args")}
    result["args"].pop("approval_id", None)
    result.update(
        requester=admission["ingress"]["subject"] or admission["ingress"]["actor"],
        pipeline=admission["pipeline"], content_labels=admission.get("content_labels", []),
        batch=admission["batch"],
        **{key: case[key] for key in ("destination_site", "controller_id", "policy_hash", "manifest_hash")},
    )
    validate_definition(result, "approval-v1.schema.json", "local_action")
    return result


def _vector_jcs(value):
    """JCS subset for these vectors: safe integers, strings, containers, literals.

    This is a vector oracle, not a general production number serializer.
    """
    if isinstance(value, dict):
        return "{" + ",".join(
            _vector_jcs(key) + ":" + _vector_jcs(value[key])
            for key in sorted(value, key=lambda key: key.encode("utf-16-be"))
        ) + "}"
    if isinstance(value, list):
        return "[" + ",".join(map(_vector_jcs, value)) + "]"
    assert value is None or isinstance(value, (str, bool, int))
    if type(value) is int:
        assert abs(value) <= 2**53 - 1
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _payload(projection):
    contract = load_schema("approval-v1.schema.json")["x-local-action-hash"]
    pairs = [[field, projection[field]] for field in contract["fields"]]
    canonical = _vector_jcs(pairs).encode("utf-8")
    return canonical, hashlib.sha256(contract["domain"].encode() + b"\0" + canonical).hexdigest()


def test_local_action_hash_vectors():
    case = _load("p0-1.json")["local_action"]
    contract = load_schema("approval-v1.schema.json")["x-local-action-hash"]
    projection = _projection(case["request"], case)
    assert projection == case["projection"]
    assert contract == {
        "fields": case["fields"], "domain": "flightctl/local-action/v1",
        "canonicalization": "JCS-RFC8785", "encoding": "UTF-8",
        "hash": "SHA-256", "output": "lowercase-hex", "separator_hex": "00",
    }
    canonical, digest = _payload(projection)
    assert canonical == case["canonical_utf8"].encode("utf-8")
    assert digest == case["payload_hash"]
    assert hashlib.sha256(canonical).hexdigest() != digest
    assert hashlib.sha256(b"flightctl/approval/v1\0" + canonical).hexdigest() != digest
    for mutation in case["negative_mutations"]:
        changed = copy.deepcopy(case["request"])
        target = changed
        for key in mutation["path"][:-1]:
            target = target[key]
        target[mutation["path"][-1]] = mutation["value"]
        if mutation["path"] == ["args", "purpose"]:
            changed["admission"]["pipeline"]["purpose"] = mutation["value"]
        assert _payload(_projection(changed, case))[1] != digest
    invalid = copy.deepcopy(projection)
    invalid["args"]["approval_id"] = "approval-a"
    with pytest.raises(ContractError):
        validate_definition(invalid, "approval-v1.schema.json", "local_action")


def test_local_action_transport_and_approval_independence():
    case = _load("p0-1.json")["local_action"]
    changed = copy.deepcopy(case["request"])
    changed["request_id"] = "another-request"
    changed["request_fingerprint"] = "b" * 64
    changed["admission"]["ingress"]["authenticated_peer"] = "another-peer"
    changed["admission"]["approval"] = {
        "approval_id": "approval-new", "required": True, "consume_atomically": True,
    }
    assert _payload(_projection(changed, case))[1] == case["payload_hash"]
    changed["op"] = "preempt"
    changed["args"] = {"token": "token-abcdefghijklmnop", "approval_id": "approval-new"}
    changed["admission"]["pipeline"] = None
    first = _payload(_projection(changed, case))[1]
    changed["args"]["approval_id"] = "approval-other"
    changed["admission"]["approval"]["approval_id"] = "approval-other"
    assert _payload(_projection(changed, case))[1] == first
    del changed["admission"]["content_labels"]
    absent = _payload(_projection(changed, case))[1]
    changed["admission"]["content_labels"] = []
    assert _payload(_projection(changed, case))[1] == absent


def test_recomputed_payload_binds_approval():
    """Use the actual approval-binding checker, not just unequal hash strings."""
    from .test_schemas import _evidence, _proof

    case = _load("p0-1.json")["local_action"]
    approval = load_schema("approval-v1.schema.json")["x-examples"]["valid"][0]
    approval.update(
        action="pipeline", lane=None, target_generation=None, booking_id=None, revision=None,
        state="approved", approved_at="2026-09-27T20:00:30Z",
        approver=case["projection"]["requester"], proof=_proof(), verified_evidence=_evidence(),
        payload_hash=case["payload_hash"], policy_hash=case["policy_hash"],
        challenge_policy_hash=case["policy_hash"], manifest_hash=None, challenge_manifest_hash=None,
    )

    def check(request):
        validate_approval_binding(
            approval, payload_hash=_payload(_projection(request, case))[1],
            destination_site=case["destination_site"], controller_id=case["controller_id"],
            policy_hash=case["policy_hash"], manifest_hash=None, action="pipeline",
            requester=case["projection"]["requester"],
        )

    check(case["request"])
    for mutation in case["negative_mutations"]:
        changed = copy.deepcopy(case["request"])
        target = changed
        for key in mutation["path"][:-1]:
            target = target[key]
        target[mutation["path"][-1]] = mutation["value"]
        with pytest.raises(ContractError, match="payload_hash"):
            check(changed)
