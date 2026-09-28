"""Scripted authority races and lifecycle branches at the actual chat seams."""

import hashlib
import json
from threading import Barrier, Event, Lock, Thread

import pytest

from ondemand.chat import ChatAdapter, ChatSelection
from tests.chat.test_chat import grant_response, load_one, make_controller, queue_load, unload_response
from tests.contracts.validation import validate_definition, validate_rpc


@pytest.mark.parametrize("winner", ["chat-load", "acquire"])
def test_atomic_load_vs_grant(winner):
    barrier, registered, lock = Barrier(2), Event(), Lock()
    owners, trace, errors, results = [], [], [], {}

    class Authority:
        def request(self, endpoint, message, timeout_s):
            validate_rpc(message)
            barrier.wait(timeout=2)
            operation = message["op"]
            if operation != winner:
                assert registered.wait(timeout=2)
            with lock:
                if owners:
                    return {"status": "ok", "response": {
                        "schema": 1, "request_id": message["request_id"], "status": 409,
                        "data": None, "error": {"code": "conflict", "message": "reserved",
                        "retryable": True, "failure_class": "conflict"},
                    }}
                owners.append(operation)
                trace.append("registered:" + operation)
                response = grant_response(request_id=message["request_id"])
                if operation == "acquire":
                    response["data"]["operation"] = "acquire"
                    response["data"]["lease"]["class"] = "batch"
                registered.set()
                return {"status": "ok", "response": response}

    authority = Authority()
    controller, _, systemd, _, _ = make_controller(transport=authority)
    original_start = systemd.start

    def start(unit, invocation):
        assert owners == ["chat-load"]
        assert controller.occupant["state"] == "loading"
        trace.append("start:chat")
        return original_start(unit, invocation)

    systemd.start = start
    selection = ChatSelection("pipeline-a", "interactive inference", compatible_lanes=frozenset({"lane-a"}))
    batch = controller._rpc_request("acquire", "lane-a", {
        "purpose": "batch processing", "class": "batch", "est_s": 120, "max_s": 120,
    }, "batch-race", selection)
    batch["admission"]["batch"] = {
        "batch_id": "batch-a", "arms": [{"arm_id": "arm-a", "predecessor": None, "dependencies": []}],
        "dependencies": [], "registered_before_execution": True, "all_arms_visible": True,
    }
    del batch["request_fingerprint"]
    batch["request_fingerprint"] = hashlib.sha256(json.dumps(batch, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def run(key, callback):
        try:
            results[key] = callback()
        except BaseException as exc:
            errors.append(exc)

    threads = [
        Thread(target=run, args=("chat", lambda: controller.load(selection, "chat-race"))),
        Thread(target=run, args=("batch", lambda: authority.request("authority", batch, 2))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors
    assert owners == [winner]
    assert results["chat"].ok == (winner == "chat-load")
    assert (results["batch"]["response"]["status"] == 200) == (winner == "acquire")
    assert len(systemd.started) == (winner == "chat-load")
    if winner == "chat-load":
        assert trace == ["registered:chat-load", "start:chat"]
        assert results["chat"].trace == ("registered/loading", "running")
    else:
        assert trace == ["registered:acquire"]
        assert results["chat"].state == "unavailable"


@pytest.mark.parametrize("status", [202, 403, 409])
def test_unacknowledged_registration_never_starts(status):
    controller, transport, systemd, _, _ = make_controller()
    response = {"schema": 1, "request_id": "no-registration", "status": status, "data": None, "error": None}
    if status == 202:
        response["data"] = {
            "kind": "pending", "operation": "chat-load", "request_id": "no-registration", "queue_id": None,
            "retry_after_s": 1, "wait_deadline": grant_response()["data"]["lease"]["deadline"], "reason": "waiting",
        }
    else:
        failure = "denied" if status == 403 else "conflict"
        response["error"] = {"code": failure, "message": failure, "retryable": True, "failure_class": "policy" if status == 403 else "conflict"}
    validate_definition(response, "rpc-envelope-v1.schema.json", "response")
    queue_load(transport, response)
    result = controller.load(ChatSelection("pipeline-a", "interactive inference"), "no-registration")
    assert not result.ok
    assert not systemd.calls
    assert controller.occupant is None


@pytest.mark.parametrize("protected_class", ["operator", "booked", "batch"])
def test_protected_grants_never_become_evictable_chat(protected_class):
    controller, transport, systemd, clock, _ = make_controller()
    response = grant_response(request_id="protected")
    response["data"]["lease"]["class"] = protected_class
    queue_load(transport, response)
    assert not controller.load(ChatSelection("pipeline-a", "interactive inference"), "protected").ok
    assert not controller.request_eviction("operator").ok
    clock.advance(utc_s=120, monotonic_s=120)
    assert not controller.process_eviction().ok
    assert not systemd.calls


@pytest.mark.parametrize("trigger", ["batch", "booked", "operator"])
def test_active_stream_at_grace_deadline_and_verified_successor(trigger):
    controller, transport, systemd, clock, probe = make_controller()
    load_one(controller, transport)
    adapter = ChatAdapter(controller, generation=1, occupant_id="occupant-a")
    assert adapter.stream_started("long-stream")
    assert controller.request_eviction(trigger).state == "draining"
    assert not adapter.stream_started("new-stream")
    clock.advance(utc_s=119, monotonic_s=119)
    assert not controller.process_eviction().ok
    assert not controller.load(ChatSelection("pipeline-b", "interactive inference"), "early-successor").ok
    assert [call["method"] for call in systemd.calls] == ["start"]
    queue_load(transport, unload_response(request_id="deadline-stop"))
    clock.advance(utc_s=1, monotonic_s=1)
    assert controller.accounting()["active_requests"] == 1
    assert controller.process_eviction("deadline-stop").ok
    assert [call["method"] for call in systemd.calls] == ["start", "inspect", "stop", "inspect"]
    assert probe.calls == [{"host": "host-a"}, {"host": "host-a"}]
    assert controller.executor.calls[-1]["request"]["stop_authority"]["mode"] == "controller-match"
    load_one(controller, transport, generation=2, lease_id="occupant-b", unit="unit-b", invocation="invoke-b", request_id="successor")
    assert [call["method"] for call in systemd.calls] == ["start", "inspect", "stop", "inspect", "start"]
    assert not adapter.stream_completed("long-stream")
    assert controller.accounting()["completed_requests"] == 0


def test_cleanup_retry_observes_both_sources_and_exact_invocation():
    controller, transport, systemd, _, probe = make_controller()
    load_one(controller, transport)
    systemd.set_occupancy("unit-a", cgroup=["worker-a"], gpu=["worker-a"])
    probe.scripted[:] = [{"gpu_tenants": ["worker-a"]}, {"gpu_tenants": ["residue"]}]
    queue_load(transport, unload_response(request_id="residual-stop"))
    assert not controller.unload(request_id="residual-stop").ok
    assert controller.state == "quarantined"
    probe.scripted[:] = [{"gpu_tenants": []}, {"gpu_tenants": []}]
    queue_load(transport, unload_response(request_id="retry-stop"))
    assert controller.unload(request_id="retry-stop").ok
    assert len(probe.calls) == 4
    assert all(call["unit"] == "unit-a" and call["invocation"] == "invoke-a" for call in systemd.calls)
    assert [call["method"] for call in systemd.calls] == ["start", "inspect", "stop", "inspect", "inspect", "stop", "inspect"]
