"""Fourth-review regressions at the production P0.1 boundary, using fakes."""

import copy
import json
import threading

import pytest

from flightctl.executor import Executor, LocalActionError, canonical_local_action_payload
from tests.contracts.validation import assert_invalid, validate_instance
from tests.executor.test_p01 import (
    DrainingSystemd, LANE, _load_p01, _prepare_owner_release,
)
from tests.executor.test_executor import DelayedStart, identity, make_executor, policy, request, workload


def _pending(tmp_path):
    executor, systemd, gpu, trusted, stop = _prepare_owner_release(
        tmp_path, systemd=DrainingSystemd(),
    )
    systemd.instance("unit-a", "invoke-a").occupants = ["pid-draining"]
    gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})
    response = executor.release_rpc(stop, "release-first", authenticated_controller=trusted)
    assert response["status"] == 202
    return executor, systemd, gpu, trusted, stop, response


@pytest.mark.parametrize("change", ["token", "authority", "principal", "kind"])
def test_pending_lane_does_not_turn_rejection_into_acceptance(tmp_path, change):
    executor, systemd, _, trusted, stop, _ = _pending(tmp_path)
    before = (tmp_path / "state.json").read_bytes()
    calls = copy.deepcopy(systemd.calls)
    if change == "token":
        stop["identity"]["token"] = "different-token-value"
    elif change == "authority":
        stop["stop_authority"]["approval_id"] = "forged-approval"
    elif change == "principal":
        trusted.effective_principal["subject"] = "another-owner"
    else:
        stop["kind"] = "beat"
        del stop["stop_authority"]
    reply = executor.release_rpc(stop, "release-invalid", authenticated_controller=trusted)
    assert reply["status"] == 403
    validate_instance(reply, "rpc-envelope-v1.schema.json")
    assert systemd.calls == calls
    assert (tmp_path / "state.json").read_bytes() == before


@pytest.mark.parametrize("source,field,value", [
    ("systemd", "gpu_occupants", None),
    ("systemd", "gpu_tenants", ["unexplained-tenant"]),
    ("gpu", "unload", {"ok": False, "status": "failed"}),
    ("gpu", "probe", {"ok": False, "status": "timeout"}),
    ("gpu", "gpu_tenants", ["unexplained-tenant"]),
    ("systemd", "active", False),
])
def test_owner_pending_does_not_mask_uncertain_observations(tmp_path, source, field, value):
    class ObservationSystemd(DrainingSystemd):
        def inspect(self, unit, invocation):
            result = dict(super().inspect(unit, invocation))
            if source == "systemd":
                result[field] = value
            return result

    executor, systemd, gpu, trusted, stop = _prepare_owner_release(
        tmp_path, systemd=ObservationSystemd(),
    )
    systemd.instance("unit-a", "invoke-a").occupants = ["pid-draining"]
    observation = {"ok": True, "status": "ok", "gpu_tenants": []}
    if source == "gpu":
        observation[field] = value
    gpu.scripted.append(observation)
    reply = executor.release_rpc(stop, "release-uncertain", authenticated_controller=trusted)
    assert reply["status"] == 503
    validate_instance(reply, "rpc-envelope-v1.schema.json")
    record = json.loads((tmp_path / "state.json").read_text())["lanes"]["site-a/host-1/lane-gpu0"]
    assert record["state"] == "quarantined" and record["release_pending"] is False
    assert [call["method"] for call in systemd.calls] == ["start", "stop", "inspect"]


def test_owner_principal_cannot_fall_back_to_context_identity(tmp_path):
    executor, systemd, _, trusted, stop = _prepare_owner_release(tmp_path)
    trusted.effective_principal = None
    reply = executor.handle(stop, authenticated_controller=trusted)
    assert reply["ok"] is False
    assert [call["method"] for call in systemd.calls] == ["start"]


@pytest.mark.parametrize("change", ["authority", "policy", "kind"])
def test_pending_replay_rejects_changed_request(tmp_path, change):
    executor, systemd, _, trusted, stop, _ = _pending(tmp_path)
    calls = copy.deepcopy(systemd.calls)
    if change == "authority":
        stop["stop_authority"]["approval_id"] = "forged-approval"
    elif change == "policy":
        stop["execution_policy"]["max_end"] = "2026-09-28T00:00:00Z"
    else:
        stop["kind"] = "beat"
        del stop["stop_authority"]
    reply = executor.release_rpc(stop, "release-first", authenticated_controller=trusted)
    assert reply["status"] in {403, 409}
    assert systemd.calls == calls


@pytest.mark.parametrize("field", ["pipeline", "delegation", "batch"])
def test_local_action_rejects_missing_admission_fields(tmp_path, field):
    case = _load_p01()["local_action"]
    requested = copy.deepcopy(case["request"])
    del requested["admission"][field]
    assert_invalid(requested, "rpc-ops-v1.schema.json")
    with pytest.raises(LocalActionError):
        canonical_local_action_payload(
            requested, destination_site=case["destination_site"],
            controller_id=case["controller_id"],
        )


@pytest.mark.parametrize("op,args", [
    ("acquire", {"purpose": "work", "class": "batch", "est_s": 1, "max_s": 2, "booking_id": 123}),
    ("acquire", {"purpose": "work", "class": "batch", "est_s": 1, "max_s": 2, "queue_id": []}),
    ("acquire", {"purpose": "x" * 513, "class": "batch", "est_s": 1, "max_s": 2}),
    ("release", {"token": "t" * 513}),
    ("claim", {"token": "t" * 16, "generation": 1, "generation_source": "authenticated-adoption", "instance": []}),
    ("claim", {"token": "t" * 513, "generation": 1, "generation_source": "authenticated-adoption"}),
    ("book", {"start": "not-a-time", "end": "not-a-time", "purpose": "work"}),
    ("book", {"start": "2026-09-28T00:00:00+01:00", "end": "2026-09-28T01:00:00+01:00", "purpose": "work"}),
])
def test_local_action_rejects_invalid_execution_args(op, args):
    case = _load_p01()["local_action"]
    requested = copy.deepcopy(case["request"])
    requested.update(op=op, args=args)
    assert_invalid(requested, "rpc-ops-v1.schema.json")
    with pytest.raises(LocalActionError):
        canonical_local_action_payload(
            requested, destination_site=case["destination_site"],
            controller_id=case["controller_id"],
        )


@pytest.mark.parametrize("field,value", [("approval", "bad id"), ("pipeline", "x" * 513)])
def test_local_action_rejects_invalid_admission_values(field, value):
    case = _load_p01()["local_action"]
    requested = copy.deepcopy(case["request"])
    if field == "approval":
        requested["admission"][field].update(approval_id=value, required=True)
    else:
        requested["admission"][field]["purpose"] = value
    assert_invalid(requested, "rpc-ops-v1.schema.json")
    with pytest.raises(LocalActionError):
        canonical_local_action_payload(
            requested, destination_site=case["destination_site"],
            controller_id=case["controller_id"],
        )


def test_pending_release_restart_and_wait_boundary(tmp_path):
    executor, systemd, gpu, trusted, stop, pending = _pending(tmp_path)
    restarted = Executor(executor.clock, systemd, gpu, tmp_path / "state.json", trusted)
    calls = copy.deepcopy(systemd.calls)
    assert restarted.release_rpc(stop, "release-first", authenticated_controller=trusted) == pending
    assert systemd.calls == calls
    restarted.clock.advance(utc_s=599, monotonic_s=599)
    gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})
    assert restarted.release_rpc(stop, "release-before", authenticated_controller=trusted)["status"] == 202
    restarted.clock.advance(utc_s=1, monotonic_s=1)
    gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})
    reply = restarted.release_rpc(stop, "release-at", authenticated_controller=trusted)
    assert reply["status"] == 503
    assert restarted.state_snapshot(LANE)["state"] == "quarantined"
    assert restarted.release_rpc(stop, "release-first", authenticated_controller=trusted) == pending
    assert len([call for call in systemd.calls if call["method"] == "stop"]) == 1


def test_pending_replay_survives_successor_reservation(tmp_path):
    executor, systemd, gpu, trusted, stop, pending = _pending(tmp_path)
    systemd.instance("unit-a", "invoke-a").occupants = []
    gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})
    assert executor.release_rpc(stop, "release-complete", authenticated_controller=trusted)["status"] == 200
    successor = copy.deepcopy(stop["identity"])
    successor.update(generation=8, token="successor-token-value", instance="successor-instance", unit=None, invocation=None)
    assert executor.handle(request("reserve", successor, stop["execution_policy"]), authenticated_controller=trusted)["ok"] is True
    restarted = Executor(executor.clock, systemd, gpu, tmp_path / "state.json", trusted)
    before = (tmp_path / "state.json").read_bytes()
    calls = copy.deepcopy(systemd.calls)
    assert restarted.release_rpc(stop, "release-first", authenticated_controller=trusted) == pending
    assert (tmp_path / "state.json").read_bytes() == before
    assert systemd.calls == calls
    assert restarted.state_snapshot(LANE)["generation"] == 8


def test_owner_wait_expires_while_start_is_pending(tmp_path):
    systemd = DelayedStart()
    executor, clock, _, gpu, trusted = make_executor(tmp_path, systemd=systemd, operation_timeout_s=1)
    running = identity(unit="unit-a", invocation="invoke-a")
    assert executor.handle(request("reserve", identity(), policy()), authenticated_controller=trusted)["ok"] is True
    start_replies = []
    thread = threading.Thread(target=lambda: start_replies.append(executor.handle(
        request("start", running, policy(), reservation_acknowledged=True, workload=workload()),
        authenticated_controller=trusted,
    )))
    thread.start()
    try:
        assert systemd.entered.wait(1)
        stop = request("stop", running, policy(), stop_authority={"mode": "owner-release", "approval_id": None})
        assert executor.release_rpc(stop, "release-first", authenticated_controller=trusted)["status"] == 202
        clock.advance(utc_s=600, monotonic_s=600)
        expired = executor.release_rpc(stop, "release-at", authenticated_controller=trusted)
        assert expired["status"] == 503
        assert executor.state_snapshot(LANE)["state"] == "quarantined"
        assert not [call for call in systemd.calls if call["method"] == "stop"]
    finally:
        gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})
        systemd.resume.set()
        thread.join(3)
    assert not thread.is_alive()
    assert start_replies[0]["ok"] is False
    assert executor.state_snapshot(LANE)["state"] == "free"
    assert [call["method"] for call in systemd.calls] == ["start", "stop", "inspect"]
