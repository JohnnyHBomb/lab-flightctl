from __future__ import annotations

from datetime import datetime, timezone
import json
from threading import Barrier, Thread
from typing import Mapping

import pytest

from ondemand.chat import ChatAdapter, ChatController, ChatSelection
from tests.fakes import FakeClock, FakeGPUProbe, FakeSSH
from tests.chat.support import UnitSystemdAdapter
from tests.contracts.validation import validate_definition, validate_rpc


SITE = "site-a"
PRINCIPAL = {"site_id": SITE, "tenant_id": "tenant-a", "issuer": "issuer-a", "subject": "service-a"}


def inventory(*, order: list[str] | None = None) -> dict[str, object]:
    order = order or ["lane-a", "lane-b"]
    hosts = [
        {"host_id": "host-a", "reachability": "confirmed"},
        {"host_id": "host-b", "reachability": "confirmed"},
    ]
    lanes = [
        {"lane_id": "lane-a", "host_id": "host-a", "enabled": True, "booked": False, "compatible": True, "reachability": "confirmed", "state": "free"},
        {"lane_id": "lane-b", "host_id": "host-b", "enabled": True, "booked": False, "compatible": True, "reachability": "confirmed", "state": "free"},
    ]
    return {"site_id": SITE, "controller": {"controller_id": "controller-a", "endpoint": "authority"}, "chat_lane_order": order, "hosts": hosts, "lanes": lanes}


def gpu_probe(*, scripted: list[Mapping[str, object]] | None = None) -> FakeGPUProbe:
    fixtures = {
        "host-a": {"family": "none", "raw": ""},
        "host-b": {"family": "none", "raw": ""},
    }
    return FakeGPUProbe(fixtures, scripted=scripted)


def grant_response(*, lane_id: str = "lane-a", host_id: str = "host-a", generation: int = 1, token: str = "token-abcdefghijklmnop", lease_id: str = "occupant-a", unit: str = "unit-a", invocation: str = "invoke-a", request_id: str = "request-from-authority") -> dict[str, object]:
    lane = {"site_id": SITE, "host_id": host_id, "lane_id": lane_id}
    lease = {
        "schema_version": 1,
        "lease_id": lease_id,
        "lane": lane,
        "generation": generation,
        "reservation": {"lane": lane, "generation": generation, "state": "starting"},
        "token": token,
        "instance": f"instance-{generation}",
        "principal": PRINCIPAL,
        "class": "service",
        "purpose": "interactive inference",
        "estimated_s": 600,
        "started_at": "2026-09-28T10:00:00Z",
        "max_end": "2027-09-28T10:00:00Z",
        "approved_max_end": "2027-09-28T10:00:00Z",
        "heartbeat_at": "2026-09-28T10:00:00Z",
        "deadline": {"boot_id": "boot-a", "deadline_s": 600, "utc_anchor": "2026-09-28T10:00:00Z", "monotonic_anchor_s": 0},
        "booking_id": None,
        "unit": unit,
        "invocation": invocation,
        "state": "starting",
    }
    return {
        "schema": 1,
        "request_id": request_id,
        "status": 200,
        "data": {
            "kind": "grant",
            "operation": "chat-load",
            "token": token,
            "generation": generation,
            "lease": lease,
            "reservation": {"lane": lane, "generation": generation, "state": "starting"},
            "adoption": {"mode": "fresh-acquire", "principal_bound": True, "generation_bound": True, "token_source": "controller-grant"},
        },
        "error": None,
    }


def unload_response(occupant_id: str = "occupant-a", generation: int = 1, lane_id: str = "lane-a", host_id: str = "host-a", request_id: str = "request-from-authority") -> dict[str, object]:
    lane = {"site_id": SITE, "host_id": host_id, "lane_id": lane_id}
    return {
        "schema": 1,
        "request_id": request_id,
        "status": 200,
        "data": {
            "kind": "mutation",
            "operation": "chat-unload",
            "record_type": "occupant",
            "record_id": occupant_id,
            "state": "stopping",
            "revision": 1,
            "reservation": {"lane": lane, "generation": generation, "state": "stopping"},
        },
        "error": None,
    }


def queue_load(transport: FakeSSH, response: Mapping[str, object]) -> None:
    transport.queue("success", response=response)


def make_controller(
    *,
    transport: FakeSSH | None = None,
    systemd: UnitSystemdAdapter | None = None,
    gpu: FakeGPUProbe | None = None,
    order: list[str] | None = None,
) -> tuple[ChatController, FakeSSH, UnitSystemdAdapter, FakeClock, FakeGPUProbe]:
    transport = transport or FakeSSH()
    systemd = systemd or UnitSystemdAdapter()
    gpu = gpu or gpu_probe()
    clock = FakeClock(datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc))
    controller = ChatController(clock, transport, systemd, gpu, inventory(order=order), principal=PRINCIPAL)
    return controller, transport, systemd, clock, gpu


def load_one(controller: ChatController, transport: FakeSSH, *, generation: int = 1, token: str = "token-abcdefghijklmnop", lane_id: str = "lane-a", host_id: str = "host-a", lease_id: str = "occupant-a", unit: str = "unit-a", invocation: str = "invoke-a", request_id: str = "load-1"):
    queue_load(transport, grant_response(lane_id=lane_id, host_id=host_id, generation=generation, token=token, lease_id=lease_id, unit=unit, invocation=invocation, request_id=request_id))
    result = controller.load(ChatSelection("pipeline-a", "interactive inference", compatible_lanes=frozenset({lane_id})), request_id)
    assert result.ok, result.as_dict(redacted=False)
    return result


def test_atomic_load_vs_grant() -> None:
    transport = FakeSSH()
    systemd = UnitSystemdAdapter()
    probe = gpu_probe()
    barrier = Barrier(2)
    responses = [grant_response(), {"schema": 1, "request_id": "r", "status": 409, "data": None, "error": {"code": "conflict", "message": "reserved elsewhere", "retryable": True, "failure_class": "conflict"}}]
    index = {"value": 0}

    class BarrierTransport:
        def request(self, endpoint: str, message: Mapping[str, object], timeout_s: float) -> Mapping[str, object]:
            barrier.wait(timeout=2)
            response = dict(responses[index["value"]])
            index["value"] += 1
            response["request_id"] = message["request_id"]
            return {"status": "ok", "response": response}

    first = ChatController(FakeClock(), BarrierTransport(), systemd, probe, inventory(order=["lane-a"]), principal=PRINCIPAL)
    second = ChatController(FakeClock(), BarrierTransport(), systemd, probe, inventory(order=["lane-a"]), principal=PRINCIPAL)
    results: list[object] = []

    def run(controller: ChatController, request_id: str) -> None:
        results.append(controller.load(ChatSelection("pipeline-a", "interactive inference", compatible_lanes=frozenset({"lane-a"})), request_id))

    threads = [Thread(target=run, args=(first, "load-a")), Thread(target=run, args=(second, "load-b"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
    assert len(results) == 2
    assert sum(result.ok for result in results) == 1
    assert len(systemd.started) == 1
    successful = next(result for result in results if result.ok)
    assert successful.trace[:2] == ("registered/loading", "running")
    assert all(result.state in {"running", "unavailable", "excluded"} for result in results)


def test_external_lane_order() -> None:
    controller, _, _, _, _ = make_controller()
    selection = ChatSelection("pipeline-a", "interactive inference", compatible_lanes=frozenset({"lane-a", "lane-b"}))
    assert controller.select_lane(selection) == "lane-a"
    assert controller.select_lane(selection, lane_order=["lane-b", "lane-a"]) == "lane-b"
    lane_a = {"host_id": "host-a", "enabled": True, "booked": False, "compatible": True, "reachability": "confirmed", "state": "free"}
    lane_b = {"host_id": "host-b", "enabled": True, "booked": False, "compatible": True, "reachability": "confirmed", "state": "free"}
    assert controller.select_lane(selection, availability={"lane-a": {**lane_a, "booked": True}, "lane-b": lane_b}) == "lane-b"
    assert controller.select_lane(selection, availability={"lane-a": {**lane_a, "reachability": "unknown"}, "lane-b": lane_b}) == "lane-b"
    assert controller.select_lane(selection, availability={"lane-a": {**lane_a, "compatible": False}, "lane-b": lane_b}) == "lane-b"
    assert controller.select_lane(ChatSelection("pipeline-a", "interactive inference", compatible_lanes=frozenset({"lane-a"})), availability={"lane-a": {**lane_a, "booked": True}}) is None


def test_lane_selection_requires_explicit_observations() -> None:
    controller, _, _, _, _ = make_controller()
    selection = ChatSelection("pipeline-a", "interactive inference", compatible_lanes=frozenset({"lane-a", "lane-b"}))
    assert controller.select_lane(selection, availability={}) is None
    assert controller.select_lane(
        selection,
        availability={
            "lane-a": {"host_id": "host-a", "enabled": None, "booked": None, "compatible": None, "reachability": "confirmed", "state": "free"},
            "lane-b": {"host_id": "host-b", "enabled": True, "booked": False, "compatible": True, "reachability": "confirmed", "state": "free"},
        },
    ) == "lane-b"
    controller.inventory.pop("hosts")
    assert controller.select_lane(selection) is None


@pytest.mark.parametrize("failure", ["authority-timeout", "executor-timeout", "mismatched-identity", "failed-load"])
def test_load_failure(failure: str) -> None:
    controller, transport, systemd, _, _ = make_controller()
    if failure == "authority-timeout":
        transport.queue("timeout")
    elif failure == "mismatched-identity":
        queue_load(transport, grant_response(lane_id="lane-b", host_id="host-b", request_id="same-request"))
    else:
        queue_load(transport, grant_response(request_id="same-request"))
        if failure in {"executor-timeout", "failed-load"}:
            systemd.queue("unit-a", "timeout")
    result = controller.load(ChatSelection("pipeline-a", "interactive inference", compatible_lanes=frozenset({"lane-a"})), "same-request")
    assert not result.ok
    assert controller.state in {"unavailable", "quarantined"}
    starts = [call for call in systemd.calls if call["method"] == "start"]
    assert len(starts) == (1 if failure in {"executor-timeout", "failed-load"} else 0)
    retry = controller.load(ChatSelection("pipeline-a", "interactive inference", compatible_lanes=frozenset({"lane-a"})), "same-request")
    assert retry is result
    assert len(starts) == (1 if failure in {"executor-timeout", "failed-load"} else 0)
    assert controller.accounting()["last_completed_at"] is None


def test_request_accounting() -> None:
    controller, transport, _, clock, _ = make_controller()
    load_one(controller, transport)
    adapter = ChatAdapter(controller, generation=1, occupant_id="occupant-a")
    assert controller.accounting() == {"active_requests": 0, "completed_requests": 0, "last_completed_at": None, "activity_basis": "completed-user-request"}
    assert adapter.request_started("request-a")
    assert adapter.request_started("request-b")
    before = controller.accounting()
    adapter.health_check()
    adapter.connected()
    assert controller.accounting()["active_requests"] == 2
    clock.advance(utc_s=4, monotonic_s=4)
    assert adapter.request_completed("request-a")
    assert adapter.request_error("request-b")
    assert not adapter.disconnected("request-b")
    accounting = controller.accounting()
    assert accounting["active_requests"] == 0
    assert accounting["completed_requests"] == 2
    assert accounting["last_completed_at"] is not None
    assert before["active_requests"] == 2
    record = controller.apply_request_event("start", "request-c")
    assert record is not None
    assert controller.apply_request_event("complete", "request-c") is not None
    validate_definition(controller.occupant_record(), "occupant-v1.schema.json", "occupant")


def test_health_and_reconnect_do_not_move_inactivity_anchor() -> None:
    controller, transport, _, clock, _ = make_controller()
    load_one(controller, transport)
    adapter = ChatAdapter(controller, generation=1, occupant_id="occupant-a")
    clock.advance(utc_s=599, monotonic_s=599)
    before = controller._current.last_activity_monotonic
    adapter.health_check()
    adapter.connected()
    assert controller._current.last_activity_monotonic == before
    assert not controller.idle_due()
    clock.advance(utc_s=1, monotonic_s=1)
    assert controller.idle_due()


def test_streaming_inactivity() -> None:
    controller, transport, _, clock, _ = make_controller()
    load_one(controller, transport)
    adapter = ChatAdapter(controller, generation=1, occupant_id="occupant-a")
    assert adapter.stream_started("stream-a")
    clock.advance(utc_s=601, monotonic_s=601)
    assert not controller.idle_due()
    assert controller.poll_idle().ok is False
    assert controller.state == "running"
    assert adapter.stream_completed("stream-a")
    clock.advance(utc_s=599, monotonic_s=599)
    assert controller.poll_idle().ok is False
    assert controller.state == "running"
    queue_load(transport, unload_response(request_id="idle-unload"))
    clock.advance(utc_s=1, monotonic_s=1)
    result = controller.poll_idle("idle-unload")
    assert result.ok
    assert controller.state == "unavailable"


def test_eviction_drain() -> None:
    controller, transport, systemd, clock, _ = make_controller()
    load_one(controller, transport)
    adapter = ChatAdapter(controller, generation=1, occupant_id="occupant-a")
    assert adapter.stream_started("stream-a")
    assert controller.request_eviction("batch").state == "draining"
    assert not adapter.request_started("new-request")
    assert adapter.stream_completed("stream-a")
    clock.advance(utc_s=119, monotonic_s=119)
    assert controller.process_eviction().ok is False
    queue_load(transport, unload_response(request_id="eviction-unload"))
    clock.advance(utc_s=1, monotonic_s=1)
    result = controller.process_eviction("eviction-unload")
    assert result.ok
    assert controller.state == "unavailable"
    assert [call["method"] for call in systemd.calls] == ["start", "inspect", "stop", "inspect"]
    assert controller.request_eviction("service").ok is False


@pytest.mark.parametrize("failure", ["residual", "unknown-probe", "failed-stop", "lost-authority"])
def test_failed_unload_exclusion(failure: str) -> None:
    probe = gpu_probe(scripted=[{"status": "unknown"}]) if failure == "unknown-probe" else gpu_probe()
    controller, transport, systemd, _, _ = make_controller(gpu=probe)
    load_one(controller, transport)
    if failure == "lost-authority":
        transport.queue("lost")
    else:
        queue_load(transport, unload_response(request_id=f"unload-{failure}"))
        if failure == "residual":
            systemd.set_occupancy("unit-a", cgroup=["tenant-a"], gpu=["gpu-a"])
        elif failure == "failed-stop":
            systemd.queue("unit-a", "failed_stop")
    result = controller.unload(occupant_id="occupant-a", generation=1, request_id=f"unload-{failure}")
    assert not result.ok
    assert controller.state == "quarantined"
    starts_before = len([call for call in systemd.calls if call["method"] == "start"])
    blocked = controller.load(ChatSelection("pipeline-a", "interactive inference", compatible_lanes=frozenset({"lane-a"})), "successor")
    assert not blocked.ok
    assert len([call for call in systemd.calls if call["method"] == "start"]) == starts_before
    if failure in {"residual", "unknown-probe", "failed-stop"}:
        inspect_calls = [call for call in systemd.calls if call["method"] == "inspect"]
        assert inspect_calls[-1]["unit"] == "unit-a"
        assert inspect_calls[-1]["invocation"] == "invoke-a"


def test_cleanup_stops_verified_worker_before_checking_empty() -> None:
    controller, transport, systemd, _, _ = make_controller()
    load_one(controller, transport)
    systemd.set_occupancy("unit-a", cgroup=["owned-worker"])
    queue_load(transport, unload_response(request_id="owned-cleanup"))
    result = controller.unload(occupant_id="occupant-a", generation=1, request_id="owned-cleanup")
    assert result.ok
    assert [call["method"] for call in systemd.calls] == ["start", "inspect", "stop", "inspect"]


def test_cleanup_rejects_unknown_and_post_stop_gpu_occupancy() -> None:
    controller, transport, systemd, _, _ = make_controller(gpu=gpu_probe(scripted=[{}]))
    load_one(controller, transport)
    queue_load(transport, unload_response(request_id="unknown-gpu"))
    unknown = controller.unload(occupant_id="occupant-a", generation=1, request_id="unknown-gpu")
    assert not unknown.ok
    assert controller.state == "quarantined"
    assert [call["method"] for call in systemd.calls] == ["start", "inspect"]

    controller, transport, systemd, _, _ = make_controller(
        gpu=gpu_probe(scripted=[{"count": 0, "reason": "confirmed no GPU", "devices": []}, {"gpu_tenants": ["residual-tenant"]}])
    )
    load_one(controller, transport)
    queue_load(transport, unload_response(request_id="post-stop-gpu"))
    residual = controller.unload(occupant_id="occupant-a", generation=1, request_id="post-stop-gpu")
    assert not residual.ok
    assert controller.state == "quarantined"
    assert [call["method"] for call in systemd.calls] == ["start", "inspect", "stop", "inspect"]


def test_cleanup_rejects_contradictory_inspection_identity() -> None:
    class ForgedInspectExecutor:
        def __init__(self, base):
            self.base = base

        def reserve(self, request):
            return self.base.reserve(request)

        def start(self, request):
            return self.base.start(request)

        def stop(self, request):
            return self.base.stop(request)

        def inspect(self, request, *, unit=None, invocation=None):
            identity = dict(request["identity"])
            identity["unit"] = None
            identity["invocation"] = None
            return {
                "schema_version": 1,
                "kind": "inspect",
                "echoed_identity": identity,
                "acknowledgement": "inspected",
                "ok": True,
                "observed_state": "unknown",
                "uncertain": False,
                "cgroup_occupants": [],
                "gpu_tenants": [],
                "error": None,
            }

    controller, transport, _, _, _ = make_controller()
    load_one(controller, transport)
    controller.executor = ForgedInspectExecutor(controller.executor)
    queue_load(transport, unload_response(request_id="forged-inspect"))
    result = controller.unload(occupant_id="occupant-a", generation=1, request_id="forged-inspect")
    assert not result.ok
    assert controller.state == "quarantined"


@pytest.mark.parametrize("binding", ["request", "operation", "host"])
def test_unload_authority_bindings_are_required(binding: str) -> None:
    controller, transport, systemd, _, _ = make_controller()
    load_one(controller, transport)
    response = unload_response(request_id="binding-check")
    if binding == "request":
        response["request_id"] = "other-request"
    elif binding == "operation":
        response["data"]["operation"] = "chat-load"
    else:
        response["data"]["reservation"]["lane"]["host_id"] = "other-host"
    queue_load(transport, response)
    result = controller.unload(occupant_id="occupant-a", generation=1, request_id="binding-check")
    assert not result.ok
    assert [call["method"] for call in systemd.calls] == ["start"]


def test_successful_cleanup_retry_restores_lane_eligibility() -> None:
    controller, transport, systemd, _, _ = make_controller()
    load_one(controller, transport)
    systemd.queue("unit-a", "failed_stop")
    queue_load(transport, unload_response(request_id="failed-cleanup"))
    failed = controller.unload(occupant_id="occupant-a", generation=1, request_id="failed-cleanup")
    assert not failed.ok
    assert controller.state == "quarantined"

    queue_load(transport, unload_response(request_id="retry-cleanup"))
    recovered = controller.unload(occupant_id="occupant-a", generation=1, request_id="retry-cleanup")
    assert recovered.ok
    assert controller.select_lane(ChatSelection("pipeline-a", "interactive inference", compatible_lanes=frozenset({"lane-a"}))) == "lane-a"


def test_manual_reload_only() -> None:
    controller, transport, systemd, _, _ = make_controller()
    load_one(controller, transport)
    queue_load(transport, unload_response(request_id="unload-1"))
    assert controller.unload(occupant_id="occupant-a", generation=1, request_id="unload-1").ok
    call_count = len(transport.calls)
    start_count = len([call for call in systemd.calls if call["method"] == "start"])
    assert controller.on_reconnect() is False
    assert controller.health_check()["state"] == "unavailable"
    assert controller.unit_restarted()["state"] == "unavailable"
    assert len(transport.calls) == call_count
    assert len([call for call in systemd.calls if call["method"] == "start"]) == start_count
    queue_load(transport, grant_response(generation=2, token="token-bbbbbbbbbbbbbbbb", lease_id="occupant-b", unit="unit-b", invocation="invoke-b", request_id="load-2"))
    explicit = controller.load(ChatSelection("pipeline-b", "interactive inference", compatible_lanes=frozenset({"lane-a"})), "load-2")
    assert explicit.ok
    assert len([call for call in systemd.calls if call["method"] == "start"]) == start_count + 1


def test_redacted_occupancy() -> None:
    controller, transport, _, _, _ = make_controller()
    load_one(controller, transport)
    public = {"status": controller.public_status(), "occupancy": controller.public_occupancy(), "accounting": controller.accounting()}
    assert "token-abcdefghijklmnop" not in json.dumps(public, sort_keys=True)
    old_adapter = ChatAdapter(controller, generation=1, occupant_id="occupant-a")
    queue_load(transport, unload_response(request_id="unload-1"))
    assert controller.unload(occupant_id="occupant-a", generation=1, request_id="unload-1").ok
    queue_load(transport, grant_response(generation=2, token="token-bbbbbbbbbbbbbbbb", lease_id="occupant-b", unit="unit-b", invocation="invoke-b", request_id="load-2"))
    assert controller.load(ChatSelection("pipeline-b", "interactive inference", compatible_lanes=frozenset({"lane-a"})), "load-2").ok
    assert not old_adapter.request_started("stale")
    assert not controller.unload(occupant_id="occupant-a", generation=1, request_id="stale-unload").ok
    assert controller.state == "running"
    assert "token-bbbbbbbbbbbbbbbb" not in json.dumps(controller.public_status(), sort_keys=True)


def test_redacted_result_removes_nested_grant_token() -> None:
    controller, transport, _, _, _ = make_controller()
    result = load_one(controller, transport)
    public = result.as_dict()
    assert "token-abcdefghijklmnop" not in json.dumps(public, sort_keys=True)
    assert "token" not in public["response"]["data"]


def test_package_adapter_rejects_unknown_systemd_outcome() -> None:
    adapter = UnitSystemdAdapter()
    with pytest.raises(ValueError):
        adapter.queue("unit-a", "not-a-real-outcome")
    assert adapter.calls == []


def test_frozen_rpc_and_executor_mappings() -> None:
    controller, transport, _, _, _ = make_controller()
    load_one(controller, transport)
    validate_rpc(transport.calls[0]["message"])
    queue_load(transport, unload_response(request_id="unload-shape"))
    assert controller.unload(occupant_id="occupant-a", generation=1, request_id="unload-shape").ok
    validate_rpc(transport.calls[1]["message"])
    for call in controller.executor.calls:
        validate_definition(call["request"], "executor-v1.schema.json", "request")
