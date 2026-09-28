"""P0.1 contract vectors exercised through the P1 authority boundary."""

from __future__ import annotations

import copy
import json
from pathlib import Path

from flightctl.auth import local_action_canonical_bytes, local_action_digest, local_action_projection
from flightctl.authority import request_fingerprint
from tests.authority.helpers import PRINCIPAL, Executor, make_authority, request
from tests.authority.test_revision2 import _genuine_approval_verifier, _security_key_proof
from tests.contracts.validation import validate_instance


ROOT = Path(__file__).resolve().parents[2]
VECTOR = json.loads((ROOT / "tests" / "contracts" / "vectors" / "p0-1.json").read_text(encoding="utf-8"))


class _PendingStopExecutor(Executor):
    def __init__(self) -> None:
        super().__init__()
        self.pending = True

    def request(self, endpoint, message, timeout_s):
        if message["kind"] == "stop" and self.pending:
            self.pending = False
            self.calls.append({"endpoint": endpoint, "message": dict(message), "timeout_s": timeout_s})
            return {
                "status": "ok",
                "response": {
                    "kind": "stop",
                    "echoed_identity": dict(message["identity"]),
                    "acknowledgement": "stopped",
                    "ok": False,
                    "observed_state": "stopping",
                    "uncertain": False,
                    "cgroup_occupants": ["occupant-a"],
                    "gpu_tenants": [],
                    "error": "stop accepted; cgroup is still draining",
                },
            }
        return super().request(endpoint, message, timeout_s)


def _pipeline(*, availability: str = "available") -> dict[str, object]:
    return {
        "pipeline_id": "batch",
        "version": "1.0.0",
        "revision": 1,
        "purpose": "batch inference",
        "content_policy": ["acceptable-use"],
        "availability": availability,
        "partner_overrides": {},
        "policy_hash": "a" * 64,
        "updated_at": "2026-09-28T10:00:00Z",
    }


def _vector_request(*, labels: object = ["acceptable-use"], request_id: str = "p01-content") -> dict[str, object]:
    result = copy.deepcopy(VECTOR["content_labels"]["valid"])
    result["request_id"] = request_id
    result["admission"]["content_labels"] = labels
    result["request_fingerprint"] = request_fingerprint(result, principal=PRINCIPAL)
    return result


def test_p01_pending_release_is_durable_and_owner_authorized(tmp_path):
    transport = _PendingStopExecutor()
    authority, _unused, _clock = make_authority(tmp_path, transport=transport)
    grant = authority.handle(request("seed", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60}), peer="peer-a")
    assert grant["status"] == 200
    token = grant["data"]["token"]

    release = request("pending-release", "release", {"token": token})
    pending = authority.handle(release, peer="peer-a")
    assert pending["status"] == 202
    validate_instance(pending, "rpc-envelope-v1.schema.json")
    expected_pending = VECTOR["pending_release"]["valid"]
    assert pending["data"]["kind"] == expected_pending["data"]["kind"]
    assert pending["data"]["operation"] == expected_pending["data"]["operation"]
    assert pending["data"]["queue_id"] == expected_pending["data"]["queue_id"]
    assert pending["data"]["retry_after_s"] == expected_pending["data"]["retry_after_s"]
    assert pending["data"]["reason"] == expected_pending["data"]["reason"]
    assert pending["data"]["retry_after_s"] > 0
    assert pending["data"]["reason"]
    expected_owner = VECTOR["owner_release"]["valid"]
    assert transport.calls[-1]["message"]["stop_authority"] == expected_owner["stop_authority"]
    validate_instance(transport.calls[-1]["message"], "executor-v1.schema.json")

    lease, reservation_status = authority.store.get_lease(token=token)
    assert lease["state"] == "stopping"
    assert lease["reservation"]["state"] == "stopping"
    assert reservation_status == "acknowledged"
    assert authority.store.get_lane("lane-gpu0")["state"] == "stopping"
    stored = authority.store.get_idempotency("pending-release")
    assert stored["status"] == 202
    assert stored["response"] == pending

    assert authority.handle(release, peer="peer-a") == pending
    assert len([call for call in transport.calls if call["message"]["kind"] == "stop"]) == 1

    completed = authority.handle(request("complete-release", "release", {"token": token}), peer="peer-a")
    assert completed["status"] == 200
    assert len([call for call in transport.calls if call["message"]["kind"] == "stop"]) == 2
    assert transport.calls[-1]["message"]["stop_authority"] == expected_owner["stop_authority"]
    assert authority.store.get_lane("lane-gpu0")["state"] == "free"
    lease, reservation_status = authority.store.get_lease(token=token)
    assert lease["reservation"]["state"] == "released"
    assert reservation_status == "released"


def test_p01_content_labels_are_admission_input_and_rechecked(tmp_path):
    valid = _vector_request()
    authority, transport, _clock = make_authority(tmp_path, pipelines=[_pipeline()])
    granted = authority.handle(valid, peer="peer-a")
    assert granted["status"] == 200
    assert len(transport.calls) == 1

    denied_dir = tmp_path / "denied"
    denied_dir.mkdir()
    denied_authority, denied_transport, _clock = make_authority(denied_dir, pipelines=[_pipeline()])
    denied = _vector_request(labels=["disallowed"], request_id="p01-content-denied")
    result = denied_authority.handle(denied, peer="peer-a")
    assert result["status"] == 403
    assert not denied_transport.calls

    invalid_cases = [None, "acceptable-use", ["acceptable-use", "acceptable-use"], [1], [""]]
    invalid_dir = tmp_path / "invalid"
    invalid_dir.mkdir()
    invalid_authority, invalid_transport, _clock = make_authority(invalid_dir, pipelines=[_pipeline()])
    for index, labels in enumerate(invalid_cases):
        invalid = _vector_request(labels=labels, request_id=f"p01-invalid-{index}")
        assert invalid_authority.handle(invalid, peer="peer-a")["status"] == 403
    assert not invalid_transport.calls


def test_p01_deferred_content_labels_are_durable_but_not_public_queue_fields(tmp_path):
    pipeline = _pipeline()
    authority, transport, _clock = make_authority(tmp_path, pipelines=[pipeline])
    binding = {key: pipeline[key] for key in ("pipeline_id", "version", "revision", "purpose", "policy_hash")}
    queue = request(
        "p01-deferred-queue",
        "queue",
        {"action": "add", "purpose": "batch inference", "class": "batch", "max_wait_s": 600},
        admission={"content_labels": ["acceptable-use"], "pipeline": binding},
    )
    assert authority.handle(queue, peer="peer-a")["status"] == 200
    queue_id = authority.store.all_queue()[0]["queue_id"]
    assert "content_labels" not in authority.store.all_queue()[0]

    authority.pipelines["batch"]["content_policy"] = []
    deferred = request(
        "p01-deferred-acquire-denied",
        "acquire",
        {"purpose": "batch inference", "class": "batch", "est_s": 1, "max_s": 60, "pipeline_ref": "batch", "queue_id": queue_id},
        admission={"content_labels": ["acceptable-use"], "pipeline": binding},
    )
    assert authority.handle(deferred, peer="peer-a")["status"] == 403
    assert not transport.calls

    authority.pipelines["batch"]["content_policy"] = ["acceptable-use"]
    deferred["request_id"] = "p01-deferred-acquire"
    deferred["request_fingerprint"] = request_fingerprint(deferred, principal=PRINCIPAL)
    assert authority.handle(deferred, peer="peer-a")["status"] == 200
    assert len(transport.calls) == 1


def _approve_vector_action(authority, request_id: str, payload_hash: str):
    issued = authority.handle(
        request(
            f"{request_id}-challenge",
            "approval-request",
            {
                "action": "pipeline",
                "booking_id": None,
                "revision": 1,
                "target_generation": None,
                "bounds": {"max_s": 600, "max_end": "2026-09-28T10:30:00Z"},
                "reason": "P0.1 vector approval",
                "destination_site": "site-a",
                "controller_id": "controller-a",
                "payload_hash": payload_hash,
                "manifest_hash": None,
                "policy_hash": "a" * 64,
            },
            lane=None,
        ),
        peer="peer-a",
    )
    assert issued["status"] == 200
    approval = issued["data"]["approval"]
    approved = authority.handle(
        request(
            f"{request_id}-approve",
            "approve",
            {
                "approval_id": approval["id"],
                "proof": _security_key_proof(bytes(range(32)), approval),
                "evidence": {"verifier": "key-a", "verified_at": "2026-09-28T10:00:01Z", "user_presence": "verified", "user_verification": "verified"},
            },
        ),
        peer="peer-a",
    )
    assert approved["status"] == 200
    return approval


def test_p01_local_action_vector_and_runtime_recomputation(tmp_path):
    case = VECTOR["local_action"]
    projection = local_action_projection(case["request"], case["projection"]["requester"], destination_site=case["destination_site"], controller_id=case["controller_id"], policy_hash=case["policy_hash"])
    assert projection == case["projection"]
    assert local_action_canonical_bytes(projection).decode("utf-8") == case["canonical_utf8"]
    assert local_action_digest(projection) == case["payload_hash"]

    pipeline = _pipeline(availability="approval_required")
    pipeline["purpose"] = case["request"]["args"]["purpose"]
    authority, transport, _clock = make_authority(tmp_path, pipelines=[pipeline], approval_verifier=_genuine_approval_verifier())
    execution = copy.deepcopy(case["request"])
    approval = _approve_vector_action(authority, "p01-runtime", case["payload_hash"])
    execution["request_id"] = "p01-runtime-execution"
    execution["admission"]["approval"] = {"approval_id": approval["id"], "required": True, "consume_atomically": True}
    execution["request_fingerprint"] = request_fingerprint(execution, principal=PRINCIPAL)
    granted = authority.handle(execution, peer="peer-a")
    assert granted["status"] == 200
    assert authority.store.get_approval(approval["id"])["state"] == "consumed"
    assert len(transport.calls) == 1

    mismatch_dir = tmp_path / "mismatch"
    mismatch_dir.mkdir()
    mismatch_authority, mismatch_transport, _clock = make_authority(mismatch_dir, pipelines=[pipeline], approval_verifier=_genuine_approval_verifier())
    mismatch_approval = _approve_vector_action(mismatch_authority, "p01-mismatch", case["payload_hash"])
    changed = copy.deepcopy(case["request"])
    changed["request_id"] = "p01-mismatch-execution"
    changed["args"]["est_s"] = 61
    changed["admission"]["approval"] = {"approval_id": mismatch_approval["id"], "required": True, "consume_atomically": True}
    changed["request_fingerprint"] = request_fingerprint(changed, principal=PRINCIPAL)
    rejected = mismatch_authority.handle(changed, peer="peer-a")
    assert rejected["status"] == 403
    assert not mismatch_transport.calls
    assert mismatch_authority.store.get_approval(mismatch_approval["id"])["state"] == "approved"
