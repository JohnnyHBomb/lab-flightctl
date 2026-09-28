from __future__ import annotations

import base64
import copy
import hashlib
import struct

import pytest

from flightctl.auth import ApprovalVerifier, AuthError, KeyRecord, approval_digest, approval_projection, canonical_json, ed25519_public_key, ed25519_sign
from tests.authority.helpers import PRINCIPAL, make_authority, request


def _approval_args():
    return {"action": "pipeline", "booking_id": None, "revision": 1, "target_generation": None, "bounds": {"max_s": 60, "max_end": "2026-09-28T10:05:00Z"}, "reason": "pipeline approval", "destination_site": "site-a", "controller_id": "controller-a", "payload_hash": "a" * 64, "manifest_hash": None, "policy_hash": "b" * 64}


def _ssh_sk_proof(seed, approval, *, flags=0x05, counter=1):
    auth_data = bytes([flags]) + counter.to_bytes(4, "big")
    signature = ed25519_sign(seed, auth_data + hashlib.sha256(approval_digest(approval)).digest())
    wrapped = struct.pack(">I", len(b"sk-ssh-ed25519@openssh.com")) + b"sk-ssh-ed25519@openssh.com"
    wrapped += struct.pack(">I", len(signature + auth_data)) + signature + auth_data
    return {"scheme": "ssh-sk", "key_id": "key-a", "namespace": "flightctl/approval/v1", "encoding": "openssh-ssh-sk-signature/base64", "signature_b64": base64.b64encode(wrapped).decode("ascii")}


def test_jcs_fixed_order_unicode_numbers_and_nulls():
    assert canonical_json({"é": 1, "a": None, "😀": 0.000001}) == '{"a":null,"é":1,"😀":0.000001}'
    assert canonical_json(1.0) == "1"
    assert canonical_json(-0.0) == "0"
    assert canonical_json(1e-7) == "1e-7"
    assert canonical_json(1e20) == "100000000000000000000"


def test_approval_proof_binding(tmp_path):
    seed = bytes(range(32))
    verifier = ApprovalVerifier({"key-a": KeyRecord(ed25519_public_key(seed), evidence={"verifier": "key-a", "user_presence": "verified", "user_verification": "verified"})})
    pipelines = [{"pipeline_id": "interactive", "version": "1.0.0", "revision": 1, "purpose": "interactive inference", "content_policy": ["acceptable-use"], "availability": "approval_required", "partner_overrides": {}, "policy_hash": "b" * 64, "updated_at": "2026-09-28T10:00:00Z"}]
    authority, _transport, _clock = make_authority(tmp_path, pipelines=pipelines, approval_verifier=verifier)
    approval_request = request("req-challenge", "approval-request", _approval_args(), lane="lane-gpu0")
    issued = authority.handle(approval_request, peer="peer-a")
    assert issued["status"] == 200
    record = issued["data"]["approval"]
    proof = _ssh_sk_proof(seed, record)
    approve = request("req-approve", "approve", {"approval_id": record["id"], "proof": proof, "evidence": {"verifier": "key-a", "verified_at": "2026-09-28T10:00:01Z", "user_presence": "verified", "user_verification": "verified"}})
    approved = authority.handle(approve, peer="peer-a")
    assert approved["status"] == 200
    mutate = copy.deepcopy(proof)
    mutate["signature_b64"] = "!"
    invalid = request("req-invalid-proof", "approve", {"approval_id": record["id"], "proof": mutate, "evidence": approve["args"]["evidence"]})
    assert authority.handle(invalid, peer="peer-a")["status"] == 403
    acquire = request("req-approved-acquire", "acquire", {"purpose": "interactive inference", "class": "batch", "est_s": 30, "max_s": 60, "pipeline_ref": "interactive"}, admission={"pipeline": {"pipeline_id": "interactive", "version": "1.0.0", "revision": 1, "purpose": "interactive inference", "policy_hash": "b" * 64}, "approval": {"approval_id": record["id"], "required": True, "consume_atomically": True}})
    assert authority.handle(acquire, peer="peer-a")["status"] == 200
    replay = request("req-approved-acquire-2", "acquire", {"purpose": "interactive inference", "class": "batch", "est_s": 30, "max_s": 60, "pipeline_ref": "interactive"}, admission={"pipeline": {"pipeline_id": "interactive", "version": "1.0.0", "revision": 1, "purpose": "interactive inference", "policy_hash": "b" * 64}, "approval": {"approval_id": record["id"], "required": True, "consume_atomically": True}})
    assert authority.handle(replay, peer="peer-a")["status"] in {403, 409}
