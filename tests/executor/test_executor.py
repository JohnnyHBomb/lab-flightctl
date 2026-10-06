from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from flightctl.executor import Executor, TrustedController
from tests.executor.systemd_adapter import IsolatedSystemd
from tests.fakes import FakeClock, FakeGPUProbe
from tests.fakes.ssh import FakeTransport
from tests.contracts.validation import validate_instance


LANE_A = {"site_id": "site-a", "host_id": "host-1", "lane_id": "lane-gpu0"}
LANE_B = {"site_id": "site-a", "host_id": "host-1", "lane_id": "lane-gpu1"}


def policy(owner_class: str = "batch", *, protected: bool = False, preemptible: bool = True, grace_s: int = 0) -> dict[str, object]:
    return {"class": owner_class, "protected": protected, "preemptible": preemptible, "grace_s": grace_s, "max_end": "2026-09-27T21:00:00Z", "deadline_kind": "max-end"}


def identity(lane: dict[str, str] = LANE_A, *, generation: int = 7, token: str = "token-abcdefghijklmnop", instance: str = "instance-a", unit: str | None = None, invocation: str | None = None, boot_id: str = "boot-a", deadline_s: float = 100.0) -> dict[str, object]:
    return {"lane": copy.deepcopy(lane), "generation": generation, "token": token, "instance": instance, "unit": unit, "invocation": invocation, "deadline": {"kind": "max-end", "owner_class": "batch", "boot_id": boot_id, "deadline_s": deadline_s, "utc_anchor": "2026-09-27T20:00:00Z", "monotonic_anchor_s": 0}}


def inspect_identity(lane: dict[str, str] = LANE_A, *, generation: int = 7, instance: str = "instance-a") -> dict[str, object]:
    return {"lane": copy.deepcopy(lane), "generation": generation, "token": None, "instance": instance, "unit": None, "invocation": None, "deadline": None}


def workload(owner_class: str = "batch", workload_id: str = "job-a") -> dict[str, object]:
    return {"workload_id": workload_id, "class": owner_class, "image_digest": "a" * 64, "parameters_hash": "b" * 64, "manifest_hash": None}


def request(kind: str, ident: dict[str, object], pol: dict[str, object], request_id: str | None = None, **extra: object) -> dict[str, object]:
    value: dict[str, object] = {"schema_version": 1, "kind": kind, "controller_request_id": request_id or f"request-{kind}", "execution_policy": copy.deepcopy(pol), "identity": copy.deepcopy(ident)}
    value.update(extra)
    return value


def make_executor(tmp_path: Path, *, clock: FakeClock | None = None, systemd: IsolatedSystemd | None = None, gpu: FakeGPUProbe | None = None, controller: object | None = None, operation_timeout_s: float = 0.2) -> tuple[Executor, FakeClock, IsolatedSystemd, FakeGPUProbe, object]:
    actual_clock = clock or FakeClock()
    actual_systemd = systemd or IsolatedSystemd()
    actual_gpu = gpu or FakeGPUProbe()
    trusted = controller if controller is not None else object()
    executor = Executor(actual_clock, actual_systemd, actual_gpu, tmp_path / "executor-state.json", trusted, operation_timeout_s=operation_timeout_s)
    return executor, actual_clock, actual_systemd, actual_gpu, trusted


def start_one(executor: Executor, trusted: object, *, lane: dict[str, str] = LANE_A, pol: dict[str, object] | None = None, generation: int = 7, token: str = "token-abcdefghijklmnop", instance_name: str = "instance-a", unit: str = "unit-a", invocation: str = "invoke-a", deadline_s: float = 100.0) -> tuple[dict[str, object], dict[str, object]]:
    actual_policy = pol or policy()
    reserved = identity(lane, generation=generation, token=token, instance=instance_name, deadline_s=deadline_s)
    running = identity(lane, generation=generation, token=token, instance=instance_name, unit=unit, invocation=invocation, deadline_s=deadline_s)
    assert executor.handle(request("reserve", reserved, actual_policy), authenticated_controller=trusted)["ok"] is True
    reply = executor.handle(request("start", running, actual_policy, reservation_acknowledged=True, workload=workload(str(actual_policy["class"]))), authenticated_controller=trusted)
    assert reply["ok"] is True
    return reserved, running


def stop_one(executor: Executor, trusted: object, running: dict[str, object]) -> dict[str, object]:
    return executor.handle(request("stop", running, policy(), stop_authority={"mode": "controller-match", "approval_id": None}), authenticated_controller=trusted)


def test_reserve_start_fence(tmp_path: Path) -> None:
    executor, _, systemd, _, trusted = make_executor(tmp_path)
    pol = policy()
    reserved = identity()
    running = identity(unit="unit-a", invocation="invoke-a")
    reply = executor.handle(request("reserve", reserved, pol), authenticated_controller=trusted)
    assert reply["ok"] is True and reply["acknowledgement"] == "reserved"
    persisted = json.loads((tmp_path / "executor-state.json").read_text(encoding="utf-8"))
    lane_record = persisted["lanes"]["site-a/host-1/lane-gpu0"]
    assert lane_record["generation"] == 7 and lane_record["state"] == "starting"
    assert lane_record["closed_generation"] == 0

    started = executor.handle(request("start", running, pol, reservation_acknowledged=True, workload=workload()), authenticated_controller=trusted)
    assert started["ok"] is True and len([call for call in systemd.calls if call["method"] == "start"]) == 1
    assert started["echoed_identity"] == running

    before = len(systemd.calls)
    duplicate_token = identity(LANE_B, generation=8, token=reserved["token"], instance="instance-b")
    assert executor.handle(request("reserve", duplicate_token, pol), authenticated_controller=trusted)["ok"] is False
    before_start = identity(LANE_B, unit="unit-b", invocation="invoke-b")
    assert executor.handle(request("start", before_start, pol, reservation_acknowledged=True, workload=workload()), authenticated_controller=trusted)["ok"] is False
    assert len(systemd.calls) == before

    wrong_token = identity(token="token-qrstuvwxyz1234", unit="unit-a", invocation="invoke-a")
    wrong_instance = identity(instance="instance-b", unit="unit-a", invocation="invoke-a")
    assert executor.handle(request("start", wrong_token, pol, reservation_acknowledged=True, workload=workload()), authenticated_controller=trusted)["ok"] is False
    assert executor.handle(request("start", wrong_instance, pol, reservation_acknowledged=True, workload=workload()), authenticated_controller=trusted)["ok"] is False
    assert len([call for call in systemd.calls if call["method"] == "start"]) == 1
    assert executor.handle(request("start", running, pol, reservation_acknowledged=True, workload=workload()), authenticated_controller=object())["ok"] is False
    assert len([call for call in systemd.calls if call["method"] == "start"]) == 1

    gpu = FakeGPUProbe(scripted=[{"ok": True, "status": "ok", "gpu_tenants": []}])
    executor.gpu_probe = gpu
    stopped = executor.handle(request("stop", running, pol, stop_authority={"mode": "controller-match", "approval_id": None}), authenticated_controller=trusted)
    assert stopped["ok"] is True and executor.state_snapshot(LANE_A)["closed_generation"] == 7
    late = executor.handle(request("start", running, pol, reservation_acknowledged=True, workload=workload()), authenticated_controller=trusted)
    assert late["ok"] is False
    assert len([call for call in systemd.calls if call["method"] == "start"]) == 1
    stale_new_token = identity(generation=7, token="token-stale-abcdefgh")
    assert executor.handle(request("reserve", stale_new_token, pol), authenticated_controller=trusted)["ok"] is False
    old_token_new_generation = identity(generation=8, token=reserved["token"])
    assert executor.handle(request("reserve", old_token_new_generation, pol), authenticated_controller=trusted)["ok"] is False


class DelayedStart(IsolatedSystemd):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.resume = threading.Event()
        self.completed = threading.Event()

    def start(self, unit: str, invocation: str):
        self.entered.set()
        assert self.resume.wait(2)
        try:
            return super().start(unit, invocation)
        finally:
            self.completed.set()


def test_pending_start_stop_serializes_host_effects(tmp_path: Path) -> None:
    systemd = DelayedStart()
    executor, _, _, gpu, trusted = make_executor(tmp_path, systemd=systemd, operation_timeout_s=1)
    running = identity(unit="unit-pending", invocation="invoke-pending")
    assert executor.handle(request("reserve", identity(), policy()), authenticated_controller=trusted)["ok"] is True
    start_replies: list[dict[str, object]] = []
    thread = threading.Thread(
        target=lambda: start_replies.append(
            executor.handle(request("start", running, policy(), reservation_acknowledged=True, workload=workload()), authenticated_controller=trusted)
        )
    )
    thread.start()
    assert systemd.entered.wait(1)
    gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})
    queued_stop = executor.handle(
        request("stop", running, policy(), stop_authority={"mode": "controller-match", "approval_id": None}),
        authenticated_controller=trusted,
    )
    assert queued_stop["ok"] is False and queued_stop["uncertain"] is True
    assert not [call for call in systemd.calls if call["method"] == "stop"]
    assert executor.state_snapshot(LANE_A)["state"] == "stopping"
    systemd.resume.set()
    thread.join(3)
    assert not thread.is_alive()
    assert start_replies[0]["ok"] is False
    assert [call["method"] for call in systemd.calls] == ["start", "stop", "inspect"]
    assert executor.state_snapshot(LANE_A)["state"] == "free"
    persisted = json.loads((tmp_path / "executor-state.json").read_text(encoding="utf-8"))
    assert persisted["lanes"]["site-a/host-1/lane-gpu0"]["state"] == "free"


def test_deadline_stop_serializes_pending_start(tmp_path: Path) -> None:
    systemd = DelayedStart()
    executor, clock, _, gpu, trusted = make_executor(tmp_path, systemd=systemd, operation_timeout_s=1)
    reserved = identity(deadline_s=2)
    running = identity(unit="unit-deadline", invocation="invoke-deadline", deadline_s=2)
    assert executor.handle(request("reserve", reserved, policy()), authenticated_controller=trusted)["ok"] is True
    start_replies: list[dict[str, object]] = []
    thread = threading.Thread(
        target=lambda: start_replies.append(
            executor.handle(request("start", running, policy(), reservation_acknowledged=True, workload=workload()), authenticated_controller=trusted)
        )
    )
    thread.start()
    assert systemd.entered.wait(1)
    clock.advance(monotonic_s=2)
    gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})
    tick = executor.enforce_deadlines()
    assert tick and tick[-1]["ok"] is False
    assert not [call for call in systemd.calls if call["method"] == "stop"]
    assert executor.state_snapshot(LANE_A)["state"] == "stopping"
    systemd.resume.set()
    thread.join(3)
    assert not thread.is_alive()
    assert start_replies[0]["ok"] is False
    assert systemd.calls == [
        {"method": "start", "unit": "unit-deadline", "invocation": "invoke-deadline"},
        {"method": "stop", "unit": "unit-deadline", "invocation": "invoke-deadline"},
        {"method": "inspect", "unit": "unit-deadline", "invocation": "invoke-deadline"},
    ]
    assert executor.state_snapshot(LANE_A)["state"] == "free"


def test_timed_out_start_retains_fence_until_worker_returns(tmp_path: Path) -> None:
    systemd = DelayedStart()
    executor, _, _, gpu, trusted = make_executor(tmp_path, systemd=systemd, operation_timeout_s=0.02)
    running = identity(unit="unit-timeout", invocation="invoke-timeout")
    assert executor.handle(request("reserve", identity(), policy()), authenticated_controller=trusted)["ok"] is True
    systemd.queue("unit-timeout", "invoke-timeout", "inspect", "success", state="inactive", active=False)
    timed_out = executor.handle(
        request("start", running, policy(), reservation_acknowledged=True, workload=workload()),
        authenticated_controller=trusted,
    )
    assert timed_out["ok"] is False and timed_out["uncertain"] is True
    gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})
    queued = executor.handle(
        request("stop", running, policy(), stop_authority={"mode": "controller-match", "approval_id": None}),
        authenticated_controller=trusted,
    )
    assert queued["ok"] is False and queued["uncertain"] is True
    assert not [call for call in systemd.calls if call["method"] == "stop"]
    assert executor.state_snapshot(LANE_A)["state"] == "stopping"
    systemd.resume.set()
    assert systemd.completed.wait(1)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and executor.state_snapshot(LANE_A)["state"] != "free":
        time.sleep(0.01)
    assert executor.state_snapshot(LANE_A)["state"] == "free"
    assert [call["method"] for call in systemd.calls] == ["start", "stop", "inspect"]


def test_unique_unit_and_invocation_ownership(tmp_path: Path) -> None:
    executor, _, systemd, _, trusted = make_executor(tmp_path)
    _, running_a = start_one(executor, trusted)
    reserved_b = identity(LANE_B, generation=8, token="token-bbbbbbbbbbbbbbbb", instance="instance-b")
    running_b = identity(LANE_B, generation=8, token="token-bbbbbbbbbbbbbbbb", instance="instance-b", unit="unit-a", invocation="invoke-a")
    assert executor.handle(request("reserve", reserved_b, policy()), authenticated_controller=trusted)["ok"] is True
    denied = executor.handle(request("start", running_b, policy(), reservation_acknowledged=True, workload=workload()), authenticated_controller=trusted)
    assert denied["ok"] is False and denied["acknowledgement"] == "started"
    assert [call["method"] for call in systemd.calls].count("start") == 1
    assert executor.state_snapshot(LANE_A)["state"] == "running"
    assert executor.state_snapshot(LANE_B)["state"] == "starting"
    assert (running_a["unit"], running_a["invocation"]) == (running_b["unit"], running_b["invocation"])


def test_idempotent_start_recovery(tmp_path: Path) -> None:
    executor, _, systemd, _, trusted = make_executor(tmp_path)
    running = identity(unit="unit-a", invocation="invoke-a")
    systemd.queue("unit-a", "invoke-a", "start", "lost")
    systemd.queue("unit-a", "invoke-a", "inspect", "success", state="running")
    first = executor.handle(request("reserve", identity(), policy()), authenticated_controller=trusted)
    assert first["ok"] is True
    recovered = executor.handle(request("start", running, policy(), reservation_acknowledged=True, workload=workload()), authenticated_controller=trusted)
    assert recovered["ok"] is True
    calls_after_recovery = list(systemd.calls)
    duplicate = executor.handle(request("start", running, policy(), reservation_acknowledged=True, workload=workload()), authenticated_controller=trusted)
    assert duplicate["ok"] is True
    assert systemd.calls == calls_after_recovery + [{"method": "inspect", "unit": "unit-a", "invocation": "invoke-a"}]
    assert [call["method"] for call in systemd.calls] == ["start", "inspect", "inspect"]


def test_restart_after_lost_start_reply_reconciles_without_relaunch(tmp_path: Path) -> None:
    executor, _, systemd, _, trusted = make_executor(tmp_path)
    running = identity(unit="unit-restart", invocation="invoke-restart")
    systemd.queue("unit-restart", "invoke-restart", "start", "lost")
    systemd.queue("unit-restart", "invoke-restart", "inspect", "timeout")
    assert executor.handle(request("reserve", identity(), policy()), authenticated_controller=trusted)["ok"] is True
    first = executor.handle(request("start", running, policy(), reservation_acknowledged=True, workload=workload()), authenticated_controller=trusted)
    assert first["ok"] is False and executor.state_snapshot(LANE_A)["state"] == "quarantined"

    systemd.queue("unit-restart", "invoke-restart", "inspect", "success", state="running")
    restarted = Executor(executor.clock, systemd, executor.gpu_probe, tmp_path / "executor-state.json", trusted)
    recovered = restarted.handle(request("start", running, policy(), reservation_acknowledged=True, workload=workload()), authenticated_controller=trusted)
    assert recovered["ok"] is True
    assert [call["method"] for call in systemd.calls].count("start") == 1


def test_crash_after_host_start_inspects_persisted_pending_start(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    child = """
import os
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from tests.executor import test_executor as t

class CrashAfterStart(t.IsolatedSystemd):
    def start(self, unit, invocation):
        super().start(unit, invocation)
        os._exit(42)

executor, _, _, _, trusted = t.make_executor(Path(sys.argv[2]), systemd=CrashAfterStart())
t.start_one(executor, trusted)
"""
    crashed = subprocess.run([sys.executable, "-B", "-c", child, str(root), str(tmp_path)], cwd=root, timeout=5)
    assert crashed.returncode == 42

    systemd = IsolatedSystemd()
    systemd.start("unit-a", "invoke-a")
    before_restart = len(systemd.calls)
    executor, _, _, _, trusted = make_executor(tmp_path, systemd=systemd)
    running = identity(unit="unit-a", invocation="invoke-a")
    recovered = executor.handle(request("start", running, policy(), reservation_acknowledged=True, workload=workload()), authenticated_controller=trusted)
    assert recovered["ok"] is True
    assert systemd.calls[before_restart:] == [{"method": "inspect", "unit": "unit-a", "invocation": "invoke-a"}]
    assert executor.state_snapshot(LANE_A)["state"] == "running"


def test_transport_reply_loss_retries_durable_reservation(tmp_path: Path) -> None:
    executor, _, systemd, _, trusted = make_executor(tmp_path)
    pol = policy()
    reserved = identity()
    first = executor.handle(request("reserve", reserved, pol), authenticated_controller=trusted)
    transport = FakeTransport([{"outcome": "lost"}])
    lost = transport.request("executor", {"reply": first}, 1)
    assert lost["status"] == "lost"
    retry = executor.handle(request("reserve", reserved, pol, request_id="retry-reserve"), authenticated_controller=trusted)
    assert retry["ok"] is True and retry["acknowledgement"] == "reserved"
    assert not [call for call in systemd.calls if call["method"] == "start"]


def test_runtime_replies_validate_against_frozen_executor_schema(tmp_path: Path) -> None:
    executor, _, _, gpu, trusted = make_executor(tmp_path)
    pol = policy()
    reserved = identity()
    running = identity(unit="unit-schema", invocation="invoke-schema")
    replies = [
        executor.handle(request("reserve", reserved, pol), authenticated_controller=trusted),
        executor.handle(request("start", running, pol, reservation_acknowledged=True, workload=workload()), authenticated_controller=trusted),
    ]
    gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})
    replies.append(executor.handle(request("stop", running, pol, stop_authority={"mode": "controller-match", "approval_id": None}), authenticated_controller=trusted))
    for reply in replies:
        validate_instance(reply, "executor-v1.schema.json")


def test_rejected_start_reply_matches_frozen_schema(tmp_path: Path) -> None:
    executor, _, _, _, trusted = make_executor(tmp_path)
    reply = executor.handle(
        request("start", identity(unit="unit-unreserved", invocation="invoke-unreserved"), policy(), reservation_acknowledged=True, workload=workload()),
        authenticated_controller=trusted,
    )
    validate_instance(reply, "executor-v1.schema.json")
    assert reply["ok"] is False and reply["acknowledgement"] == "started"


def test_pid_reuse_and_invocation(tmp_path: Path) -> None:
    executor, _, systemd, _, trusted = make_executor(tmp_path)
    _, running = start_one(executor, trusted)
    reused = identity(unit="unit-a", invocation="invoke-new")
    before = list(systemd.calls)
    reply = executor.handle(request("stop", reused, policy(), stop_authority={"mode": "controller-match", "approval_id": None}), authenticated_controller=trusted)
    assert reply["ok"] is False
    assert systemd.calls == before
    assert executor.state_snapshot(LANE_A)["state"] == "running"


def test_stop_without_lock_deadlock(tmp_path: Path) -> None:
    executor, _, systemd, gpu, trusted = make_executor(tmp_path)
    _, running = start_one(executor, trusted)
    gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})
    observed: list[str] = []

    def reenter(unit: str, invocation: str) -> None:
        observed.append(str(executor.state_snapshot(LANE_A)["state"]))

    systemd.on_stop = reenter
    reply = executor.handle(request("stop", running, policy(), stop_authority={"mode": "controller-match", "approval_id": None}), authenticated_controller=trusted)
    assert reply["ok"] is True
    assert observed == ["stopping"]
    assert [call["method"] for call in systemd.calls] == ["start", "stop", "inspect"]
    assert gpu.calls == [{"host": "host-1"}]
    assert executor.state_snapshot(LANE_A)["state"] == "free"


def test_stop_timeout_is_bounded_and_retains_fence(tmp_path: Path) -> None:
    executor, _, systemd, gpu, trusted = make_executor(tmp_path, operation_timeout_s=0.02)
    _, running = start_one(executor, trusted)
    gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})
    entered = threading.Event()

    def blocking_stop(unit: str, invocation: str) -> None:
        entered.set()
        time.sleep(0.08)

    systemd.on_stop = blocking_stop
    start = time.monotonic()
    reply = executor.handle(request("stop", running, policy(), stop_authority={"mode": "controller-match", "approval_id": None}), authenticated_controller=trusted)
    elapsed = time.monotonic() - start
    assert entered.is_set() and elapsed < 0.07
    assert reply["ok"] is False and executor.state_snapshot(LANE_A)["state"] == "quarantined"


def test_cleanup_unknown_and_other_unit_untouched(tmp_path: Path) -> None:
    executor, _, systemd, gpu, trusted = make_executor(tmp_path)
    _, running_a = start_one(executor, trusted)
    _, running_b = start_one(executor, trusted, lane=LANE_B, generation=8, token="token-bbbbbbbbbbbbbbbb", instance_name="instance-b", unit="unit-b", invocation="invoke-b")
    systemd.queue("unit-a", "invoke-a", "stop", "failed_stop")
    gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})
    failed = executor.handle(request("stop", running_a, policy(), stop_authority={"mode": "controller-match", "approval_id": None}), authenticated_controller=trusted)
    assert failed["ok"] is False and failed["uncertain"] is True
    assert executor.state_snapshot(LANE_A)["state"] == "quarantined"
    assert executor.state_snapshot(LANE_B)["state"] == "running"
    assert not [call for call in systemd.calls if call["method"] == "stop" and call["unit"] == "unit-b"]

    # Missing/partial GPU evidence cannot release an otherwise empty cgroup.
    executor2, _, systemd2, gpu2, trusted2 = make_executor(tmp_path / "partial")
    _, running2 = start_one(executor2, trusted2, unit="unit-c", invocation="invoke-c")
    gpu2.scripted.append({"ok": True, "status": "ok"})
    partial = executor2.handle(request("stop", running2, policy(), stop_authority={"mode": "controller-match", "approval_id": None}), authenticated_controller=trusted2)
    assert partial["ok"] is False and executor2.state_snapshot(LANE_A)["state"] == "quarantined"


def test_cleanup_rejects_unknown_status_and_malformed_occupants(tmp_path: Path) -> None:
    gpu_cases = (
        {"ok": True, "status": "unrecognised-status", "gpu_tenants": []},
        {"ok": True, "status": "ok", "gpu_tenants": [1234]},
    )
    for index, gpu_result in enumerate(gpu_cases):
        executor, _, _, gpu, trusted = make_executor(tmp_path / f"gpu-{index}")
        _, running = start_one(executor, trusted)
        gpu.scripted.append(gpu_result)
        reply = stop_one(executor, trusted, running)
        assert reply["ok"] is False and reply["uncertain"] is True
        assert executor.state_snapshot(LANE_A)["state"] == "quarantined"

    executor, _, systemd, gpu, trusted = make_executor(tmp_path / "cgroup")
    _, running = start_one(executor, trusted)
    systemd.queue("unit-a", "invoke-a", "inspect", "success", cgroup_occupants=[1234])
    gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})
    reply = stop_one(executor, trusted, running)
    assert reply["ok"] is False and reply["uncertain"] is True
    assert executor.state_snapshot(LANE_A)["state"] == "quarantined"


def test_successful_stop_can_report_pre_cleanup_occupants(tmp_path: Path) -> None:
    executor, _, systemd, gpu, trusted = make_executor(tmp_path)
    _, running = start_one(executor, trusted)
    systemd.instance("unit-a", "invoke-a").occupants = ["pid-before-stop"]
    gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})
    reply = stop_one(executor, trusted, running)
    assert reply["ok"] is True and reply["observed_state"] == "free"
    assert reply["cgroup_occupants"] == [] and reply["gpu_tenants"] == []
    assert executor.state_snapshot(LANE_A)["state"] == "free"


class ContradictoryInspectSystemd(IsolatedSystemd):
    def inspect(self, unit: str, invocation: str):
        result = dict(super().inspect(unit, invocation))
        result["invocation"] = "different-invocation"
        return result


class ObservingGPUProbe(FakeGPUProbe):
    def __init__(self, observed: list[str]) -> None:
        super().__init__()
        self.observed = observed
        self.executor: Executor | None = None

    def inspect(self, host: str):
        assert self.executor is not None
        self.observed.append(str(self.executor.state_snapshot(LANE_A)["state"]))
        return {"ok": True, "status": "ok"}


def test_cleanup_release_waits_for_independent_probes(tmp_path: Path) -> None:
    observed: list[str] = []
    gpu = ObservingGPUProbe(observed)
    executor, _, _, _, trusted = make_executor(tmp_path, gpu=gpu)
    gpu.executor = executor
    _, running = start_one(executor, trusted)
    reply = executor.handle(request("stop", running, policy(), stop_authority={"mode": "controller-match", "approval_id": None}), authenticated_controller=trusted)
    assert reply["ok"] is False and observed == ["stopping"]
    assert executor.state_snapshot(LANE_A)["state"] == "quarantined"


def test_cleanup_unknown_matrix(tmp_path: Path) -> None:
    cases = ("failed-unload", "remaining-cgroup", "gpu-tenant", "partial-gpu", "timeout", "lost", "unknown", "contradictory")
    for case in cases:
        systemd = ContradictoryInspectSystemd() if case == "contradictory" else IsolatedSystemd()
        gpu_result: dict[str, object] = {"ok": True, "status": "ok", "gpu_tenants": []}
        executor, _, _, gpu, trusted = make_executor(tmp_path / case, systemd=systemd)
        _, running = start_one(executor, trusted, unit=f"unit-{case}", invocation=f"invoke-{case}")
        unit = f"unit-{case}"
        invocation = f"invoke-{case}"
        if case == "failed-unload":
            gpu_result = {"ok": False, "status": "failure", "gpu_tenants": []}
        elif case == "remaining-cgroup":
            systemd.queue(unit, invocation, "inspect", "success", cgroup_occupants=["pid-remains"])
        elif case == "gpu-tenant":
            gpu_result = {"ok": True, "status": "ok", "gpu_tenants": ["tenant-remains"]}
        elif case == "partial-gpu":
            gpu_result = {"ok": True, "status": "ok"}
        elif case in {"timeout", "lost"}:
            systemd.queue(unit, invocation, "stop", case)
        elif case == "unknown":
            systemd.queue(unit, invocation, "stop", "unrecognised-script-token")
        gpu.scripted.append(gpu_result)
        reply = executor.handle(request("stop", running, policy(), stop_authority={"mode": "controller-match", "approval_id": None}), authenticated_controller=trusted)
        assert reply["ok"] is False and reply["uncertain"] is True and executor.state_snapshot(LANE_A)["state"] == "quarantined"
        assert not reply.get("error") is None


def test_deadlines_controller_loss_and_grace(tmp_path: Path) -> None:
    clock = FakeClock()
    executor, _, systemd, gpu, trusted = make_executor(tmp_path, clock=clock)
    protected = policy(protected=True, preemptible=False)
    _, running = start_one(executor, trusted, pol=protected, deadline_s=1)
    clock.advance(monotonic_s=1)
    protected_result = executor.enforce_deadlines()
    assert protected_result and executor.state_snapshot(LANE_A)["state"] == "quarantined"
    assert not [call for call in systemd.calls if call["method"] == "stop"]

    clock2 = FakeClock()
    executor2, _, systemd2, gpu2, trusted2 = make_executor(tmp_path / "evict", clock=clock2)
    evictable = policy("batch", protected=False, preemptible=True)
    _, running2 = start_one(executor2, trusted2, pol=evictable, deadline_s=10000)
    clock2.advance(monotonic_s=179)
    assert not [call for call in systemd2.calls if call["method"] == "stop"]
    clock2.advance(monotonic_s=1)
    gpu2.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})
    evicted = executor2.enforce_deadlines()
    assert evicted and evicted[-1]["ok"] is True
    assert [call["method"] for call in systemd2.calls].count("stop") == 1

    clock3 = FakeClock()
    executor3, _, systemd3, gpu3, trusted3 = make_executor(tmp_path / "service", clock=clock3)
    service = policy("service", protected=False, preemptible=True)
    _, running3 = start_one(executor3, trusted3, pol=service, unit="unit-s", invocation="invoke-s", deadline_s=1)
    clock3.advance(monotonic_s=1)
    assert executor3.enforce_deadlines()[-1]["ok"] is False
    assert not [call for call in systemd3.calls if call["method"] == "stop"]
    clock3.advance(monotonic_s=120)
    gpu3.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})
    assert executor3.enforce_deadlines()[-1]["ok"] is True
    assert [call["method"] for call in systemd3.calls].count("stop") == 1


def test_protected_stop_authority(tmp_path: Path) -> None:
    executor, _, systemd, gpu, trusted = make_executor(tmp_path)
    protected = policy(protected=True, preemptible=False)
    _, running = start_one(executor, trusted, pol=protected)
    denied = executor.handle(request("stop", running, protected, stop_authority={"mode": "controller-match", "approval_id": None}), authenticated_controller=trusted)
    assert denied["ok"] is False and executor.state_snapshot(LANE_A)["state"] == "quarantined"
    assert not [call for call in systemd.calls if call["method"] == "stop"]

    executor2, _, systemd2, gpu2, trusted2 = make_executor(tmp_path / "approved")
    _, running2 = start_one(executor2, trusted2, pol=protected, unit="unit-p", invocation="invoke-p")
    gpu2.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})
    approved = Executor(executor2.clock, systemd2, gpu2, tmp_path / "approved" / "executor-state.json", trusted2, approved_forced_preemptions={"approval-a"})
    allowed = approved.handle(request("stop", running2, protected, stop_authority={"mode": "approved-forced-preemption", "approval_id": "approval-a"}), authenticated_controller=trusted2)
    assert allowed["ok"] is True
    assert [call["method"] for call in systemd2.calls].count("stop") == 1

    executor3, _, systemd3, _, trusted3 = make_executor(tmp_path / "forged")
    _, running3 = start_one(executor3, trusted3, pol=protected, unit="unit-forged", invocation="invoke-forged")
    guarded = Executor(executor3.clock, systemd3, executor3.gpu_probe, tmp_path / "forged" / "executor-state.json", trusted3, approved_forced_preemptions={"approval-a"})
    forged = guarded.handle(request("stop", running3, protected, stop_authority={"mode": "approved-forced-preemption", "approval_id": "forged-reference"}), authenticated_controller=trusted3)
    assert forged["ok"] is False
    assert not [call for call in systemd3.calls if call["method"] == "stop"]


def test_clock_reboot_and_same_boot_deadline(tmp_path: Path) -> None:
    clock = FakeClock()
    executor, _, systemd, gpu, trusted = make_executor(tmp_path, clock=clock)
    _, running = start_one(executor, trusted, deadline_s=50)
    start_calls = len([call for call in systemd.calls if call["method"] == "start"])
    clock.jump_utc(31)
    new_reservation = identity(generation=8, token="token-cccccccccccccccc")
    frozen = executor.handle(request("reserve", new_reservation, policy()), authenticated_controller=trusted)
    assert frozen["ok"] is False and len([call for call in systemd.calls if call["method"] == "start"]) == start_calls
    clock.advance(monotonic_s=50)
    gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})
    deadline_result = executor.enforce_deadlines()
    assert deadline_result and executor.state_snapshot(LANE_A)["state"] == "free"

    clock2 = FakeClock()
    executor2, _, systemd2, gpu2, trusted2 = make_executor(tmp_path / "reboot", clock=clock2)
    _, running2 = start_one(executor2, trusted2, unit="unit-r", invocation="invoke-r", deadline_s=2)
    clock2.reboot(boot_id="boot-b", monotonic_s=0)
    no_kill = executor2.enforce_deadlines()
    assert no_kill == [] and executor2.state_snapshot(LANE_A)["reconcile_required"] is True
    assert not [call for call in systemd2.calls if call["method"] == "stop"]
    gpu2.scripted.append({"ok": True, "status": "ok", "gpu_tenants": ["tenant-r"]})
    reconciled = executor2.reconcile()
    assert reconciled["site-a/host-1/lane-gpu0"]["ok"] is True
    assert executor2.state_snapshot(LANE_A)["state"] == "running"


def test_reboot_reconcile_rejects_partial_and_reanchors_deadline(tmp_path: Path) -> None:
    partial_clock = FakeClock()
    partial, _, _, partial_gpu, partial_trusted = make_executor(tmp_path / "partial", clock=partial_clock)
    start_one(partial, partial_trusted, deadline_s=2)
    partial_clock.reboot(boot_id="boot-b")
    assert partial.enforce_deadlines() == []
    partial_gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": [], "complete": False})
    failed = partial.reconcile()["site-a/host-1/lane-gpu0"]
    assert failed["ok"] is False and failed["state"] == "quarantined"
    assert partial.state_snapshot(LANE_A)["state"] == "quarantined"

    clock = FakeClock()
    executor, _, systemd, gpu, trusted = make_executor(tmp_path / "valid", clock=clock)
    start_one(executor, trusted, deadline_s=2)
    clock.reboot(boot_id="boot-b")
    assert executor.enforce_deadlines() == []
    gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": [], "complete": True})
    reconciled = executor.reconcile()["site-a/host-1/lane-gpu0"]
    assert reconciled["ok"] is True
    assert executor.state_snapshot(LANE_A)["identity"]["deadline"]["boot_id"] == "boot-b"
    clock.advance(utc_s=1000, monotonic_s=1000)
    gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})
    ticks = executor.enforce_deadlines()
    assert ticks and ticks[-1]["ok"] is True
    assert [call["method"] for call in systemd.calls].count("stop") == 1
    assert executor.state_snapshot(LANE_A)["state"] == "free"


def test_repeated_reboots_preserve_absolute_deadline(tmp_path: Path) -> None:
    clock = FakeClock()
    executor, _, systemd, gpu, trusted = make_executor(tmp_path, clock=clock)
    start_one(executor, trusted, unit="unit-repeat", invocation="invoke-repeat", deadline_s=1000)

    clock.advance(utc_s=100, monotonic_s=100)
    clock.reboot(boot_id="boot-b")
    assert executor.enforce_deadlines() == []
    gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": [], "complete": True})
    first = executor.reconcile()["site-a/host-1/lane-gpu0"]
    assert first["ok"] is True
    first_deadline = executor.state_snapshot(LANE_A)["identity"]["deadline"]
    assert first_deadline["boot_id"] == "boot-b"
    assert first_deadline["deadline_s"] == 900
    assert first_deadline["utc_anchor"] == "2026-09-27T20:01:40Z"

    clock.advance(utc_s=100, monotonic_s=100)
    clock.reboot(boot_id="boot-c")
    assert executor.enforce_deadlines() == []
    gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": [], "complete": True})
    second = executor.reconcile()["site-a/host-1/lane-gpu0"]
    assert second["ok"] is True
    second_deadline = executor.state_snapshot(LANE_A)["identity"]["deadline"]
    assert second_deadline["boot_id"] == "boot-c"
    assert second_deadline["deadline_s"] == 800
    assert second_deadline["utc_anchor"] == "2026-09-27T20:03:20Z"

    clock.advance(utc_s=700, monotonic_s=700)
    current_identity = executor.state_snapshot(LANE_A)["identity"]
    heartbeat = executor.handle(request("beat", current_identity, policy()), authenticated_controller=trusted)
    assert heartbeat["ok"] is True
    gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})
    assert executor.enforce_deadlines() == []
    assert executor.state_snapshot(LANE_A)["state"] == "running"
    clock.advance(utc_s=100, monotonic_s=100)
    gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})
    assert executor.enforce_deadlines()[-1]["ok"] is True
    assert [call["method"] for call in systemd.calls].count("stop") == 1
    assert executor.state_snapshot(LANE_A)["state"] == "free"


def test_clock_skew_freezes_free_lane_admission(tmp_path: Path) -> None:
    clock = FakeClock()
    executor, _, systemd, _, trusted = make_executor(tmp_path, clock=clock)
    clock.jump_utc(31)
    denied = executor.handle(request("reserve", identity(LANE_B, generation=8, token="token-bbbbbbbbbbbbbbbb", instance="instance-b"), policy()), authenticated_controller=trusted)
    assert denied["ok"] is False
    assert not [call for call in systemd.calls if call["method"] == "start"]
    executor.resynchronise_clock()
    accepted = executor.handle(request("reserve", identity(LANE_B, generation=8, token="token-bbbbbbbbbbbbbbbb", instance="instance-b"), policy()), authenticated_controller=trusted)
    assert accepted["ok"] is True


def test_stale_thresholds_and_resident_standby_graces(tmp_path: Path) -> None:
    protected_clock = FakeClock()
    protected, _, protected_systemd, _, protected_trusted = make_executor(tmp_path / "protected", clock=protected_clock)
    start_one(protected, protected_trusted, pol=policy(protected=True, preemptible=False), deadline_s=10000)
    protected_clock.advance(monotonic_s=599)
    assert protected.enforce_deadlines() == []
    protected_clock.advance(monotonic_s=1)
    assert protected.enforce_deadlines()
    assert protected.state_snapshot(LANE_A)["state"] == "quarantined"
    assert not [call for call in protected_systemd.calls if call["method"] == "stop"]

    for owner_class, grace in (("resident", 120), ("standby", 300)):
        clock = FakeClock()
        executor, _, systemd, gpu, trusted = make_executor(tmp_path / owner_class, clock=clock)
        start_one(executor, trusted, pol=policy(owner_class), unit=f"unit-{owner_class}", invocation=f"invoke-{owner_class}", deadline_s=1)
        clock.advance(monotonic_s=1)
        first = executor.enforce_deadlines()
        assert first and first[-1]["ok"] is False
        clock.advance(monotonic_s=grace - 1)
        assert not [call for call in systemd.calls if call["method"] == "stop"]
        clock.advance(monotonic_s=1)
        gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})
        final = executor.enforce_deadlines()
        assert final and final[-1]["ok"] is True
        assert [call["method"] for call in systemd.calls].count("stop") == 1


def test_deadline_enforcement_calls_before_and_at_boundaries(tmp_path: Path) -> None:
    clock = FakeClock()
    executor, _, systemd, gpu, trusted = make_executor(tmp_path / "preemptible", clock=clock)
    start_one(executor, trusted, pol=policy("batch", protected=False, preemptible=True), deadline_s=10000)
    clock.advance(monotonic_s=179)
    assert executor.enforce_deadlines() == []
    assert not [call for call in systemd.calls if call["method"] == "stop"]
    clock.advance(monotonic_s=1)
    gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})
    assert executor.enforce_deadlines()[-1]["ok"] is True
    assert [call["method"] for call in systemd.calls].count("stop") == 1

    for owner_class, grace in (("service", 120), ("resident", 120), ("standby", 300)):
        clock = FakeClock()
        executor, _, systemd, gpu, trusted = make_executor(tmp_path / owner_class, clock=clock)
        start_one(executor, trusted, pol=policy(owner_class), unit=f"unit-{owner_class}", invocation=f"invoke-{owner_class}", deadline_s=1)
        clock.advance(monotonic_s=1)
        first = executor.enforce_deadlines()
        assert first and first[-1]["ok"] is False
        clock.advance(monotonic_s=grace - 1)
        before = executor.enforce_deadlines()
        assert before and before[-1]["ok"] is False
        assert not [call for call in systemd.calls if call["method"] == "stop"]
        clock.advance(monotonic_s=1)
        gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})
        final = executor.enforce_deadlines()
        assert final and final[-1]["ok"] is True
        assert [call["method"] for call in systemd.calls].count("stop") == 1
