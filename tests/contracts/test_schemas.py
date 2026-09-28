import copy
import json
from pathlib import Path

import pytest

from .test_vectors import _load as load_vector, _rpc_defaults
from .validation import (
    ContractError,
    SCHEMA_FILES,
    assert_invalid,
    assert_valid,
    examples,
    pipeline_admission,
    require_jsonschema,
    validate_approval,
    validate_approval_binding,
    validate_booking,
    validate_booking_transition,
    validate_delegation_bounds,
    validate_definition,
    validate_inventory,
    validate_pipeline_config,
    validate_rpc,
    validate_signed_manifest,
    validate_state_transition,
    validator,
)


ROOT = Path(__file__).parents[2]


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _proof() -> dict:
    return {"scheme": "ssh-sk", "key_id": "key-a", "namespace": "flightctl/approval/v1", "encoding": "openssh-ssh-sk-signature/base64", "signature_b64": "c2ln"}


def _evidence() -> dict:
    return {"verifier": "controller-a", "verified_at": "2026-09-27T20:01:00Z", "user_presence": "verified", "user_verification": "verified"}


def test_schema_examples() -> None:
    require_jsonschema()
    assert len(SCHEMA_FILES) == 13
    for path in SCHEMA_FILES:
        schema = validator(path)
        schema.check_schema(schema.schema)
        sample = examples(path.name)
        assert sample["valid"], f"{path.name} has no valid example"
        assert sample["invalid"], f"{path.name} has no invalid example"
        for instance in sample["valid"]:
            assert_valid(instance, path.name)
        for instance in sample["invalid"]:
            assert_invalid(instance, path.name)


def test_referencing_registry_is_local_and_unresolved_refs_fail() -> None:
    import jsonschema
    from referencing import Registry

    unresolved = {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "https://flightctl.local/test-unresolved.json", "$ref": "https://unconfigured.invalid/no-network.json"}
    with pytest.raises(Exception, match="unconfigured.invalid"):
        list(jsonschema.Draft202012Validator(unresolved, registry=Registry()).iter_errors({}))
    assert "RefResolver" not in (ROOT / "tests" / "contracts" / "validation.py").read_text(encoding="utf-8")


def test_inventory_semantics() -> None:
    inventory = _load(ROOT / "config" / "inventory.json.example")
    validate_inventory(inventory)

    duplicate = copy.deepcopy(inventory)
    duplicate["lanes"].append(copy.deepcopy(duplicate["lanes"][0]))
    with pytest.raises(ContractError, match="duplicate lane_id"):
        validate_inventory(duplicate)

    dangling = copy.deepcopy(inventory)
    dangling["lanes"][0]["host_id"] = "missing-host"
    with pytest.raises(ContractError, match="dangling lane host_id"):
        validate_inventory(dangling)

    no_gpu = copy.deepcopy(inventory)
    no_gpu["stage"] = "draft"
    no_gpu["controller"]["endpoint"] = None
    no_gpu["controller"]["account"] = None
    no_gpu["controller"]["auth_state"] = "unresolved"
    no_gpu["hosts"][0]["gpu_count"] = 0
    no_gpu["hosts"][0]["gpu_count_reason"] = "confirmed no GPU"
    no_gpu["hosts"][0]["devices"] = []
    no_gpu["lanes"] = []
    no_gpu["chat_lane_order"] = []
    validate_inventory(no_gpu)

    unknown = copy.deepcopy(no_gpu)
    unknown["hosts"][0]["gpu_count"] = None
    unknown["hosts"][0]["gpu_count_reason"] = "probe unavailable"
    validate_inventory(unknown)

    nested_zone = copy.deepcopy(no_gpu)
    nested_zone["timezone"] = "Area/Region/City"
    with pytest.raises(ContractError, match="timezone"):
        validate_inventory(nested_zone)


def test_occupant_read_form_rejects_raw_token() -> None:
    read_form = copy.deepcopy(_load(ROOT / "contracts" / "occupant-v1.schema.json")["x-examples"]["valid"][1])
    read_form["token"] = "token-abcdefghijklmnop"
    with pytest.raises(ContractError):
        validate_definition(read_form, "occupant-v1.schema.json", "read_form")


def test_approval_bindings() -> None:
    approval = _load(ROOT / "contracts" / "approval-v1.schema.json")["x-examples"]["valid"][0]
    validate_approval(approval, now="2026-09-27T20:01:00Z")
    approved = copy.deepcopy(approval)
    approved["state"] = "approved"
    approved["approver"] = copy.deepcopy(approval["requester"])
    approved["approved_at"] = "2026-09-27T20:01:00Z"
    approved["proof"] = _proof()
    approved["verified_evidence"] = _evidence()
    validate_approval_binding(approved, destination_site="site-a", controller_id="controller-a", payload_hash=approved["payload_hash"], policy_hash=approved["policy_hash"], manifest_hash=None, action="forced-preemption", requester=approved["requester"], generation=4, lane=approved["lane"], booking_id=approved["booking_id"])
    wrong = copy.deepcopy(approved)
    wrong["payload_hash"] = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    with pytest.raises(ContractError, match="payload_hash"):
        validate_approval_binding(wrong, destination_site="site-a", controller_id="controller-a", payload_hash=approved["payload_hash"], policy_hash=approved["policy_hash"], manifest_hash=None, generation=4)
    replay = copy.deepcopy(approved)
    replay["state"] = "consumed"
    replay["consumed_at"] = "2026-09-27T20:02:00Z"
    with pytest.raises(ContractError, match="replay"):
        validate_approval_binding(replay, destination_site="site-a", controller_id="controller-a", payload_hash=replay["payload_hash"], policy_hash=replay["policy_hash"], manifest_hash=None, generation=4)
    changed = copy.deepcopy(approved)
    changed["revision"] = 2
    with pytest.raises(ContractError, match="revision"):
        validate_approval_binding(changed, destination_site="site-a", controller_id="controller-a", payload_hash=changed["payload_hash"], policy_hash=changed["policy_hash"], manifest_hash=None, revision=1, generation=4)
    unsigned = copy.deepcopy(approval)
    unsigned["state"] = "approved"
    with pytest.raises(ContractError, match="proof"):
        validate_approval(unsigned)


def test_pipeline_policy_vectors() -> None:
    config = _load(ROOT / "config" / "pipelines.json.example")
    validate_pipeline_config(config)
    available, approval_required, unavailable = config["pipelines"]
    requester = {"site_id": "site-a", "tenant_id": "tenant-a", "issuer": "issuer-a", "subject": "subject-a"}
    assert pipeline_admission(available, "batch inference", policy_hash=available["policy_hash"], destination_site="site-a", content_labels=["acceptable-use"])
    assert not pipeline_admission(unavailable, "maintenance test")
    assert not pipeline_admission(approval_required, "interactive inference", destination_site="site-a", requester=requester, payload_hash="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    assert not pipeline_admission(available, "batch inference", destination_site="site-b", content_labels=["acceptable-use"])
    assert not pipeline_admission(available, "batch inference", destination_site="site-a", content_labels=["rejected-content"])
    assert not pipeline_admission(available, "batch inference", policy_hash="cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc", destination_site="site-a")
    assert not pipeline_admission(approval_required, "interactive inference", approval={})
    assert available["partner_overrides"]["site-b"] == "approval_required"

    approval = _load(ROOT / "contracts" / "approval-v1.schema.json")["x-examples"]["valid"][0]
    approval["action"] = "pipeline"
    approval["lane"] = None
    approval["target_generation"] = None
    approval["state"] = "approved"
    approval["approver"] = copy.deepcopy(requester)
    approval["approved_at"] = "2026-09-27T20:01:00Z"
    approval["proof"] = _proof()
    approval["verified_evidence"] = _evidence()
    approval["policy_hash"] = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    approval["challenge_policy_hash"] = approval["policy_hash"]
    assert pipeline_admission(approval_required, "interactive inference", approval=approval, destination_site="site-a", requester=requester, payload_hash=approval["payload_hash"], policy_hash=approval_required["policy_hash"], action="pipeline") is False
    approval["policy_hash"] = approval_required["policy_hash"]
    approval["challenge_policy_hash"] = approval_required["policy_hash"]
    assert pipeline_admission(approval_required, "interactive inference", approval=approval, destination_site="site-a", requester=requester, payload_hash=approval["payload_hash"], policy_hash=approval_required["policy_hash"], action="pipeline")


def test_security_hook_contracts() -> None:
    policy = _load(ROOT / "config" / "policy.json.example")
    validate_definition(policy, "common.schema.json", "policy")
    proof = _proof()
    validate_definition(proof, "common.schema.json", "proof")
    bad_proof = copy.deepcopy(proof)
    bad_proof["scheme"] = "unknown"
    with pytest.raises(ContractError):
        validate_definition(bad_proof, "common.schema.json", "proof")
    manifest = {
        "schema_version": 1,
        "job_id": "job-a",
        "request_id": "req-a",
        "origin_site": "site-a",
        "destination_site": "site-b",
        "principal": { "site_id": "site-a", "tenant_id": "tenant-a", "issuer": "issuer-a", "subject": "subject-a" },
        "nonce": "nonce-a",
        "expires": "2026-09-27T21:00:00Z",
        "pipeline_id": "batch",
        "pipeline_version": "1.0.0",
        "image_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "parameters": { "batch_size": { "type": "integer", "value": 1 } },
        "input_hashes": ["aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
        "output_policy": { "destination": "receipt-a", "retention_s": 60, "sanitise": True, "receipt_required": True, "transfer": { "interface_version": 1, "mode": "local", "destination": "receipt-a", "object_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "authenticated_context_ref": "ctx-a", "key_rotation_ref": None }, "receipt_ref": None },
        "resource_ceiling": { "max_s": 600, "max_vram_bytes": 17179869184 },
        "deadline_s": 600,
        "policy_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "required_assurance": { "required": ["observed"], "offered": [], "verified": [], "evidence_refs": [], "unknown": False },
        "canonical_payload_hash": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
        "proof": proof,
        "key_rotation_ref": None,
    }
    validate_signed_manifest(manifest, destination_site="site-b", policy_hash=manifest["policy_hash"])
    validate_signed_manifest(json.loads(json.dumps(manifest)), destination_site="site-b", policy_hash=manifest["policy_hash"])
    missing_digest = copy.deepcopy(manifest)
    del missing_digest["image_digest"]
    with pytest.raises(ContractError, match="image_digest"):
        validate_signed_manifest(missing_digest, destination_site="site-b", policy_hash=manifest["policy_hash"])
    with pytest.raises(ContractError, match="destination"):
        validate_signed_manifest(manifest, destination_site="site-c", policy_hash=manifest["policy_hash"])
    parent = {
        "issuer": { "site_id": "site-a", "tenant_id": "tenant-a", "issuer": "issuer-a", "subject": "subject-a" },
        "delegate": { "site_id": "site-a", "tenant_id": "tenant-a", "issuer": "issuer-a", "subject": "subject-b" },
        "audience": "controller-b", "destination_site": "site-b", "allowed_operations": ["acquire", "release"], "allowed_pipelines": ["batch"],
        "resource_ceiling": { "max_s": 600, "max_vram_bytes": 17179869184 }, "expires": "2026-09-27T21:00:00Z", "nonce": "nonce-parent", "max_depth": 2,
        "binding": { "canonicalization": "JCS-RFC8785", "hash_algorithm": "SHA-256", "domain": "flightctl/approval/v1", "signed_fields": ["audience", "expires", "nonce"], "payload_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "audience": "controller-b", "expires": "2026-09-27T21:00:00Z", "nonce": "nonce-parent", "proof": proof, "attenuation": { "max_depth": 2, "allowed_operations": ["acquire", "release"], "resource_ceiling": { "max_s": 600, "max_vram_bytes": 17179869184 } } },
    }
    child = copy.deepcopy(parent)
    child["issuer"] = copy.deepcopy(parent["delegate"])
    child["delegate"]["subject"] = "subject-c"
    child["allowed_operations"] = ["acquire"]
    child["resource_ceiling"]["max_s"] = 300
    child["expires"] = "2026-09-27T20:30:00Z"
    child["max_depth"] = 1
    child["nonce"] = "nonce-child"
    child["binding"]["expires"] = "2026-09-27T20:30:00Z"
    child["binding"]["nonce"] = "nonce-child"
    child["binding"]["attenuation"]["max_depth"] = 1
    child["binding"]["attenuation"]["allowed_operations"] = ["acquire"]
    child["binding"]["attenuation"]["resource_ceiling"]["max_s"] = 300
    validate_delegation_bounds(parent, child)
    cross_site = copy.deepcopy(child)
    cross_site["destination_site"] = "site-c"
    with pytest.raises(ContractError, match="destination"):
        validate_delegation_bounds(parent, cross_site)
    assert "security" not in _load(ROOT / "contracts" / "common.schema.json")["x-examples"]["valid"][0]


def test_state_booking_and_rpc_semantics() -> None:
    for previous, current in (("free", "starting"), ("starting", "running"), ("running", "stopping"), ("stopping", "free"), ("running", "quarantined")):
        validate_state_transition(previous, current)
    with pytest.raises(ContractError):
        validate_state_transition("free", "running")
    validate_booking(_load(ROOT / "contracts" / "booking-v1.schema.json")["x-examples"]["valid"][0])
    validate_booking_transition("scheduled", "blocked", blocked=True)
    validate_booking_transition("blocked", "claimed")
    with pytest.raises(ContractError):
        validate_booking_transition("scheduled", "blocked")
    vector = load_vector("rpc.json")
    request = _rpc_defaults(vector, vector["requests"][0], 900)
    request["args"]["est_s"] = 601
    request["args"]["max_s"] = 600
    with pytest.raises(ContractError, match="estimate"):
        validate_rpc(request)
    no_approval = copy.deepcopy(request)
    no_approval["args"]["est_s"] = 60
    no_approval["admission"]["approval"] = {"approval_id": None, "required": False, "consume_atomically": True}
    validate_rpc(no_approval)
    incomplete_batch = copy.deepcopy(no_approval)
    incomplete_batch["admission"]["batch"] = {"batch_id": "batch-a", "arms": [{"arm_id": "arm-a", "predecessor": "arm-missing", "dependencies": ["arm-missing"]}], "dependencies": ["arm-missing"], "registered_before_execution": True, "all_arms_visible": True}
    with pytest.raises(ContractError, match="registered"):
        validate_rpc(incomplete_batch)
