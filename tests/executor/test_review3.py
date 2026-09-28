"""Third-review regressions exercised through the real executor with fakes."""

import json

import pytest

from flightctl.executor import Executor
from tests.executor.test_executor import (
    LANE_A, IsolatedSystemd, identity, make_executor, policy, request, start_one,
    stop_one, workload,
)


@pytest.mark.parametrize("source,field,value", [
    ("systemd", "occupants", ["pid-remains"]),
    ("systemd", "occupants", [123]),
    ("systemd", "gpu_tenants", ["tenant-remains"]),
    ("gpu", "tenants", ["tenant-remains"]),
    ("gpu", "active_tenants", [123]),
    ("gpu", "occupants", None),
])
def test_cleanup_checks_every_occupancy_field(tmp_path, source, field, value):
    class ObservationSystemd(IsolatedSystemd):
        def inspect(self, unit, invocation):
            result = dict(super().inspect(unit, invocation))
            if source == "systemd":
                result[field] = value
            return result

    executor, _, systemd, gpu, trusted = make_executor(tmp_path, systemd=ObservationSystemd())
    _, running = start_one(executor, trusted)
    observation = {"ok": True, "status": "ok", "gpu_tenants": []}
    if source == "gpu":
        observation[field] = value
    gpu.scripted.append(observation)
    reply = stop_one(executor, trusted, running)
    assert reply["ok"] is False and reply["uncertain"] is True
    persisted = json.loads((tmp_path / "executor-state.json").read_text())
    record = persisted["lanes"]["site-a/host-1/lane-gpu0"]
    assert record["state"] == "quarantined" and record["closed_generation"] == 7
    assert [call["method"] for call in systemd.calls] == ["start", "stop", "inspect"]
    assert gpu.calls == [{"host": "host-1"}]


def test_heartbeat_does_not_extend_max_end_grace(tmp_path):
    executor, clock, systemd, gpu, trusted = make_executor(tmp_path)
    pol = policy("service")
    _, running = start_one(executor, trusted, pol=pol, deadline_s=1)
    clock.advance(utc_s=1, monotonic_s=1)
    assert executor.enforce_deadlines()[-1]["ok"] is False
    clock.advance(utc_s=119, monotonic_s=119)
    assert executor.handle(request("beat", running, pol), authenticated_controller=trusted)["ok"] is True
    assert executor.enforce_deadlines()[-1]["ok"] is False
    assert [call["method"] for call in systemd.calls] == ["start"]
    clock.advance(utc_s=1, monotonic_s=1)
    gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})
    assert executor.enforce_deadlines()[-1]["ok"] is True
    assert executor.state_snapshot(LANE_A)["state"] == "free"
    assert [call["method"] for call in systemd.calls] == ["start", "stop", "inspect"]


def test_start_completion_cannot_acknowledge_released_generation(tmp_path):
    class CompletionBoundaryExecutor(Executor):
        def _mark_start_timeout(self, generation_key, ident):
            timed_out = super()._mark_start_timeout(generation_key, ident)
            # Schedule a controller stop after the worker completes and before
            # the initiating request commits its result.
            self.stop_reply = stop_one(self, trusted, dict(ident))
            return timed_out

    base, _, systemd, gpu, trusted = make_executor(tmp_path)
    executor = CompletionBoundaryExecutor(
        base.clock, systemd, gpu, tmp_path / "executor-state.json", trusted,
    )
    reserved = identity()
    running = identity(unit="unit-a", invocation="invoke-a")
    assert executor.handle(request("reserve", reserved, policy()), authenticated_controller=trusted)["ok"] is True
    gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})
    reply = executor.handle(
        request("start", running, policy(), reservation_acknowledged=True, workload=workload()),
        authenticated_controller=trusted,
    )
    assert executor.stop_reply["ok"] is True
    assert executor.state_snapshot(LANE_A)["state"] == "free"
    assert reply["ok"] is False
    assert not systemd.instance("unit-a", "invoke-a").started
