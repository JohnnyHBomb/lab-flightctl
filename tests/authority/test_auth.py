from __future__ import annotations

import base64
import copy
import hashlib
import struct

import pytest

from flightctl.auth import ApprovalVerifier, AuthError, KeyRecord, approval_digest, approval_projection, canonical_json, ed25519_public_key, ed25519_sign, local_action_digest, local_action_projection
from tests.authority.helpers import PRINCIPAL, make_authority, request


_SECURITY_KEY_APPLICATION = b"ssh:"


def _approval_args():
    return {"action": "pipeline", "booking_id": None, "revision": 1, "target_generation": None, "bounds": {"max_s": 60, "max_end": "2026-09-28T10:05:00Z"}, "reason": "pipeline approval", "destination_site": "site-a", "controller_id": "controller-a", "payload_hash": "a" * 64, "manifest_hash": None, "policy_hash": "b" * 64}


def _ssh_sk_proof(seed, approval, *, flags=0x05, counter=1):
    auth_data = bytes([flags]) + counter.to_bytes(4, "big")
    signature = ed25519_sign(seed, hashlib.sha256(_SECURITY_KEY_APPLICATION).digest() + auth_data + hashlib.sha256(approval_digest(approval)).digest())
    wrapped = struct.pack(">I", len(b"sk-ssh-ed25519@openssh.com")) + b"sk-ssh-ed25519@openssh.com"
    wrapped += struct.pack(">I", len(signature)) + signature + auth_data
    return {"scheme": "ssh-sk", "key_id": "key-a", "namespace": "flightctl/approval/v1", "encoding": "openssh-ssh-sk-signature/base64", "signature_b64": base64.b64encode(wrapped).decode("ascii")}


def test_jcs_fixed_order_unicode_numbers_and_nulls():
    assert canonical_json({"é": 1, "a": None, "😀": 0.000001}) == '{"a":null,"é":1,"😀":0.000001}'
    assert canonical_json(1.0) == "1"
    assert canonical_json(-0.0) == "0"
    assert canonical_json(1e-7) == "1e-7"
    assert canonical_json(1e20) == "100000000000000000000"


def test_approval_proof_binding(tmp_path):
    seed = bytes(range(32))
    verifier = ApprovalVerifier({"key-a": KeyRecord(ed25519_public_key(seed), evidence={"verifier": "key-a", "user_presence": "verified", "user_verification": "verified"}, application=_SECURITY_KEY_APPLICATION)})
    pipelines = [{"pipeline_id": "interactive", "version": "1.0.0", "revision": 1, "purpose": "interactive inference", "content_policy": ["acceptable-use"], "availability": "approval_required", "partner_overrides": {}, "policy_hash": "b" * 64, "updated_at": "2026-09-28T10:00:00Z"}]
    authority, _transport, _clock = make_authority(tmp_path, pipelines=pipelines, approval_verifier=verifier)
    acquire_args = {"purpose": "interactive inference", "class": "batch", "est_s": 30, "max_s": 60, "pipeline_ref": "interactive"}
    acquire_admission = {"pipeline": {"pipeline_id": "interactive", "version": "1.0.0", "revision": 1, "purpose": "interactive inference", "policy_hash": "b" * 64}, "approval": {"approval_id": "approval-placeholder", "required": True, "consume_atomically": True}}
    acquire_request = request("req-approved-acquire", "acquire", acquire_args, admission=acquire_admission)
    approval_args = _approval_args()
    approval_args["payload_hash"] = local_action_digest(local_action_projection(acquire_request, PRINCIPAL, destination_site="site-a", controller_id="controller-a", policy_hash="b" * 64))
    approval_request = request("req-challenge", "approval-request", approval_args, lane="lane-gpu0")
    issued = authority.handle(approval_request, peer="peer-a")
    assert issued["status"] == 200
    record = issued["data"]["approval"]
    proof = _ssh_sk_proof(seed, record)
    # Exercise rejection while the challenge is still issued: lifecycle
    # rejection after approval cannot demonstrate signature verification.
    tampered = copy.deepcopy(proof)
    blob = bytearray(base64.b64decode(tampered["signature_b64"]))
    blob[35] ^= 1
    tampered["signature_b64"] = base64.b64encode(blob).decode("ascii")
    invalid = request("req-tampered-proof", "approve", {"approval_id": record["id"], "proof": tampered, "evidence": {"verifier": "key-a", "verified_at": "2026-09-28T10:00:00Z", "user_presence": "verified", "user_verification": "verified"}})
    assert authority.handle(invalid, peer="peer-a")["status"] == 403
    assert authority.store.get_approval(record["id"])["state"] == "issued"
    assert not _transport.calls
    approve = request("req-approve", "approve", {"approval_id": record["id"], "proof": proof, "evidence": {"verifier": "key-a", "verified_at": "2026-09-28T10:00:01Z", "user_presence": "verified", "user_verification": "verified"}})
    approved = authority.handle(approve, peer="peer-a")
    assert approved["status"] == 200
    mutate = copy.deepcopy(proof)
    mutate["signature_b64"] = "!"
    invalid = request("req-invalid-proof", "approve", {"approval_id": record["id"], "proof": mutate, "evidence": approve["args"]["evidence"]})
    assert authority.handle(invalid, peer="peer-a")["status"] == 403
    acquire = request("req-approved-acquire", "acquire", acquire_args, admission={"pipeline": {"pipeline_id": "interactive", "version": "1.0.0", "revision": 1, "purpose": "interactive inference", "policy_hash": "b" * 64}, "approval": {"approval_id": record["id"], "required": True, "consume_atomically": True}})
    assert authority.handle(acquire, peer="peer-a")["status"] == 200
    replay = request("req-approved-acquire-2", "acquire", {"purpose": "interactive inference", "class": "batch", "est_s": 30, "max_s": 60, "pipeline_ref": "interactive"}, admission={"pipeline": {"pipeline_id": "interactive", "version": "1.0.0", "revision": 1, "purpose": "interactive inference", "policy_hash": "b" * 64}, "approval": {"approval_id": record["id"], "required": True, "consume_atomically": True}})
    assert authority.handle(replay, peer="peer-a")["status"] in {403, 409}


def test_approval_signature_verification_and_application_binding_are_required(monkeypatch):
    seed = bytes(range(32))
    approval = {"id": "approval", "action": "pipeline", "requester": PRINCIPAL, "lane": None, "booking_id": None, "revision": 1, "target_generation": None, "bounds": {"max_s": 60, "max_end": "2026-09-28T10:05:00Z"}, "reason": "reason", "nonce": "nonce", "expires": "2026-09-28T10:04:00Z", "destination_site": "site-a", "controller_id": "controller-a", "payload_hash": "a" * 64, "manifest_hash": None, "policy_hash": "b" * 64, "challenge_id": "challenge", "challenge_nonce": "challenge-nonce"}
    proof = _ssh_sk_proof(seed, approval)
    verifier = ApprovalVerifier({"key-a": KeyRecord(ed25519_public_key(seed), application=_SECURITY_KEY_APPLICATION)})
    with monkeypatch.context() as patched:
        patched.setattr("flightctl.auth.ed25519_verify", lambda *_args: False)
        with pytest.raises(AuthError):
            verifier.verify(proof, approval_digest(approval))

    assert verifier.verify(proof, approval_digest(approval))["user_presence"] == "verified"
    wrong_application = ApprovalVerifier({"key-a": KeyRecord(ed25519_public_key(seed), application=b"https://wrong.example")})
    with pytest.raises(AuthError):
        wrong_application.verify(proof, approval_digest(approval))
