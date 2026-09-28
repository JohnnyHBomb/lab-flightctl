"""Reviewer regressions against production authority and persisted state."""

import copy
import base64
import hashlib
import json
import struct
from pathlib import Path

import pytest

from flightctl.authority import request_fingerprint
from flightctl.auth import canonical_bytes, ed25519_sign
from flightctl.store import StoreError
from tests.authority.helpers import PRINCIPAL, Executor, make_authority, request
from tests.contracts.validation import ContractError, validate_instance, validate_rpc
from tests.authority.test_revision2 import _genuine_approval_verifier, _security_key_proof
from tests.authority.test_auth import _approval_args


ROOT = Path(__file__).resolve().parents[2]
PIPELINE = {"pipeline_id": "interactive", "version": "1.0.0", "revision": 1,
            "purpose": "interactive inference", "availability": "available",
            "content_policy": ["acceptable-use"], "partner_overrides": {},
            "policy_hash": "b" * 64}


def chat_request(request_id="load"):
    return request(request_id, "chat-load", {"pipeline_ref": "interactive", "purpose": PIPELINE["purpose"]},
                   admission={"pipeline": {key: PIPELINE[key] for key in ("pipeline_id", "version", "revision", "purpose", "policy_hash")}})


def test_chat_load_replay_has_one_occupant_and_identical_result(tmp_path):
    authority, transport, clock = make_authority(tmp_path, pipelines=[PIPELINE])
    first = authority.handle(chat_request(), peer="peer-a")
    assert first["status"] == 200
    authority.store.close()
    restarted, _, _ = make_authority(tmp_path, pipelines=[PIPELINE], transport=transport, clock=clock)
    assert restarted.handle(chat_request(), peer="peer-a") == first
    assert len(restarted.store.all_occupants()) == 1
    assert [call["message"]["kind"] for call in transport.calls] == ["reserve"]


def test_chat_load_commit_failure_does_not_publish_a_grant(tmp_path, monkeypatch):
    authority, transport, clock = make_authority(tmp_path, pipelines=[PIPELINE])

    def fail(*_args, **_kwargs):
        raise StoreError("injected occupant write failure")

    monkeypatch.setattr(authority.store, "put_occupant", fail)
    assert authority.handle(chat_request(), peer="peer-a")["status"] == 503
    assert authority.store.get_idempotency("load")["status"] != 200
    authority.store.close()
    restarted, _, _ = make_authority(tmp_path, pipelines=[PIPELINE], transport=transport, clock=clock)
    assert restarted.store.get_lane("lane-gpu0")["state"] == "quarantined"
    assert restarted.handle(chat_request(), peer="peer-a")["status"] == 503
    assert not restarted.store.all_occupants()
    assert len(transport.calls) == 1


def test_chat_unload_cannot_free_a_successor_generation(tmp_path):
    authority, transport, _ = make_authority(tmp_path, pipelines=[PIPELINE])
    loaded = authority.handle(chat_request(), peer="peer-a")
    unload_args = {"occupant_id": loaded["data"]["record_id"], "generation": 1}
    assert authority.handle(request("unload", "chat-unload", unload_args), peer="peer-a")["status"] == 200
    successor = authority.handle(request("next", "acquire", {"purpose": "next", "class": "batch", "est_s": 1, "max_s": 60}), peer="peer-a")
    assert successor["status"] == 200
    before = copy.deepcopy(authority.store.get_lane("lane-gpu0"))
    calls = len(transport.calls)
    assert authority.handle(request("stale-unload", "chat-unload", unload_args), peer="peer-a")["status"] in {403, 409}
    assert authority.store.get_lane("lane-gpu0") == before
    assert len(transport.calls) == calls


def test_uncertain_chat_unload_quarantines_lease_and_lane(tmp_path):
    authority, transport, _ = make_authority(tmp_path, pipelines=[PIPELINE], transport=Executor(stop="lost"))
    loaded = authority.handle(chat_request(), peer="peer-a")
    result = authority.handle(request("unload", "chat-unload", {"occupant_id": loaded["data"]["record_id"], "generation": 1}), peer="peer-a")
    assert result["status"] == 503
    lease, status = authority.store.leases()[0]
    assert (lease["state"], status, authority.store.get_lane("lane-gpu0")["state"]) == ("quarantined", "uncertain", "quarantined")
    assert [call["message"]["kind"] for call in transport.calls] == ["reserve", "stop"]


def test_booked_acquire_without_lane_uses_its_booking(tmp_path):
    authority, transport, _ = make_authority(tmp_path)
    booked = authority.handle(request("book", "book", {"purpose": "benchmark", "start": "2026-09-28T10:00:00Z", "end": "2026-09-28T10:15:00Z"}), peer="peer-a")
    result = authority.handle(request("acquire", "acquire", {"purpose": "benchmark", "class": "booked", "est_s": 1, "max_s": 36000,
                              "booking_id": booked["data"]["booking"]["booking_id"]}, lane=None), peer="peer-a")
    assert result["status"] == 200
    assert result["data"]["lease"]["max_end"] == "2026-09-28T10:15:00Z"
    assert len(transport.calls) == 1


def test_queue_refresh_cannot_revive_an_unobserved_expiry(tmp_path):
    authority, transport, clock = make_authority(tmp_path)
    authority.handle(request("queue", "queue", {"action": "add", "class": "batch", "purpose": "benchmark", "max_wait_s": 3600}), peer="peer-a")
    queue_id = authority.store.all_queue()[0]["queue_id"]
    clock.advance(utc_s=601, monotonic_s=601)
    result = authority.handle(request("refresh", "queue", {"action": "refresh", "queue_id": queue_id, "max_wait_s": 3600}), peer="peer-a")
    assert result["status"] == 409
    assert authority.store.get_queue(queue_id)["state"] == "expired"
    assert not transport.calls


def test_retired_lease_is_not_projected_over_its_successor(tmp_path, monkeypatch):
    authority, _, _ = make_authority(tmp_path)
    first = authority.handle(request("first", "acquire", {"purpose": "old", "class": "batch", "est_s": 1, "max_s": 60}), peer="peer-a")
    authority.handle(request("release", "release", {"token": first["data"]["token"]}), peer="peer-a")
    second = authority.handle(request("second", "acquire", {"purpose": "new", "class": "batch", "est_s": 1, "max_s": 120}), peer="peer-a")
    rows = authority.store.leases()
    # Storage order is by random lease ID; make the retired row sort last.
    monkeypatch.setattr(authority.store, "leases", lambda **_: sorted(rows, key=lambda row: row[1] == "released"))
    projection = authority.handle(request("cal", "cal", {}), peer="peer-a")
    assert projection["data"]["windows"][0]["end"] == second["data"]["lease"]["max_end"]


def test_protected_voluntary_release_uses_owner_authority(tmp_path):
    """A matching owner's release is distinct from forced preemption."""
    authority, transport, _ = make_authority(tmp_path)
    grant = authority.handle(request("first", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60}), peer="peer-a")
    assert authority.handle(request("release", "release", {"token": grant["data"]["token"]}), peer="peer-a")["status"] == 200
    validate_instance(transport.calls[0]["message"], "executor-v1.schema.json")
    stop = transport.calls[1]["message"]
    assert stop["stop_authority"] == {"mode": "owner-release", "approval_id": None}
    validate_instance(stop, "executor-v1.schema.json")


def test_frozen_rpc_vectors_against_production_handlers(tmp_path):
    vector = json.loads((ROOT / "tests/contracts/vectors/rpc.json").read_text())
    authority, transport, _ = make_authority(tmp_path, pipelines=[PIPELINE], approval_verifier=_genuine_approval_verifier())
    visited = set()

    def send(index, *, overrides=None, expected=200):
        payload = copy.deepcopy(vector["request_defaults"])
        payload.update(copy.deepcopy(vector["requests"][index]))
        payload["args"].update(overrides or {})
        if payload["op"] in {"approve", "preempt"}:
            payload["admission"]["approval"] = {"approval_id": payload["args"]["approval_id"], "required": True, "consume_atomically": True}
        if payload["args"].get("pipeline_ref"):
            payload["admission"]["pipeline"] = {key: PIPELINE[key] for key in ("pipeline_id", "version", "revision", "purpose", "policy_hash")}
        payload["request_fingerprint"] = request_fingerprint(payload, principal=PRINCIPAL)
        validate_rpc(payload)
        result = authority.handle(payload, peer="peer-a")
        assert result["status"] == expected, (index, result)
        if payload["op"] == "report":
            for event in result["data"]["events"]:
                validate_instance(event, "event-v1.schema.json")
        validate_instance(result, "rpc-envelope-v1.schema.json")
        visited.add(index)
        return result["data"]

    send(4)  # add
    queue_id = authority.store.all_queue()[0]["queue_id"]
    assert send(7)["entries"][0]["queue_id"] == queue_id
    send(5, overrides={"queue_id": queue_id})
    send(6, overrides={"queue_id": queue_id})
    booking = send(8)["booking"]
    grant = send(1, overrides={"booking_id": booking["booking_id"]})
    send(3, overrides={"token": grant["token"]}, expected=409)  # already at booking bound
    send(2, overrides={"token": grant["token"]})
    send(9, overrides={"booking_id": booking["booking_id"], "revision": 2})
    fresh = send(0)
    send(12, overrides={"token": fresh["token"]}, expected=403)  # no signed preemption
    assert authority.handle(request("cleanup", "release", {"token": fresh["token"]}), peer="peer-a")["status"] == 200
    approval = send(10)["approval"]
    send(11, overrides={"approval_id": approval["id"], "proof": _security_key_proof(bytes(range(32)), approval),
                       "evidence": {"verifier": "key-a", "verified_at": "2026-09-28T10:00:00Z", "user_presence": "verified", "user_verification": "verified"}})
    loaded = send(13)
    send(14, overrides={"occupant_id": loaded["record_id"], "generation": loaded["reservation"]["generation"]})
    send(15)
    send(16)
    send(17)
    send(18)
    assert visited == set(range(19))
    assert [call["message"]["kind"] for call in transport.calls] == ["reserve", "stop"] * 3
    for event in authority.store.events():
        validate_instance(event, "event-v1.schema.json")
    for payload in vector["negative_requests"]:
        payload = copy.deepcopy(payload)
        payload["request_fingerprint"] = request_fingerprint(payload, principal=PRINCIPAL)
        calls = len(transport.calls)
        assert authority.handle(payload, peer="peer-a")["status"] == 403
        assert len(transport.calls) == calls


@pytest.mark.parametrize("case", json.loads((ROOT / "tests/contracts/vectors/policy.json").read_text())["decision_vectors"], ids=lambda case: case["name"])
def test_frozen_policy_decisions_against_production(tmp_path, case):
    config = json.loads((ROOT / "config/pipelines.json.example").read_text())
    aliases = {"available": "batch", "unavailable": "retired", "approval": "interactive"}
    pipeline = next(item for item in config["pipelines"] if item["pipeline_id"] == aliases[case["pipeline_id"]])
    # Destination is controller configuration, never a caller authority flag.
    pipeline = copy.deepcopy(pipeline)
    if case["destination_site"] in pipeline.get("partner_overrides", {}):
        pipeline["partner_overrides"]["site-a"] = pipeline["partner_overrides"][case["destination_site"]]
    authority, transport, _ = make_authority(tmp_path, pipelines=[pipeline])
    args = {"purpose": case["purpose"], "class": "batch", "est_s": 1, "max_s": 60, "pipeline_ref": pipeline["pipeline_id"]}
    if case["name"] == "rejected content":
        # P0 has this decision input but no legal RPC field for it.  Reject
        # the unsupported field; do not invent a schema extension in P1.
        args["content_labels"] = case["content_labels"]
    binding = {key: pipeline[key] for key in ("pipeline_id", "version", "revision", "purpose", "policy_hash")}
    binding["policy_hash"] = case.get("policy_hash", binding["policy_hash"])
    result = authority.handle(request("policy", "acquire", args, admission={"pipeline": binding}), peer="peer-a")
    assert (result["status"] == 200) is case["expected"]
    assert len(transport.calls) == int(case["expected"])
    assert authority.store.reservation_count("lane-gpu0") == int(case["expected"])


def _signed_hook(payload):
    message = hashlib.sha256(canonical_bytes(payload)).digest()
    auth_data = b"\x05\x00\x00\x00\x01"
    signature = ed25519_sign(bytes(range(32)), hashlib.sha256(b"ssh:").digest() + auth_data + hashlib.sha256(message).digest())
    algorithm = b"sk-ssh-ed25519@openssh.com"
    blob = struct.pack(">I", len(algorithm)) + algorithm + struct.pack(">I", 64) + signature + auth_data
    return {"scheme": "ssh-sk", "key_id": "key-a", "namespace": "flightctl/approval/v1", "encoding": "openssh-ssh-sk-signature/base64", "signature_b64": base64.b64encode(blob).decode()}


@pytest.mark.parametrize("hook", ["manifest", "delegation"])
def test_unsupported_hooks_do_not_gain_authority_from_a_signature(tmp_path, hook):
    authority, transport, _ = make_authority(tmp_path, pipelines=[PIPELINE], approval_verifier=_genuine_approval_verifier())
    args = {"purpose": PIPELINE["purpose"], "class": "batch", "est_s": 1, "max_s": 60, "pipeline_ref": "interactive"}
    admission = {"pipeline": {key: PIPELINE[key] for key in ("pipeline_id", "version", "revision", "purpose", "policy_hash")}}
    if hook == "manifest":
        payload = {"destination_site": "site-a", "principal": PRINCIPAL, "expires": "2026-09-28T11:00:00Z",
                   "pipeline_id": "interactive", "policy_hash": PIPELINE["policy_hash"], "resource_ceiling": {"max_s": 1}}
        args["signed_manifest"] = dict(payload, canonical_payload_hash=hashlib.sha256(canonical_bytes(payload)).hexdigest(), proof=_signed_hook(payload))
    else:
        binding = {"audience": "controller-a", "nonce": "delegation-nonce"}
        admission["delegation"] = {"delegate": PRINCIPAL, "destination_site": "site-a", "audience": "controller-a",
                                   "allowed_operations": ["acquire"], "allowed_pipelines": ["interactive"],
                                   "expires": "2026-09-28T11:00:00Z", "max_depth": 0, "nonce": "delegation-nonce",
                                   "resource_ceiling": {"max_s": 1}, "binding": dict(binding, proof=_signed_hook(binding))}
    result = authority.handle(request("hook", "acquire", args, admission=admission), peer="peer-a")
    assert result["status"] == 403
    assert not transport.calls
    assert not authority.store.leases()


def test_queue_dependency_waits_for_confirmed_completion(tmp_path):
    authority, transport, _ = make_authority(tmp_path)
    authority.register_batch({"batch_id": "batch", "purpose": "benchmark", "registered_before_execution": True, "all_arms_visible": True,
                              "arms": [{"arm_id": "first", "predecessor": None, "dependencies": []},
                                       {"arm_id": "next", "predecessor": "first", "dependencies": ["first"]}]}, peer="peer-a", lane="lane-gpu0")
    grant = authority.handle(request("first", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60, "queue_id": "batch-first"}), peer="peer-a")
    assert grant["status"] == 200
    listed = authority.handle(request("list", "queue", {"action": "list"}), peer="peer-a")
    assert next(item for item in listed["data"]["entries"] if item["queue_id"] == "batch-next")["eligible"] is False
    assert authority.handle(request("release", "release", {"token": grant["data"]["token"]}), peer="peer-a")["status"] == 200
    listed = authority.handle(request("list-again", "queue", {"action": "list"}), peer="peer-a")
    assert next(item for item in listed["data"]["entries"] if item["queue_id"] == "batch-next")["eligible"] is True
    successor = authority.handle(request("next", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60, "queue_id": "batch-next"}), peer="peer-a")
    assert successor["status"] == 200
    assert [call["message"]["kind"] for call in transport.calls] == ["reserve", "stop", "reserve"]


@pytest.mark.parametrize("field,value", [("manifest_hash", "c" * 64), ("booking_id", "other-booking")])
def test_pipeline_approval_cannot_discard_a_signed_optional_target(tmp_path, field, value):
    pipeline = dict(PIPELINE, availability="approval_required")
    authority, transport, _ = make_authority(tmp_path, pipelines=[pipeline], approval_verifier=_genuine_approval_verifier())
    args = _approval_args()
    args[field] = value
    issued = authority.handle(request("issue", "approval-request", args), peer="peer-a")
    approval = issued["data"]["approval"]
    signed = authority.handle(request("approve", "approve", {"approval_id": approval["id"], "proof": _security_key_proof(bytes(range(32)), approval),
                                                              "evidence": {"verifier": "key-a", "verified_at": "2026-09-28T10:00:00Z", "user_presence": "verified", "user_verification": "verified"}}), peer="peer-a")
    assert signed["status"] == 200
    admission = {"pipeline": {key: pipeline[key] for key in ("pipeline_id", "version", "revision", "purpose", "policy_hash")},
                 "approval": {"approval_id": approval["id"], "required": True, "consume_atomically": True}}
    result = authority.handle(request("consume", "acquire", {"purpose": pipeline["purpose"], "class": "batch", "est_s": 1, "max_s": 60, "pipeline_ref": "interactive"}, admission=admission), peer="peer-a")
    assert result["status"] == 403
    assert authority.store.get_approval(approval["id"])["state"] == "approved"
    assert not transport.calls
