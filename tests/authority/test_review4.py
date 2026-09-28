"""P0.1 regression checks at the production RPC/SQLite boundary."""

import copy

import pytest

from flightctl.auth import local_action_digest, local_action_projection
from flightctl.authority import request_fingerprint
from tests.authority.helpers import PRINCIPAL, Executor, make_authority, request
from tests.authority.test_p0_1_adoption import _PendingStopExecutor, _approve_vector_action, _pipeline
from tests.authority.test_revision2 import _genuine_approval_verifier, _security_key_proof
from tests.contracts.validation import validate_instance


def binding(pipeline):
    return {key: pipeline[key] for key in ("pipeline_id", "version", "revision", "purpose", "policy_hash")}


@pytest.mark.parametrize("signed_op", ["chat-load", "acquire"])
def test_chat_approval_binds_original_execution_rpc(tmp_path, signed_op):
    pipeline = _pipeline(availability="approval_required")
    authority, transport, _ = make_authority(tmp_path, pipelines=[pipeline], approval_verifier=_genuine_approval_verifier())
    execution = request("chat", "chat-load", {"purpose": pipeline["purpose"], "pipeline_ref": "batch"},
                        admission={"pipeline": binding(pipeline), "content_labels": ["acceptable-use"]})
    intended = copy.deepcopy(execution)
    if signed_op == "acquire":
        intended["op"] = "acquire"
        intended["args"].update({"class": "service", "est_s": 1, "max_s": 600})
    digest = local_action_digest(local_action_projection(intended, PRINCIPAL, destination_site="site-a",
                                                       controller_id="controller-a", policy_hash=pipeline["policy_hash"]))
    approval = _approve_vector_action(authority, "chat", digest)
    execution["admission"]["approval"] = {"approval_id": approval["id"], "required": True, "consume_atomically": True}
    execution["request_fingerprint"] = request_fingerprint(execution, principal=PRINCIPAL)
    response = authority.handle(execution, peer="peer-a")
    assert response["status"] == (200 if signed_op == "chat-load" else 403)
    validate_instance(response, "rpc-envelope-v1.schema.json")
    assert len(transport.calls) == (1 if signed_op == "chat-load" else 0)
    assert len(authority.store.all_occupants()) == (1 if signed_op == "chat-load" else 0)
    assert authority.store.get_approval(approval["id"])["state"] == ("consumed" if signed_op == "chat-load" else "approved")


@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("restriction", ["content", "unavailable", "approval_required", "stale-version"])
def test_pipeline_binding_cannot_be_bypassed_by_omitting_args_ref(tmp_path, deferred, restriction):
    pipeline = _pipeline()
    authority, transport, _ = make_authority(tmp_path, pipelines=[pipeline])
    admission = {"pipeline": binding(pipeline), "content_labels": ["acceptable-use"]}
    args = {"purpose": pipeline["purpose"], "class": "batch", "est_s": 1, "max_s": 60}
    if deferred:
        queued = authority.handle(request("queue", "queue", {"action": "add", "purpose": pipeline["purpose"], "class": "batch", "max_wait_s": 600}, admission=admission), peer="peer-a")
        assert queued["status"] == 200
        args["queue_id"] = authority.store.all_queue()[0]["queue_id"]
    if restriction == "content":
        authority.pipelines["batch"]["content_policy"] = []
    elif restriction == "stale-version":
        authority.pipelines["batch"]["version"] = "2.0.0"
    else:
        authority.pipelines["batch"]["availability"] = restriction
    response = authority.handle(request("acquire", "acquire", args, admission=admission), peer="peer-a")
    assert response["status"] == 403
    assert not transport.calls
    assert not authority.store.leases()
    assert authority.store.get_lane("lane-gpu0")["state"] == "free"


@pytest.mark.parametrize("late_outcome", ["empty", "pending", "uncertain"])
def test_late_release_reply_cannot_overwrite_successor(tmp_path, late_outcome):
    class InterleavedExecutor(Executor):
        callback = None

        def request(self, endpoint, message, timeout_s):
            reply = super().request(endpoint, message, timeout_s)
            if message["kind"] == "stop" and self.callback is not None:
                callback, self.callback = self.callback, None
                callback()
                if late_outcome != "empty":
                    reply["response"].update(ok=False, observed_state="stopping", error="still draining",
                                             uncertain=late_outcome == "uncertain", cgroup_occupants=["occupant-a"])
            return reply

    transport = InterleavedExecutor()
    authority, _, _ = make_authority(tmp_path, transport=transport)
    args = {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60}
    original = authority.handle(request("first", "acquire", args), peer="peer-a")
    token = original["data"]["token"]
    successor = {}

    def finish_then_grant():
        assert authority.handle(request("release-fast", "release", {"token": token}), peer="peer-a")["status"] == 200
        successor.update(authority.handle(request("successor", "acquire", args), peer="peer-a"))
        assert successor["status"] == 200

    transport.callback = finish_then_grant
    slow_request = request("release-slow", "release", {"token": token})
    slow = authority.handle(slow_request, peer="peer-a")
    lane = authority.store.get_lane("lane-gpu0")
    assert (lane["generation"], lane["state"]) == (2, "starting")
    assert authority.store.get_lease(token=token)[1] == "released"
    assert authority.store.get_lease(token=successor["data"]["token"])[1] == "acknowledged"
    assert slow["status"] == 409
    assert authority.handle(slow_request, peer="peer-a") == slow
    assert [call["message"]["kind"] for call in transport.calls] == ["reserve", "stop", "stop", "reserve"]


def test_content_policy_changes_do_not_replace_durable_replay(tmp_path):
    authority, transport, clock = make_authority(tmp_path, policy={"content_rules": ["acceptable-use"]})
    execution = request("first", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60},
                        admission={"content_labels": ["acceptable-use"]})
    granted = authority.handle(execution, peer="peer-a")
    assert granted["status"] == 200
    authority.store.close()
    restarted, _, _ = make_authority(tmp_path, transport=transport, clock=clock, policy={"content_rules": ["different"]})
    assert restarted.handle(execution, peer="peer-a") == granted
    assert len(transport.calls) == 1
    fresh = dict(execution, request_id="fresh")
    fresh["request_fingerprint"] = request_fingerprint(fresh, principal=PRINCIPAL)
    assert restarted.handle(fresh, peer="peer-a")["status"] == 403
    assert len(transport.calls) == 1


def test_pending_release_replays_after_restart_without_new_stop(tmp_path):
    transport = _PendingStopExecutor()
    authority, _, clock = make_authority(tmp_path, transport=transport)
    granted = authority.handle(request("first", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60}), peer="peer-a")
    release = request("release", "release", {"token": granted["data"]["token"]})
    pending = authority.handle(release, peer="peer-a")
    assert pending["status"] == 202
    authority.store.close()
    restarted, _, _ = make_authority(tmp_path, transport=transport, clock=clock)
    assert restarted.handle(release, peer="peer-a") == pending
    assert restarted.store.get_lane("lane-gpu0")["state"] != "free"
    assert [call["message"]["kind"] for call in transport.calls] == ["reserve", "stop"]


def test_batch_deferred_admission_retains_content_labels(tmp_path):
    authority, transport, _ = make_authority(tmp_path, policy={"content_rules": ["acceptable-use"]})
    batch = {"batch_id": "batch-a", "arms": [{"arm_id": "later", "predecessor": None, "dependencies": []}],
             "dependencies": [], "registered_before_execution": True, "all_arms_visible": True}
    args = {"purpose": "batch", "class": "batch", "est_s": 1, "max_s": 60}
    seed = authority.handle(request("seed", "acquire", args,
                                    admission={"batch": batch, "content_labels": ["acceptable-use"]}), peer="peer-a")
    assert seed["status"] == 200
    assert authority.handle(request("release", "release", {"token": seed["data"]["token"]}), peer="peer-a")["status"] == 200
    deferred_args = dict(args, queue_id="batch-a-later")
    stripped = authority.handle(request("stripped", "acquire", deferred_args), peer="peer-a")
    assert stripped["status"] == 403
    assert len(transport.calls) == 2
    assert authority.store.get_queue("batch-a-later")["state"] == "queued"
    valid = authority.handle(request("valid", "acquire", deferred_args,
                                     admission={"content_labels": ["acceptable-use"]}), peer="peer-a")
    assert valid["status"] == 200
    assert len(transport.calls) == 3


def test_booking_claim_approval_binds_original_execution_rpc(tmp_path):
    pipeline = _pipeline(availability="approval_required")
    authority, transport, _ = make_authority(tmp_path, pipelines=[pipeline], approval_verifier=_genuine_approval_verifier())
    booked = authority.handle(request("book", "book", {"purpose": pipeline["purpose"], "start": "2026-09-28T10:00:00Z",
                                                      "end": "2026-09-28T10:15:00Z"}), peer="peer-a")
    assert booked["status"] == 200
    booking = booked["data"]["booking"]
    execution = request("claim", "claim", {"booking_id": booking["booking_id"], "revision": booking["revision"]},
                        admission={"pipeline": binding(pipeline)})
    digest = local_action_digest(local_action_projection(execution, PRINCIPAL, destination_site="site-a",
                                                       controller_id="controller-a", policy_hash=pipeline["policy_hash"]))
    issued = authority.handle(request("challenge", "approval-request", {
        "action": "pipeline", "booking_id": booking["booking_id"], "revision": pipeline["revision"], "target_generation": None,
        "bounds": {"max_s": 900, "max_end": booking["end"]}, "reason": "claim approval", "destination_site": "site-a",
        "controller_id": "controller-a", "payload_hash": digest, "manifest_hash": None, "policy_hash": pipeline["policy_hash"],
    }), peer="peer-a")
    assert issued["status"] == 200
    approval = issued["data"]["approval"]
    approved = authority.handle(request("approve", "approve", {"approval_id": approval["id"],
        "proof": _security_key_proof(bytes(range(32)), approval),
        "evidence": {"verifier": "key-a", "verified_at": "2026-09-28T10:00:01Z", "user_presence": "verified", "user_verification": "verified"},
    }), peer="peer-a")
    assert approved["status"] == 200
    execution["admission"]["approval"] = {"approval_id": approval["id"], "required": True, "consume_atomically": True}
    execution["request_fingerprint"] = request_fingerprint(execution, principal=PRINCIPAL)
    result = authority.handle(execution, peer="peer-a")
    assert result["status"] == 200
    validate_instance(result, "rpc-envelope-v1.schema.json")
    assert result["data"]["operation"] == "claim"
    assert authority.store.get_approval(approval["id"])["state"] == "consumed"
    assert len(transport.calls) == 1
