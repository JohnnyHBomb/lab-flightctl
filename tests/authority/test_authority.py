from __future__ import annotations

import copy
import threading
import time

import pytest

from flightctl.authority import request_fingerprint
from flightctl.store import SQLiteStore
from tests.authority.helpers import OTHER_PRINCIPAL, PRINCIPAL, Executor, make_authority, request


def test_atomic_admission(tmp_path):
    authority, transport, _clock = make_authority(tmp_path)
    first_entered = threading.Event()
    allow_first = threading.Event()
    original = transport.request

    def slow(endpoint, message, timeout_s):
        first_entered.set()
        allow_first.wait(2)
        return original(endpoint, message, timeout_s)

    transport.request = slow
    first = request("req-1", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 60, "max_s": 600})
    second = request("req-2", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 60, "max_s": 600})
    result = {}

    def run():
        result["first"] = authority.handle(first, peer="peer-a")

    thread = threading.Thread(target=run)
    thread.start()
    assert first_entered.wait(1)
    result["second"] = authority.handle(second, peer="peer-a")
    allow_first.set()
    thread.join(2)
    assert result["first"]["status"] == 200
    assert result["second"]["status"] == 409
    assert result["first"]["data"]["generation"] == 1
    assert len([call for call in transport.calls if call["message"]["kind"] == "reserve"]) == 1
    assert authority.store.reservation_count("lane-gpu0") == 1
    assert transport.calls[0]["message"]["kind"] == "reserve"


def test_durable_replay(tmp_path):
    authority, transport, clock = make_authority(tmp_path)
    original = request("req-replay", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 60, "max_s": 600})
    first = authority.handle(original, peer="peer-a")
    assert first["status"] == 200
    calls = len(transport.calls)
    restarted = make_authority(tmp_path, transport=transport, clock=clock)[0]
    replay = restarted.handle(original, peer="peer-a")
    assert replay == first
    assert len(transport.calls) == calls
    changed = copy.deepcopy(original)
    changed["args"]["max_s"] = 601
    changed["request_fingerprint"] = request_fingerprint(changed, principal=PRINCIPAL)
    assert restarted.handle(changed, peer="peer-a")["status"] == 409
    cross = copy.deepcopy(original)
    cross["request_fingerprint"] = request_fingerprint(cross, principal=OTHER_PRINCIPAL)
    assert restarted.handle(cross, peer="peer-b")["status"] == 409
    assert len(transport.calls) == calls


def test_identity_and_policy(tmp_path):
    pipelines = [{"pipeline_id": "available", "version": "1.0.0", "revision": 1, "purpose": "batch inference", "content_policy": ["acceptable-use"], "availability": "available", "partner_overrides": {}, "policy_hash": "a" * 64, "updated_at": "2026-09-28T10:00:00Z"}, {"pipeline_id": "retired", "version": "1.0.0", "revision": 1, "purpose": "retired", "content_policy": ["acceptable-use"], "availability": "unavailable", "partner_overrides": {}, "policy_hash": "b" * 64, "updated_at": "2026-09-28T10:00:00Z"}]
    authority, transport, _clock = make_authority(tmp_path, pipelines=pipelines)
    forged = request("req-forged", "acquire", {"purpose": "benchmark", "class": "operator", "est_s": 1, "max_s": 2}, admission={"ingress": {"operator_elevation": "approval-only"}})
    assert authority.handle(forged, peer="peer-a")["status"] == 403
    assert authority.handle(request("req-unmapped", "status", {}, lane=None), peer="unmapped")["status"] == 403
    unavailable = request("req-retired", "acquire", {"purpose": "retired", "class": "batch", "est_s": 1, "max_s": 2, "pipeline_ref": "retired"})
    assert authority.handle(unavailable, peer="peer-a")["status"] == 403
    stale = request(
        "req-stale-policy",
        "acquire",
        {"purpose": "batch inference", "class": "batch", "est_s": 1, "max_s": 2, "pipeline_ref": "available"},
        admission={"pipeline": {"pipeline_id": "available", "revision": 0, "policy_hash": "a" * 64}},
    )
    assert authority.handle(stale, peer="peer-a")["status"] == 403
    authority.pipelines["available"]["partner_overrides"] = {"site-a": "approval_required"}
    partner_without_approval = request(
        "req-partner-no-approval",
        "acquire",
        {"purpose": "batch inference", "class": "batch", "est_s": 1, "max_s": 2, "pipeline_ref": "available"},
        admission={"pipeline": {"pipeline_id": "available", "revision": 1, "policy_hash": "a" * 64}},
    )
    assert authority.handle(partner_without_approval, peer="peer-a")["status"] == 403
    unsupported_manifest = request(
        "req-unsupported-manifest",
        "acquire",
        {"purpose": "batch inference", "class": "batch", "est_s": 1, "max_s": 2, "pipeline_ref": "available", "signed_manifest": {"destination_site": "site-a", "principal": PRINCIPAL, "expires": "2026-09-28T11:00:00Z", "pipeline_id": "available", "policy_hash": "a" * 64, "canonical_payload_hash": "0" * 64, "proof": {"scheme": "webauthn"}}},
    )
    assert authority.handle(unsupported_manifest, peer="peer-a")["status"] == 403
    unsupported_delegation = request(
        "req-unsupported-delegation",
        "acquire",
        {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 2},
        admission={"delegation": {"delegate": PRINCIPAL, "destination_site": "site-a", "audience": "controller-a", "allowed_operations": ["acquire"], "allowed_pipelines": [], "expires": "2026-09-28T11:00:00Z", "max_depth": 0, "nonce": "delegation-nonce", "binding": {"audience": "controller-a", "nonce": "delegation-nonce"}}},
    )
    assert authority.handle(unsupported_delegation, peer="peer-a")["status"] == 403
    assert not transport.calls


def test_queue_graph(tmp_path):
    authority, _transport, _clock = make_authority(tmp_path)
    good = {"batch_id": "batch-a", "arms": [{"arm_id": "arm-a", "predecessor": None, "dependencies": []}, {"arm_id": "arm-b", "predecessor": "arm-a", "dependencies": ["arm-a"]}], "dependencies": ["arm-a"], "registered_before_execution": True, "all_arms_visible": True}
    registered = authority.register_batch(good, peer="peer-a", lane="lane-gpu0")
    assert registered["visible"] is True
    entries = authority.store.all_queue()
    assert len(entries) == 2
    assert sum(bool(entry["eligible"]) for entry in entries) == 1
    before = authority.store.event_count()
    cyclic = copy.deepcopy(good)
    cyclic["batch_id"] = "batch-cycle"
    cyclic["arms"][0]["predecessor"] = "arm-b"
    cyclic["arms"][0]["dependencies"] = ["arm-b"]
    with pytest.raises(Exception):
        authority.register_batch(cyclic, peer="peer-a", lane="lane-gpu0")
    assert authority.store.get_batch("batch-cycle") is None
    assert authority.store.event_count() == before


def test_booking_boundaries(tmp_path):
    authority, _transport, clock = make_authority(tmp_path)
    good = request("req-book", "book", {"start": "2026-09-28T11:00:00Z", "end": "2026-09-28T11:15:00Z", "purpose": "benchmark"})
    assert authority.handle(good, peer="peer-a")["status"] == 200
    short = request("req-short", "book", {"start": "2026-09-28T12:00:00Z", "end": "2026-09-28T12:14:59Z", "purpose": "benchmark"})
    assert authority.handle(short, peer="peer-a")["status"] == 403
    overlap = request("req-overlap", "book", {"start": "2026-09-28T11:10:00Z", "end": "2026-09-28T11:30:00Z", "purpose": "benchmark"})
    assert authority.handle(overlap, peer="peer-a")["status"] == 409
    horizon = request("req-horizon", "book", {"start": "2026-10-12T10:00:01Z", "end": "2026-10-12T10:15:01Z", "purpose": "benchmark"})
    assert authority.handle(horizon, peer="peer-a")["status"] == 403


def test_partition_recovery(tmp_path):
    lost, transport, clock = make_authority(tmp_path, transport=Executor(reserve="lost"))
    req = request("req-lost", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 2})
    first = lost.handle(req, peer="peer-a")
    assert first["status"] == 503
    assert lost.store.get_lane("lane-gpu0")["state"] == "quarantined"
    restart = make_authority(tmp_path, transport=Executor(), clock=clock)[0]
    assert restart.handle(request("req-new", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 2}), peer="peer-a")["status"] == 503


def test_audit_redaction(tmp_path):
    authority, _transport, _clock = make_authority(tmp_path)
    req = request("req-audit", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 2})
    granted = authority.handle(req, peer="peer-a")
    assert granted["status"] == 200
    token = granted["data"]["token"]
    events = authority.store.events()
    assert all(token not in str(event) for event in events)
    status = authority.handle(request("req-status", "status", {}), peer="peer-a")
    assert status["status"] == 200
    assert token not in str(status)


def test_matching_token_operations_and_no_caller_generation(tmp_path):
    authority, transport, _clock = make_authority(tmp_path)
    req = request("req-token", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 20})
    grant = authority.handle(req, peer="peer-a")
    token = grant["data"]["token"]
    bad = request("req-bad-release", "release", {"token": token, "generation": 1})
    assert authority.handle(bad, peer="peer-a")["status"] == 403
    wrong = request("req-wrong", "release", {"token": token})
    assert authority.handle(wrong, peer="peer-b")["status"] in {403, 409}
    good = request("req-good-release", "release", {"token": token})
    assert authority.handle(good, peer="peer-a")["status"] == 200
    assert [c["message"]["kind"] for c in transport.calls] == ["reserve", "stop"]


def test_peer_lookup_and_priority(tmp_path):
    calls = []

    def whois(peer):
        calls.append(peer)
        if peer == "peer-timeout":
            raise TimeoutError("lookup timeout")
        return {"external_id": peer}

    authority, transport, _clock = make_authority(tmp_path)
    authority.peer_authenticator = __import__("flightctl.auth", fromlist=["PeerAuthenticator"]).PeerAuthenticator(
        [
            {"external_id": "peer-a", "principal": PRINCIPAL, "roles": ["agent"]},
            {"external_id": "peer-b", "principal": OTHER_PRINCIPAL, "roles": ["agent"]},
        ],
        whois=whois,
    )
    assert authority.handle(request("req-timeout", "status", {}, lane=None), peer="peer-timeout")["status"] == 403
    assert authority.handle(request("req-unmapped", "status", {}, lane=None), peer="not-mapped")["status"] == 403
    assert calls == ["peer-timeout", "not-mapped"]
    # A lower-priority waiter cannot be skipped by a raw acquire; class rank
    # is taken from the queued record and the request's raw flags do not help.
    queued = request("req-service-queue", "queue", {"action": "add", "purpose": "service", "class": "service", "max_wait_s": 600})
    assert authority.handle(queued, peer="peer-a")["status"] == 200
    raw = request("req-raw-batch", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 2})
    assert authority.handle(raw, peer="peer-a")["status"] == 409
    assert not transport.calls


def test_mutations(tmp_path):
    """The live guards have observable failure surfaces for mutation runs."""

    authority, transport, _clock = make_authority(tmp_path)
    valid = request("req-mutation", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 2})
    assert authority.handle(valid, peer="peer-a")["status"] == 200
    changed = copy.deepcopy(valid)
    changed["args"]["purpose"] = "changed"
    changed["request_fingerprint"] = request_fingerprint(changed, principal=PRINCIPAL)
    assert authority.handle(changed, peer="peer-a")["status"] == 409
    assert len(transport.calls) == 1

    # Queue and proof guards are checked before any executor call or durable
    # success record; these are the mutation points used by the harness.
    queue = request("req-mutation-queue", "queue", {"action": "add", "purpose": "later", "class": "batch", "max_wait_s": 600})
    assert authority.handle(queue, peer="peer-a")["status"] == 200
    bypass = request("req-mutation-bypass", "acquire", {"purpose": "later", "class": "batch", "est_s": 1, "max_s": 2})
    assert authority.handle(bypass, peer="peer-a")["status"] in {409, 503}
    assert authority.store.idempotency_count() == 2
