from __future__ import annotations

import base64
import copy
import hashlib
import json
import struct
import threading
from datetime import datetime, timedelta, timezone

import pytest

from flightctl.auth import (
    APPROVAL_SIGNED_FIELDS,
    ApprovalVerifier,
    AuthError,
    KeyRecord,
    UnsupportedProof,
    approval_digest,
    ed25519_public_key,
    ed25519_sign,
    local_action_digest,
    local_action_projection,
)
from flightctl.authority import Authority, request_fingerprint
from flightctl.store import SQLiteStore, StoreError
from tests.authority.helpers import OTHER_PRINCIPAL, PRINCIPAL, Executor, request
from tests.fakes.clock import FakeClock


def _lanes(*lane_ids: str, reachability: str = "confirmed", quota_key: str | None = None) -> list[dict[str, object]]:
    return [
        {
            "lane_id": lane_id,
            "host_id": f"host-{index}",
            "reachability": reachability,
            "enabled": True,
            **({"quota_key": quota_key} if quota_key is not None else {}),
        }
        for index, lane_id in enumerate(lane_ids, 1)
    ]


def _authority(
    tmp_path,
    *,
    lanes: list[dict[str, object]] | None = None,
    transport=None,
    clock=None,
    identity_mapping=None,
    policy=None,
    approval_verifier=None,
):
    clock = clock or FakeClock(datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc))
    transport = transport or Executor()
    identity_mapping = identity_mapping or [
        {"external_id": "peer-a", "principal": PRINCIPAL, "roles": ["agent"]},
        {"external_id": "peer-b", "principal": OTHER_PRINCIPAL, "roles": ["agent"]},
    ]
    store = SQLiteStore(tmp_path / "state.sqlite")
    authority = Authority(
        store,
        transport,
        clock,
        lanes=lanes or _lanes("lane-gpu0"),
        identity_mapping=identity_mapping,
        policy=policy,
        approval_verifier=approval_verifier,
    )
    return authority, transport, clock


def _ssh_string(value: bytes) -> bytes:
    return struct.pack(">I", len(value)) + value


_SECURITY_KEY_APPLICATION = b"ssh:"


def _security_key_proof(seed: bytes, approval: dict[str, object], *, flags: int = 0x05, counter: int = 1) -> dict[str, str]:
    auth_data = bytes([flags]) + counter.to_bytes(4, "big")
    signature = ed25519_sign(seed, hashlib.sha256(_SECURITY_KEY_APPLICATION).digest() + auth_data + hashlib.sha256(approval_digest(approval)).digest())
    wrapped = _ssh_string(b"sk-ssh-ed25519@openssh.com") + _ssh_string(signature) + auth_data
    return {
        "scheme": "ssh-sk",
        "key_id": "key-a",
        "namespace": "flightctl/approval/v1",
        "encoding": "openssh-ssh-sk-signature/base64",
        "signature_b64": base64.b64encode(wrapped).decode("ascii"),
    }


# Fixed OpenSSL Ed25519 output over the protocol-correct security-key bytes;
# verification has no runtime OpenSSL dependency.
_OPENSSL_VECTOR_PUBLIC_KEY = bytes.fromhex("a87a6368b50137811aacb38ecf8f34739c51f1a6755357324b1796ed1bbd5582")
_OPENSSL_VECTOR_SIGNATURE = bytes.fromhex("77e60658cea49351d7eb5874c8457d5ef8a7f519e3c9d9cc544f61cd7c7e5923ee96e1ff32a872fd875ca5c7750b1a6d7bfdcb68d6738a6bbe9d981d58926b0e")


def _approval_record(*, lane: dict[str, str] | None = None, generation: int | None = None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "id": "approval-test",
        "challenge_id": "challenge-test",
        "challenge_nonce": "nonce-test",
        "action": "forced-preemption",
        "requester": copy.deepcopy(PRINCIPAL),
        "lane": copy.deepcopy(lane),
        "booking_id": None,
        "revision": None,
        "target_generation": generation,
        "bounds": {"max_s": 600, "max_end": "2026-09-28T11:00:00Z"},
        "reason": "test approval",
        "nonce": "nonce-test",
        "expires": "2026-09-28T10:30:00Z",
        "approver": copy.deepcopy(PRINCIPAL),
        "approved_at": "2026-09-28T10:00:00Z",
        "proof": None,
        "verified_evidence": None,
        "consumed_at": None,
        "destination_site": "site-a",
        "controller_id": "controller-a",
        "payload_hash": "a" * 64,
        "manifest_hash": None,
        "policy_hash": "b" * 64,
        "challenge_policy_hash": "b" * 64,
        "challenge_manifest_hash": None,
        "canonicalization": "JCS-RFC8785",
        "canonical_encoding": "UTF-8",
        "hash_algorithm": "SHA-256",
        "domain": "flightctl/approval/v1",
        "signed_fields": [],
        "state": "approved",
    }


def _genuine_approval_verifier() -> ApprovalVerifier:
    return ApprovalVerifier({"key-a": KeyRecord(ed25519_public_key(bytes(range(32))), application=_SECURITY_KEY_APPLICATION)})


def _issue_genuine_approval(authority, request_id: str, *, action: str, peer: str, lane: str | None, booking_id: str | None = None, revision: int | None = None, target_generation: int | None = None, token: str | None = None, max_s: int = 60, max_end: str = "2026-09-28T10:05:00Z") -> dict[str, object]:
    if action == "operator-admission":
        execution = request(f"{request_id}-execution", "acquire", {"purpose": "benchmark", "class": "operator", "est_s": 1, "max_s": 60}, lane="lane-gpu0", admission={"approval": {"approval_id": "approval-placeholder", "required": True, "consume_atomically": True}})
    else:
        execution = request(f"{request_id}-execution", "preempt", {"token": token or "token-abcdefghijklmnop", "approval_id": "approval-placeholder"}, lane="lane-gpu0")
    policy_hash = authority._current_policy_hash()
    payload_hash = local_action_digest(local_action_projection(execution, PRINCIPAL, destination_site=authority.site_id, controller_id=authority.controller_id, policy_hash=policy_hash))
    approval_args = {
        "action": action,
        "booking_id": booking_id,
        "revision": revision,
        "target_generation": target_generation,
        "bounds": {"max_s": max_s, "max_end": max_end},
        "reason": "test approval",
        "destination_site": "site-a",
        "controller_id": "controller-a",
        "payload_hash": payload_hash,
        "manifest_hash": None,
        "policy_hash": policy_hash,
    }
    issued = authority.handle(request(request_id, "approval-request", approval_args, lane=lane), peer=peer)
    assert issued["status"] == 200
    record = issued["data"]["approval"]
    approved = authority.handle(
        request(
            f"{request_id}-approve",
            "approve",
            {
                "approval_id": record["id"],
                "proof": _security_key_proof(bytes(range(32)), record),
                "evidence": {"verifier": "key-a", "verified_at": "2026-09-28T10:00:01Z", "user_presence": "verified", "user_verification": "verified"},
            },
            lane=None,
        ),
        peer=peer,
    )
    assert approved["status"] == 200
    return approved["data"]["approval"]


class _BlockingTransport(Executor):
    def __init__(self, *, block_kind: str):
        super().__init__()
        self.block_kind = block_kind
        self.entered = threading.Event()
        self.release = threading.Event()

    def request(self, endpoint, message, timeout_s):
        if message["kind"] == self.block_kind and not self.entered.is_set():
            self.entered.set()
            if not self.release.wait(2):
                raise AssertionError("bounded executor barrier did not open")
        return super().request(endpoint, message, timeout_s)


class _ReplyMutationTransport(Executor):
    def __init__(self, *, kind: str, mutation):
        super().__init__()
        self.kind = kind
        self.mutation = mutation

    def request(self, endpoint, message, timeout_s):
        raw = super().request(endpoint, message, timeout_s)
        if message["kind"] == self.kind:
            self.mutation(raw["response"], message)
        return raw


class _EchoingFailureTransport:
    def __init__(self):
        self.calls: list[dict[str, object]] = []

    def request(self, endpoint, message, timeout_s):
        self.calls.append({"endpoint": endpoint, "message": dict(message), "timeout_s": timeout_s})
        raise RuntimeError(json.dumps(message, sort_keys=True))


def test_revision2_security_key_proof_binds_touch_and_pin_and_disables_webauthn():
    seed = bytes(range(32))
    approval = _approval_record()
    verifier = ApprovalVerifier({"key-a": KeyRecord(ed25519_public_key(seed), application=_SECURITY_KEY_APPLICATION)})
    proof = _security_key_proof(seed, approval)
    independent = copy.deepcopy(proof)
    independent["signature_b64"] = base64.b64encode(_ssh_string(b"sk-ssh-ed25519@openssh.com") + _ssh_string(_OPENSSL_VECTOR_SIGNATURE) + b"\x05\x00\x00\x00\x07").decode("ascii")
    public_key = _ssh_string(b"sk-ssh-ed25519@openssh.com") + _ssh_string(_OPENSSL_VECTOR_PUBLIC_KEY) + _ssh_string(_SECURITY_KEY_APPLICATION)
    verified = ApprovalVerifier({"key-a": KeyRecord(public_key)}).verify(independent, approval_digest(approval))
    assert verified["user_presence"] == "verified"
    assert verified["user_verification"] == "verified"
    raw = copy.deepcopy(proof)
    raw["signature_b64"] = base64.b64encode(ed25519_sign(seed, approval_digest(approval))).decode("ascii")
    with pytest.raises(AuthError):
        verifier.verify(raw, approval_digest(approval), evidence={"user_presence": "verified", "user_verification": "verified"})
    unknown_key = copy.deepcopy(proof)
    unknown_key["key_id"] = "unregistered"
    with pytest.raises(AuthError):
        verifier.verify(unknown_key, approval_digest(approval))
    for flags in (0, 0x01, 0x04):
        with pytest.raises(AuthError):
            verifier.verify(_security_key_proof(seed, approval, flags=flags), approval_digest(approval))
    with pytest.raises(AuthError):
        verifier.verify(proof, approval_digest(approval), evidence={"verified_at": "2026-09-28T10:00:31Z"}, now=datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc))
    with pytest.raises(UnsupportedProof):
        verifier.verify({"scheme": "webauthn", "key_id": "key-a", "signature_b64": "AA=="}, approval_digest(approval))


def test_revision2_every_signed_approval_field_and_authenticator_byte_is_bound():
    seed = bytes(range(32))
    approval = _approval_record()
    verifier = ApprovalVerifier({"key-a": KeyRecord(ed25519_public_key(seed), application=_SECURITY_KEY_APPLICATION)})
    proof = _security_key_proof(seed, approval)
    for field in APPROVAL_SIGNED_FIELDS:
        mutated = copy.deepcopy(approval)
        value = mutated.get(field)
        if isinstance(value, dict):
            mutated[field] = {**value, "mutated": True}
        elif isinstance(value, int):
            mutated[field] = value + 1
        else:
            mutated[field] = "mutated" if value is None else f"{value}-mutated"
        with pytest.raises(AuthError):
            verifier.verify(proof, approval_digest(mutated))
    tampered = copy.deepcopy(proof)
    encoded = bytearray(base64.b64decode(tampered["signature_b64"]))
    encoded[-1] ^= 1
    tampered["signature_b64"] = base64.b64encode(encoded).decode("ascii")
    with pytest.raises(AuthError):
        verifier.verify(tampered, approval_digest(approval))


def test_revision2_same_request_id_is_owned_before_external_reserve(tmp_path):
    transport = _BlockingTransport(block_kind="reserve")
    authority, _transport, _clock = _authority(tmp_path, lanes=_lanes("lane-a", "lane-b"), transport=transport)
    first = request("same-request", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60}, lane="lane-a")
    second = request("same-request", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60}, lane="lane-b")
    result: dict[str, dict[str, object]] = {}
    thread = threading.Thread(target=lambda: result.setdefault("first", authority.handle(first, peer="peer-a")))
    thread.start()
    assert transport.entered.wait(1)
    result["second"] = authority.handle(second, peer="peer-a")
    transport.release.set()
    thread.join(2)
    assert result["first"]["status"] == 200
    assert result["second"]["status"] == 409
    assert len([call for call in transport.calls if call["message"]["kind"] == "reserve"]) == 1
    assert authority.store.reservation_count() == 1


def test_revision2_idempotency_binds_admission_context_as_well_as_args(tmp_path):
    authority, transport, _clock = _authority(tmp_path)
    original = request(
        "admission-fingerprint",
        "acquire",
        {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60},
        admission={"ingress": {"forwarded_owner": "ignored"}},
    )
    original["idempotency_scope"] = {"scope": "authenticated-principal", "controller_id": "controller-a"}
    original["request_fingerprint"] = request_fingerprint(original, principal=PRINCIPAL)
    assert authority.handle(original, peer="peer-a")["status"] == 200
    assert authority.store.get_idempotency("admission-fingerprint")["idempotency_scope"] == original["idempotency_scope"]
    changed = copy.deepcopy(original)
    changed["admission"]["ingress"]["forwarded_owner"] = "changed"
    changed["request_fingerprint"] = request_fingerprint(changed, principal=PRINCIPAL)
    assert authority.handle(changed, peer="peer-a")["status"] == 409
    assert len(transport.calls) == 1


def test_revision2_approval_is_consumed_before_stop_effect(tmp_path):
    transport = _BlockingTransport(block_kind="stop")
    mapping = [
        {"external_id": "peer-a", "principal": PRINCIPAL, "roles": ["agent"]},
        {"external_id": "peer-op", "principal": PRINCIPAL, "roles": ["operator"]},
    ]
    authority, _transport, _clock = _authority(tmp_path, transport=transport, identity_mapping=mapping, approval_verifier=_genuine_approval_verifier())
    grant = authority.handle(request("acquire-before-preempt", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60}), peer="peer-a")
    assert grant["status"] == 200
    lease = grant["data"]["lease"]
    approval = _issue_genuine_approval(authority, "forced-preemption-approval", action="forced-preemption", peer="peer-op", lane="lane-gpu0", target_generation=lease["generation"], token=grant["data"]["token"], max_end=lease["max_end"])
    preempt = request("preempt-once", "preempt", {"token": grant["data"]["token"], "approval_id": approval["id"]})
    result: dict[str, dict[str, object]] = {}
    thread = threading.Thread(target=lambda: result.setdefault("response", authority.handle(preempt, peer="peer-op")))
    thread.start()
    assert transport.entered.wait(1)
    assert authority.store.get_approval(approval["id"])["state"] == "consumed"
    second = authority.handle(request("preempt-twice", "preempt", {"token": grant["data"]["token"], "approval_id": approval["id"]}), peer="peer-op")
    assert second["status"] == 503
    transport.release.set()
    thread.join(2)
    assert result["response"]["status"] == 200


def test_revision2_clock_freeze_commits_and_survives_clock_restore(tmp_path):
    authority, _transport, clock = _authority(tmp_path)
    grant = authority.handle(request("skew-seed", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60}), peer="peer-a")
    assert grant["status"] == 200
    clock.jump_utc(31)
    frozen = authority.handle(request("after-skew", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60}), peer="peer-a")
    assert frozen["status"] == 503
    assert authority.store.get_lane("lane-gpu0")["state"] == "quarantined"
    assert authority.store.recovery_required()
    clock.jump_utc(-31)
    still_frozen = authority.handle(request("after-restore", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60}), peer="peer-a")
    assert still_frozen["status"] == 503

    reboot_path = tmp_path / "reboot"
    reboot_path.mkdir()
    reboot_authority, _transport, reboot_clock = _authority(reboot_path)
    assert reboot_authority.handle(request("reboot-seed", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60}), peer="peer-a")["status"] == 200
    reboot_clock.reboot()
    rebooted = reboot_authority.handle(request("after-reboot", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60}), peer="peer-a")
    assert rebooted["status"] == 503
    assert reboot_authority.store.get_lane("lane-gpu0")["state"] == "quarantined"


def test_revision2_restart_reconciliation_and_corrupt_store_keep_admission_closed(tmp_path):
    state_path = tmp_path / "pending.sqlite"
    store = SQLiteStore(state_path)
    lane = {
        "lane_id": "lane-gpu0",
        "lane": {"site_id": "site-a", "host_id": "host-1", "lane_id": "lane-gpu0"},
        "host_id": "host-1",
        "endpoint": "host-1",
        "state": "starting",
        "generation": 1,
        "reachability": "confirmed",
        "enabled": True,
        "quota_key": "host-1",
    }
    lease = {
        "schema_version": 1,
        "lease_id": "lease-pending",
        "lane": lane["lane"],
        "generation": 1,
        "reservation": {"lane": lane["lane"], "generation": 1, "state": "starting"},
        "token": "pending-token-abcdefghijklmnop",
        "instance": "instance-pending",
        "principal": PRINCIPAL,
        "class": "batch",
        "purpose": "benchmark",
        "estimated_s": 60,
        "started_at": "2026-09-28T10:00:00Z",
        "max_end": "2026-09-28T11:00:00Z",
        "approved_max_end": "2026-09-28T11:00:00Z",
        "heartbeat_at": "2026-09-28T10:00:00Z",
        "deadline": {"boot_id": "boot-a", "deadline_s": 60, "utc_anchor": "2026-09-28T10:00:00Z", "monotonic_anchor_s": 0},
        "booking_id": None,
        "unit": "unit-pending",
        "invocation": "invoke-pending",
        "state": "starting",
    }
    with store.transaction() as connection:
        store.put_lane(lane, connection=connection)
        store.put_lease(lease, reservation_status="pending", connection=connection)
        crash_request = request("crash-replay", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60})
        store.claim_idempotency(
            crash_request["request_id"],
            store.principal_key(PRINCIPAL),
            {"scope": "authenticated-principal", "controller_id": "controller-a", "in_progress": True},
            crash_request["request_fingerprint"],
            {"schema": 1, "request_id": crash_request["request_id"], "status": 409, "data": None, "error": {"code": "conflict", "message": "request is already in progress", "retryable": True, "failure_class": "conflict"}},
            connection=connection,
        )
    store.close()
    restarted = Authority(
        SQLiteStore(state_path),
        Executor(),
        FakeClock(datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)),
        identity_mapping=[{"external_id": "peer-a", "principal": PRINCIPAL, "roles": ["agent"]}],
        lanes=[],
    )
    assert restarted.store.recovery_required()
    assert restarted.store.get_lane("lane-gpu0")["state"] == "quarantined"
    assert restarted.handle(crash_request, peer="peer-a")["status"] == 503
    assert restarted.handle(request("restart-admission", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60}), peer="peer-a")["status"] == 503

    after_ack_path = tmp_path / "after-ack"
    after_ack_path.mkdir()
    after_ack, after_ack_transport, after_ack_clock = _authority(after_ack_path)
    after_ack_db = after_ack_path / "state.sqlite"
    original_put_event = after_ack.store.put_event

    def crash_after_ack(event, **kwargs):
        if event.get("kind") == "acquire":
            raise StoreError("simulated lost grant commit")
        return original_put_event(event, **kwargs)

    after_ack.store.put_event = crash_after_ack
    after_ack_request = request("after-ack-replay", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60})
    assert after_ack.handle(after_ack_request, peer="peer-a")["status"] == 503
    assert [call["message"]["kind"] for call in after_ack_transport.calls] == ["reserve"]
    after_ack.store.close()
    after_ack_restart = Authority(
        SQLiteStore(after_ack_db),
        Executor(),
        after_ack_clock,
        identity_mapping=[{"external_id": "peer-a", "principal": PRINCIPAL, "roles": ["agent"]}],
        lanes=[],
    )
    assert after_ack_restart.store.get_lane("lane-gpu0")["state"] == "quarantined"
    assert after_ack_restart.handle(after_ack_request, peer="peer-a")["status"] == 503

    corrupt = tmp_path / "corrupt.sqlite"
    corrupt.write_text("not a sqlite database", encoding="utf-8")
    unavailable = Authority(
        SQLiteStore(corrupt),
        Executor(),
        FakeClock(datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)),
        identity_mapping=[{"external_id": "peer-a", "principal": PRINCIPAL, "roles": ["agent"]}],
        lanes=[],
    )
    assert unavailable.handle(request("corrupt-admission", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60}), peer="peer-a")["status"] == 503


def test_revision2_queue_claim_requires_owner_and_all_dependencies(tmp_path):
    authority, transport, _clock = _authority(tmp_path)
    queued = request("queue-owner", "queue", {"action": "add", "purpose": "benchmark", "class": "batch", "max_wait_s": 600})
    assert authority.handle(queued, peer="peer-a")["status"] == 200
    queue_id = authority.store.all_queue()[0]["queue_id"]
    stolen = request("queue-stolen", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60, "queue_id": queue_id}, principal=OTHER_PRINCIPAL)
    assert authority.handle(stolen, peer="peer-b")["status"] in {403, 409}
    assert not transport.calls

    batch = {
        "batch_id": "batch-all-deps",
        "purpose": "benchmark",
        "arms": [
            {"arm_id": "root", "predecessor": None, "dependencies": []},
            {"arm_id": "side", "predecessor": None, "dependencies": []},
            {"arm_id": "successor", "predecessor": "root", "dependencies": ["root", "side"]},
        ],
        "dependencies": ["root", "side"],
        "registered_before_execution": True,
        "all_arms_visible": True,
    }
    authority.register_batch(batch, peer="peer-a", lane="lane-gpu0")
    entries = authority.store.all_queue(lane_id="lane-gpu0")
    for entry in entries:
        if entry["queue_id"].endswith("-root"):
            entry["state"] = "claimed"
            entry["eligible"] = False
        elif entry["queue_id"].endswith("-side"):
            entry["state"] = "expired"
            entry["eligible"] = False
        with authority.store.transaction() as connection:
            authority.store.put_queue(entry, connection=connection)
    successor = next(entry["queue_id"] for entry in entries if entry["queue_id"].endswith("-successor"))
    dependency_bypass = request("dependency-bypass", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60, "queue_id": successor})
    assert authority.handle(dependency_bypass, peer="peer-a")["status"] in {403, 409}
    predecessor_batch = {
        "batch_id": "batch-predecessor-edge",
        "purpose": "benchmark",
        "arms": [
            {"arm_id": "root", "predecessor": None, "dependencies": []},
            {"arm_id": "side", "predecessor": None, "dependencies": []},
            {"arm_id": "successor", "predecessor": "root", "dependencies": ["side"]},
        ],
        "dependencies": ["root", "side"],
        "registered_before_execution": True,
        "all_arms_visible": True,
    }
    authority.register_batch(predecessor_batch, peer="peer-a", lane="lane-gpu0")
    predecessor_entries = authority.store.all_queue(lane_id="lane-gpu0")
    for entry in predecessor_entries:
        if entry["queue_id"] == "batch-predecessor-edge-root":
            entry["state"] = "expired"
            entry["eligible"] = False
        elif entry["queue_id"] == "batch-predecessor-edge-side":
            entry["state"] = "claimed"
            entry["eligible"] = False
        with authority.store.transaction() as connection:
            authority.store.put_queue(entry, connection=connection)
    authority.handle(request("predecessor-edge-refresh", "queue", {"action": "list"}, lane="lane-gpu0"), peer="peer-a")
    edge_successor = authority.store.get_queue("batch-predecessor-edge-successor")
    assert edge_successor["eligible"] is False
    assert edge_successor["predecessor"] == "batch-predecessor-edge-root"
    dangling = copy.deepcopy(batch)
    dangling["batch_id"] = "batch-dangling"
    dangling["arms"] = [{"arm_id": "only", "predecessor": "missing", "dependencies": ["missing"]}]
    with pytest.raises(Exception):
        authority.register_batch(dangling, peer="peer-a", lane="lane-gpu0")
    assert authority.store.get_batch("batch-dangling") is None


def test_revision2_booking_cannot_be_claimed_twenty_five_hours_early(tmp_path):
    authority, transport, _clock = _authority(tmp_path)
    booking = authority.handle(request("future-booking", "book", {"start": "2026-09-29T11:00:00Z", "end": "2026-09-29T11:15:00Z", "purpose": "benchmark"}), peer="peer-a")
    assert booking["status"] == 200
    booking_id = booking["data"]["booking"]["booking_id"]
    early = request("early-claim", "acquire", {"purpose": "benchmark", "class": "booked", "est_s": 60, "max_s": 60, "booking_id": booking_id})
    assert authority.handle(early, peer="peer-a")["status"] == 403
    assert not [call for call in transport.calls if call["message"]["kind"] == "reserve"]


def test_revision2_booking_recovery_and_queue_expiry_are_persisted(tmp_path):
    authority, _transport, clock = _authority(tmp_path)
    booking = authority.handle(request("no-show-booking", "book", {"start": "2026-09-28T10:00:00Z", "end": "2026-09-28T10:15:00Z", "purpose": "benchmark"}), peer="peer-a")
    assert booking["status"] == 200
    booking_id = booking["data"]["booking"]["booking_id"]
    clock.advance(utc_s=901, monotonic_s=901)
    authority.handle(request("trigger-no-show", "queue", {"action": "list"}, lane=None), peer="peer-a")
    no_show = authority.store.get_booking(booking_id)
    assert no_show["state"] == "missed"
    assert no_show["recovery"]["state"] == "no-show-reopened"
    assert any(event["kind"] == "book" and event["state"] == "recovery" and event["data"]["booking_id"] == booking_id for event in authority.store.events())

    queue = authority.handle(request("expiring-queue", "queue", {"action": "add", "purpose": "later", "class": "batch", "max_wait_s": 600}), peer="peer-a")
    assert queue["status"] == 200
    queue_id = authority.store.all_queue()[0]["queue_id"]
    clock.advance(utc_s=60, monotonic_s=60)
    before_refresh = authority.store.get_queue(queue_id)["wait_deadline"]["deadline_s"]
    refreshed = authority.handle(request("refresh-queue", "queue", {"action": "refresh", "purpose": "later", "class": "batch", "max_wait_s": 600, "queue_id": queue_id}), peer="peer-a")
    assert refreshed["status"] == 200
    assert authority.store.get_queue(queue_id)["wait_deadline"]["deadline_s"] > before_refresh
    clock.advance(utc_s=600, monotonic_s=600)
    authority.handle(request("expire-queue", "queue", {"action": "list"}, lane=None), peer="peer-a")
    entry = authority.store.get_queue(queue_id)
    assert entry["state"] == "expired"
    assert entry["eligible"] is False
    assert any(event["kind"] == "queue" and event["state"] == "expired" and event["data"]["queue_id"] == queue_id for event in authority.store.events())


def test_revision2_blocked_checkin_records_attendance_and_end_recovery(tmp_path):
    authority, _transport, clock = _authority(tmp_path)
    occupied = authority.handle(request("occupy-for-booking", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 3600}), peer="peer-a")
    assert occupied["status"] == 200
    booking = authority.handle(request("blocked-booking", "book", {"start": "2026-09-28T10:00:00Z", "end": "2026-09-28T10:15:00Z", "purpose": "benchmark"}), peer="peer-a")
    assert booking["status"] == 200
    record = booking["data"]["booking"]
    clock.advance(utc_s=900, monotonic_s=900)
    claim = request("blocked-checkin", "claim", {"booking_id": record["booking_id"], "revision": record["revision"]})
    checked = authority.handle(claim, peer="peer-a")
    assert checked["status"] == 200
    blocked = authority.store.get_booking(record["booking_id"])
    assert blocked["state"] == "blocked"
    assert blocked["checked_in_at"] is not None
    assert blocked["recovery"]["state"] == "blocked-check-in"
    clock.advance(utc_s=900, monotonic_s=900)
    authority.handle(request("trigger-blocked-end", "queue", {"action": "list"}, lane=None), peer="peer-a")
    missed = authority.store.get_booking(record["booking_id"])
    assert missed["state"] == "missed"
    assert missed["recovery"]["state"] == "overrun-delayed"


def test_revision2_booking_claim_at_minus_fifteen_uses_one_admission(tmp_path):
    authority, transport, clock = _authority(tmp_path)
    booked = authority.handle(request("early-booking", "book", {"start": "2026-09-28T11:00:00Z", "end": "2026-09-28T11:15:00Z", "purpose": "benchmark"}), peer="peer-a")
    assert booked["status"] == 200
    record = booked["data"]["booking"]
    clock.advance(utc_s=2700, monotonic_s=2700)
    claim = authority.handle(request("claim-at-minus-fifteen", "claim", {"booking_id": record["booking_id"], "revision": record["revision"]}), peer="peer-a")
    assert claim["status"] == 200
    assert claim["data"]["lease"]["booking_id"] == record["booking_id"]
    assert [call["message"]["kind"] for call in transport.calls] == ["reserve"]

    end_path = tmp_path / "end"
    end_path.mkdir()
    end_authority, end_transport, end_clock = _authority(end_path)
    ending = end_authority.handle(request("end-booking", "book", {"start": "2026-09-28T10:00:00Z", "end": "2026-09-28T10:15:00Z", "purpose": "benchmark"}), peer="peer-a")
    end_record = ending["data"]["booking"]
    end_clock.advance(utc_s=900, monotonic_s=900)
    at_end = end_authority.handle(request("claim-at-end", "claim", {"booking_id": end_record["booking_id"], "revision": end_record["revision"]}), peer="peer-a")
    assert at_end["status"] == 409
    assert not end_transport.calls

    occupied_path = tmp_path / "occupied-early"
    occupied_path.mkdir()
    occupied_authority, _occupied_transport, occupied_clock = _authority(occupied_path)
    assert occupied_authority.handle(request("occupied-early-seed", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 3600}), peer="peer-a")["status"] == 200
    occupied_booking = occupied_authority.handle(request("occupied-early-booking", "book", {"start": "2026-09-28T11:00:00Z", "end": "2026-09-28T11:15:00Z", "purpose": "benchmark"}), peer="peer-a")
    occupied_record = occupied_booking["data"]["booking"]
    occupied_clock.advance(utc_s=2700, monotonic_s=2700)
    occupied_claim = occupied_authority.handle(request("occupied-early-claim", "claim", {"booking_id": occupied_record["booking_id"], "revision": occupied_record["revision"]}), peer="peer-a")
    assert occupied_claim["status"] == 409


def test_revision2_booking_warning_and_protected_overrun_quarantine(tmp_path):
    warning_path = tmp_path / "warning"
    warning_path.mkdir()
    warning_authority, _transport, warning_clock = _authority(warning_path)
    booked = warning_authority.handle(request("warning-booking", "book", {"start": "2026-09-28T11:00:00Z", "end": "2026-09-28T11:15:00Z", "purpose": "benchmark"}), peer="peer-a")
    assert booked["status"] == 200
    warning_clock.advance(utc_s=3900, monotonic_s=3900)
    warning_authority.handle(request("warning-trigger", "queue", {"action": "list"}, lane=None), peer="peer-a")
    warning_events = [event for event in warning_authority.store.events() if event["kind"] == "book" and event["data"].get("booking_id") == booked["data"]["booking"]["booking_id"]]
    assert any("completion warning" in event["reason"] for event in warning_events)

    overrun_path = tmp_path / "overrun"
    overrun_path.mkdir()
    overrun_authority, transport, overrun_clock = _authority(overrun_path)
    claimed = overrun_authority.handle(request("overrun-booking", "book", {"start": "2026-09-28T10:00:00Z", "end": "2026-09-28T10:15:00Z", "purpose": "benchmark"}), peer="peer-a")
    record = claimed["data"]["booking"]
    claim = overrun_authority.handle(request("overrun-claim", "claim", {"booking_id": record["booking_id"], "revision": record["revision"]}), peer="peer-a")
    assert claim["status"] == 200
    overrun_clock.advance(utc_s=900, monotonic_s=900)
    assert not overrun_authority.enforce_deadlines(peer="peer-a")
    assert overrun_authority.store.get_lane("lane-gpu0")["state"] != "quarantined"
    overrun_clock.advance(utc_s=600, monotonic_s=600)
    assert overrun_authority.enforce_deadlines(peer="peer-a")
    assert overrun_authority.store.get_lane("lane-gpu0")["state"] == "quarantined"
    assert any("completion requested" in event["reason"] for event in overrun_authority.store.events())
    assert [call["message"]["kind"] for call in transport.calls] == ["reserve"]


def test_revision2_future_booking_limit_and_estimate_conflict_are_enforced(tmp_path):
    authority, _transport, _clock = _authority(tmp_path, lanes=_lanes("lane-a", "lane-b"))
    first = authority.handle(request("future-one", "book", {"start": "2026-09-28T11:00:00Z", "end": "2026-09-28T11:15:00Z", "purpose": "benchmark"}, lane="lane-a"), peer="peer-a")
    second = authority.handle(request("future-two", "book", {"start": "2026-09-28T12:00:00Z", "end": "2026-09-28T12:15:00Z", "purpose": "benchmark"}, lane="lane-b"), peer="peer-a")
    assert first["status"] == 200
    assert second["status"] == 200
    third = authority.handle(request("future-three", "book", {"start": "2026-09-28T13:00:00Z", "end": "2026-09-28T13:15:00Z", "purpose": "benchmark"}, lane="lane-a"), peer="peer-a")
    assert third["status"] == 409
    calendar = authority.handle(request("calendar-estimates", "cal", {"at": None}, lane="lane-a"), peer="peer-a")
    assert calendar["status"] == 200
    assert any(window["state"] == "booked" and window["certainty"] == "estimate" for window in calendar["data"]["windows"])
    crossing = authority.handle(request("crossing-estimate", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 7200, "max_s": 7200}, lane="lane-a"), peer="peer-a")
    assert crossing["status"] == 409
    assert crossing["data"] is None

    duration_path = tmp_path / "duration"
    duration_path.mkdir()
    duration_mapping = [
        {"external_id": "peer-a", "principal": PRINCIPAL, "roles": ["agent"]},
        {"external_id": "peer-john", "principal": PRINCIPAL, "roles": ["operator"]},
    ]
    duration_authority, _transport, _clock = _authority(duration_path, lanes=_lanes("lane-duration"), identity_mapping=duration_mapping)
    agent_limit = duration_authority.handle(request("agent-four-hours", "book", {"start": "2026-09-28T11:00:00Z", "end": "2026-09-28T15:00:00Z", "purpose": "benchmark"}, lane="lane-duration"), peer="peer-a")
    assert agent_limit["status"] == 200
    agent_over = duration_authority.handle(request("agent-four-hours-plus-one", "book", {"start": "2026-09-28T16:00:00Z", "end": "2026-09-28T20:00:01Z", "purpose": "benchmark"}, lane="lane-duration"), peer="peer-a")
    assert agent_over["status"] == 403
    john_limit = duration_authority.handle(request("john-twelve-hours", "book", {"start": "2026-09-29T10:00:00Z", "end": "2026-09-29T22:00:00Z", "purpose": "benchmark"}, lane="lane-duration"), peer="peer-john")
    assert john_limit["status"] == 200
    john_over = duration_authority.handle(request("john-twelve-hours-plus-one", "book", {"start": "2026-09-30T10:00:00Z", "end": "2026-09-30T22:00:01Z", "purpose": "benchmark"}, lane="lane-duration"), peer="peer-john")
    assert john_over["status"] == 403


def test_revision2_operator_and_shared_device_quota_are_not_caller_flags(tmp_path):
    mapping = [
        {"external_id": "peer-john", "principal": PRINCIPAL, "roles": ["operator"], "device_id": "device-a"},
        {"external_id": "peer-a", "principal": PRINCIPAL, "roles": ["agent"], "device_id": "device-a"},
        {"external_id": "peer-b", "principal": OTHER_PRINCIPAL, "roles": ["agent"], "device_id": "device-a"},
    ]
    authority, _transport, _clock = _authority(
        tmp_path,
        lanes=_lanes("lane-a", "lane-b", quota_key="device-a"),
        identity_mapping=mapping,
        policy={"quotas": {"device-a": 1}},
    )
    operator_path = tmp_path / "operator-approval"
    operator_path.mkdir()
    operator_authority, _operator_transport, _clock = _authority(operator_path, identity_mapping=mapping, approval_verifier=_genuine_approval_verifier())
    operator_approval = _issue_genuine_approval(operator_authority, "operator-approval-request", action="operator-admission", peer="peer-john", lane=None, max_end="2026-09-28T10:05:00Z")
    signed_operator = request(
        "signed-operator",
        "acquire",
        {"purpose": "benchmark", "class": "operator", "est_s": 1, "max_s": 60},
        admission={"approval": {"approval_id": operator_approval["id"], "required": True, "consume_atomically": True}},
    )
    assert operator_authority.handle(signed_operator, peer="peer-john")["status"] == 200
    assert operator_authority.store.get_approval(operator_approval["id"])["state"] == "consumed"
    operator = request("unsigned-operator", "acquire", {"purpose": "benchmark", "class": "operator", "est_s": 1, "max_s": 60})
    assert authority.handle(operator, peer="peer-john")["status"] == 403
    first = request("quota-first", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60}, lane="lane-a")
    second = request("quota-second", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60}, lane="lane-b", principal=OTHER_PRINCIPAL)
    first_result = authority.handle(first, peer="peer-a")
    assert first_result["status"] == 200
    renew = authority.handle(request("quota-renew", "renew", {"token": first_result["data"]["token"], "extend_s": 1}, lane="lane-a"), peer="peer-a")
    assert renew["status"] == 409
    assert authority.handle(second, peer="peer-b")["status"] == 409


def test_revision2_executor_reserve_and_stop_require_complete_observations(tmp_path):
    reserve_bad = _ReplyMutationTransport(kind="reserve", mutation=lambda response, _message: response.update({"observed_state": "quarantined"}))
    authority, _transport, _clock = _authority(tmp_path, transport=reserve_bad)
    result = authority.handle(request("bad-reserve", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60}), peer="peer-a")
    assert result["status"] == 503
    assert authority.store.get_lane("lane-gpu0")["state"] == "quarantined"

    stop_bad = _ReplyMutationTransport(kind="stop", mutation=lambda response, _message: response["echoed_identity"].update({"unit": "other-unit", "invocation": "other-invocation"}))
    stop_path = tmp_path / "stop-state"
    stop_path.mkdir()
    authority, _transport, _clock = _authority(stop_path, transport=stop_bad)
    grant = authority.handle(request("stop-seed", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60}), peer="peer-a")
    released = authority.handle(request("stop-bad-identity", "release", {"token": grant["data"]["token"]}), peer="peer-a")
    assert released["status"] == 503
    assert authority.store.get_lane("lane-gpu0")["state"] == "quarantined"

    unreachable_path = tmp_path / "unreachable"
    unreachable_path.mkdir()
    unreachable, unreachable_transport, _clock = _authority(unreachable_path, lanes=_lanes("lane-unreachable", reachability="unknown"))
    unavailable = unreachable.handle(request("unreachable-lane", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60}, lane="lane-unreachable"), peer="peer-a")
    assert unavailable["status"] == 503
    assert not unreachable_transport.calls


def test_revision2_eviction_matrix_allows_only_lower_classes_with_bounded_grace(tmp_path):
    mapping = [
        {"external_id": "peer-a", "principal": PRINCIPAL, "roles": ["agent"]},
        {"external_id": "peer-op", "principal": PRINCIPAL, "roles": ["operator"]},
    ]
    transport = Executor()
    authority, _transport, _clock = _authority(tmp_path, transport=transport, identity_mapping=mapping)
    service = authority.handle(request("service-lease", "acquire", {"purpose": "service", "class": "service", "est_s": 1, "max_s": 60}), peer="peer-a")
    assert service["status"] == 200
    evicted = authority.handle(request("evict-service", "preempt", {"token": service["data"]["token"]}), peer="peer-op")
    assert evicted["status"] == 200
    stop = next(call for call in transport.calls if call["message"]["kind"] == "stop")
    assert stop["message"]["execution_policy"]["grace_s"] == 120
    assert stop["message"]["stop_authority"] == {"mode": "controller-match", "approval_id": None}

    protected = authority.handle(request("protected-lease", "acquire", {"purpose": "protected", "class": "batch", "est_s": 1, "max_s": 60}), peer="peer-a")
    assert protected["status"] == 200
    denied = authority.handle(request("protected-without-approval", "preempt", {"token": protected["data"]["token"]}), peer="peer-op")
    assert denied["status"] == 403

    displacement_path = tmp_path / "displacement"
    displacement_path.mkdir()
    displacement_authority, displacement_transport, _clock = _authority(displacement_path, identity_mapping=mapping, approval_verifier=_genuine_approval_verifier())
    booked = displacement_authority.handle(request("displacement-booking", "book", {"start": "2026-09-28T10:00:00Z", "end": "2026-09-28T10:15:00Z", "purpose": "benchmark"}), peer="peer-a")
    assert booked["status"] == 200
    booking = booked["data"]["booking"]
    claimed = displacement_authority.handle(request("displacement-claim", "claim", {"booking_id": booking["booking_id"], "revision": booking["revision"]}), peer="peer-a")
    assert claimed["status"] == 200
    lease = claimed["data"]["lease"]
    current_booking = displacement_authority.store.get_booking(booking["booking_id"])
    unsigned = displacement_authority.handle(request("unsigned-displacement", "preempt", {"token": lease["token"]}), peer="peer-op")
    assert unsigned["status"] == 403
    displacement_approval = _issue_genuine_approval(displacement_authority, "displacement-approval-request", action="displacement", peer="peer-op", lane="lane-gpu0", booking_id=booking["booking_id"], revision=current_booking["revision"], token=lease["token"], max_end=lease["max_end"])
    displaced = displacement_authority.handle(request("approved-displacement", "preempt", {"token": lease["token"], "approval_id": displacement_approval["id"]}), peer="peer-op")
    assert displaced["status"] == 200
    assert displacement_authority.store.get_booking(booking["booking_id"])["state"] == "displaced"
    assert any(event["kind"] == "book" and event["state"] == "displaced" for event in displacement_authority.store.events())
    assert [call["message"]["kind"] for call in displacement_transport.calls] == ["reserve", "stop"]


def test_revision2_transport_errors_and_store_events_never_export_secret_values(tmp_path):
    transport = _EchoingFailureTransport()
    authority, _transport, _clock = _authority(tmp_path, transport=transport)
    result = authority.handle(request("secret-error", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60}), peer="peer-a")
    echoed_token = transport.calls[0]["message"]["identity"]["token"]
    assert echoed_token not in str(result)
    lease = authority.store.leases()[0][0]
    ok, _reply, why = authority._executor_call(authority.store.get_lane("lane-gpu0"), lease, "reserve")
    assert not ok
    assert echoed_token not in why
    report = authority.handle(request("secret-report", "report", {"at": None}, lane=None), peer="peer-a")
    assert echoed_token not in str(report)
    occupancy = authority.handle(request("secret-occupancy", "free", {"at": None}, lane="lane-gpu0"), peer="peer-a")
    assert occupancy["status"] == 200
    assert echoed_token not in str(occupancy)

    store = SQLiteStore(tmp_path / "event.sqlite")
    token_record = {
        "lease_id": "lease-secret",
        "lane": {"site_id": "site-a", "host_id": "host-1", "lane_id": "lane-gpu0"},
        "generation": 1,
        "token": echoed_token,
        "principal": copy.deepcopy(PRINCIPAL),
        "state": "quarantined",
    }
    with store.transaction() as connection:
        store.put_lease(token_record, connection=connection)
    with pytest.raises(StoreError):
        store.put_event({"event_id": "event-secret", "data": {"message": echoed_token}})
    store.connection.execute("INSERT INTO events(event_id, occurred_ts, record_json) VALUES(?,?,?)", ("event-old-secret", 0.0, json.dumps({"event_id": "event-old-secret", "data": {"message": echoed_token}})))
    assert echoed_token not in str(store.events())
    assert echoed_token not in str(store.dump())

    rollback_path = tmp_path / "rollback"
    rollback_path.mkdir()
    rollback_authority, _transport, _clock = _authority(rollback_path)
    before_events = rollback_authority.store.event_count()
    before_requests = rollback_authority.store.idempotency_count()
    rolled_back = rollback_authority.handle(request("rollback-mutation", "queue", {"action": "invalid"}), peer="peer-a")
    assert rolled_back["status"] == 403
    assert rollback_authority.store.event_count() == before_events
    assert rollback_authority.store.idempotency_count() == before_requests


def test_revision2_unknown_reachability_stays_unknown_in_calendar(tmp_path):
    authority, _transport, _clock = _authority(tmp_path, lanes=_lanes("lane-unknown", reachability="unknown"))
    lane = authority.store.get_lane("lane-unknown")
    with authority.store.transaction() as connection:
        authority.store.put_lease(
            {"lease_id": "unknown-lease", "token": "unknown-token-abcdefghijklmnop", "lane": lane["lane"], "generation": 1, "reservation": {"lane": lane["lane"], "generation": 1, "state": "starting"}, "principal": copy.deepcopy(PRINCIPAL), "class": "batch", "purpose": "benchmark", "started_at": "2026-09-28T10:00:00Z", "max_end": "2026-09-28T11:00:00Z", "state": "starting"},
            connection=connection,
        )
    result = authority.handle(request("unknown-calendar", "cal", {"at": None}, lane="lane-unknown"), peer="peer-a")
    assert result["status"] == 200
    assert result["data"]["observation"]["certainty"] == "unknown"
    assert result["data"]["observation"]["reason"]
    occupied = next(window for window in result["data"]["windows"] if window["state"] == "occupied")
    assert occupied["certainty"] == "unknown"
    status = authority.handle(request("unknown-status", "status", {}, lane="lane-unknown"), peer="peer-a")
    assert status["data"]["occupancy"]["certainty"] == "unknown"


def test_revision2_empty_purpose_is_denied_before_transport(tmp_path):
    authority, transport, _clock = _authority(tmp_path)
    result = authority.handle(request("empty-purpose", "acquire", {"purpose": "", "class": "batch", "est_s": 1, "max_s": 60}), peer="peer-a")
    assert result["status"] == 403
    empty_queue = authority.handle(request("empty-queue-purpose", "queue", {"action": "add", "purpose": "", "class": "batch", "max_wait_s": 600}), peer="peer-a")
    assert empty_queue["status"] == 403
    assert not transport.calls
