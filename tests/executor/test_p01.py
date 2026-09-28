"""P0.1 vectors executed through the P2 production boundary."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from flightctl.executor import (
    Executor,
    LocalActionError,
    TrustedController,
    canonical_local_action_payload,
    content_labels_admitted,
    local_action_projection,
)
from tests.contracts.validation import assert_invalid, validate_instance
from tests.executor.systemd_adapter import IsolatedSystemd
from tests.executor.test_executor import identity, policy, request, workload
from tests.fakes import FakeClock, FakeGPUProbe


ROOT = Path(__file__).resolve().parents[2]
VECTORS = ROOT / "tests" / "contracts" / "vectors"
LANE = {"site_id": "site-a", "host_id": "host-1", "lane_id": "lane-gpu0"}
PRINCIPAL = {
    "site_id": "site-a",
    "tenant_id": "tenant-a",
    "issuer": "issuer-a",
    "subject": "subject-a",
}


def _load_p01() -> dict[str, object]:
    return json.loads((VECTORS / "p0-1.json").read_text(encoding="utf-8"))


def _pipeline_for(requested: dict[str, object]) -> dict[str, object]:
    pipeline = copy.deepcopy(requested["admission"]["pipeline"])
    pipeline["content_policy"] = ["acceptable-use"]
    return pipeline


def _executor(tmp_path: Path, *, systemd: IsolatedSystemd | None = None) -> tuple[Executor, IsolatedSystemd, FakeGPUProbe, TrustedController]:
    actual_systemd = systemd or IsolatedSystemd()
    gpu = FakeGPUProbe()
    clock = FakeClock()
    trusted = TrustedController("controller-a", effective_principal=PRINCIPAL)
    executor = Executor(clock, actual_systemd, gpu, tmp_path / "state.json", trusted, operation_timeout_s=0.2)
    return executor, actual_systemd, gpu, trusted


def _prepare_owner_release(tmp_path: Path, *, systemd: IsolatedSystemd | None = None):
    cases = _load_p01()["owner_release"]
    valid = copy.deepcopy(cases["valid"])
    executor, actual_systemd, gpu, trusted = _executor(tmp_path, systemd=systemd)
    reservation = copy.deepcopy(valid["identity"])
    reservation["unit"] = None
    reservation["invocation"] = None
    reserve = request("reserve", reservation, valid["execution_policy"], request_id="req-reserve")
    assert executor.handle(reserve, authenticated_controller=trusted)["ok"] is True
    start = copy.deepcopy(valid["identity"])
    started = request(
        "start",
        start,
        valid["execution_policy"],
        request_id="req-start",
        reservation_acknowledged=True,
        workload=workload("batch"),
    )
    assert executor.handle(started, authenticated_controller=trusted)["ok"] is True
    return executor, actual_systemd, gpu, trusted, valid


def test_owner_release_vector_uses_authenticated_owner_and_keeps_forced_preemption_distinct(tmp_path: Path) -> None:
    cases = _load_p01()["owner_release"]
    validate_instance(cases["valid"], "executor-v1.schema.json")
    executor, systemd, gpu, trusted, valid = _prepare_owner_release(tmp_path)
    gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})

    stopped = executor.handle(valid, authenticated_controller=trusted)
    assert stopped["ok"] is True
    assert executor.state_snapshot(LANE)["state"] == "free"
    assert [call["method"] for call in systemd.calls] == ["start", "stop", "inspect"]

    # The same protected stop authority cannot be fabricated with a forced-
    # preemption reference or a controller-match mode.
    for invalid in cases["invalid"]:
        assert_invalid(invalid, "executor-v1.schema.json")

    executor2, systemd2, _, trusted2, valid2 = _prepare_owner_release(tmp_path / "protected")
    forged_owner = copy.deepcopy(valid2)
    forged_owner["stop_authority"]["approval_id"] = "approval-a"
    denied = executor2.handle(forged_owner, authenticated_controller=trusted2)
    assert denied["ok"] is False and not [call for call in systemd2.calls if call["method"] == "stop"]

    executor3, systemd3, _, trusted3, valid3 = _prepare_owner_release(tmp_path / "match")
    controller_match = copy.deepcopy(valid3)
    controller_match["stop_authority"] = {"mode": "controller-match", "approval_id": None}
    denied_match = executor3.handle(controller_match, authenticated_controller=trusted3)
    assert denied_match["ok"] is False
    assert not [call for call in systemd3.calls if call["method"] == "stop"]


class DrainingSystemd(IsolatedSystemd):
    """A production-boundary fake whose verified stop leaves a live occupant."""

    def stop(self, unit: str, invocation: str):
        self.calls.append({"method": "stop", "unit": unit, "invocation": invocation})
        instance = self.instance(unit, invocation)
        return {
            "ok": True,
            "status": "success",
            "unit": unit,
            "invocation": invocation,
            "cgroup_occupants": list(instance.occupants),
            "gpu_occupants": list(instance.gpu_occupants),
        }


def test_pending_release_vector_is_202_excluded_and_idempotently_replayed(tmp_path: Path) -> None:
    cases = _load_p01()["pending_release"]
    validate_instance(cases["valid"], "rpc-envelope-v1.schema.json")
    systemd = DrainingSystemd()
    executor, actual_systemd, gpu, trusted, valid = _prepare_owner_release(tmp_path, systemd=systemd)
    actual_systemd.instance("unit-a", "invoke-a").occupants = ["pid-draining"]
    gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})

    pending = executor.release_rpc(valid, "req-release-pending", authenticated_controller=trusted)
    validate_instance(pending, "rpc-envelope-v1.schema.json")
    assert pending["status"] == cases["valid"]["status"]
    assert pending["data"]["operation"] == "release"
    assert pending["data"]["queue_id"] is None
    assert executor.state_snapshot(LANE)["state"] == "stopping"
    assert executor.state_snapshot(LANE)["release_pending"] is True

    call_count = len(actual_systemd.calls)
    replay = executor.release_rpc(valid, "req-release-pending", authenticated_controller=trusted)
    assert replay == pending
    assert len(actual_systemd.calls) == call_count

    # A fresh request ID may ask again. Once the exact observations are empty,
    # release completes; the old pending request remains a pending replay.
    actual_systemd.instance("unit-a", "invoke-a").occupants = []
    gpu.scripted.append({"ok": True, "status": "ok", "gpu_tenants": []})
    complete = executor.release_rpc(valid, "req-release-retry", authenticated_controller=trusted)
    validate_instance(complete, "rpc-envelope-v1.schema.json")
    assert complete["status"] == 200 and complete["data"]["operation"] == "release"
    assert executor.state_snapshot(LANE)["state"] == "free"
    assert executor.release_rpc(valid, "req-release-pending", authenticated_controller=trusted) == pending


def test_content_label_vector_is_admitted_only_from_admission(tmp_path: Path) -> None:
    cases = _load_p01()["content_labels"]
    pipeline = _pipeline_for(cases["valid"])

    valid = cases["valid"]
    projection = local_action_projection(
        valid,
        destination_site="site-a",
        controller_id="controller-a",
        current_pipeline=pipeline,
    )
    assert projection["content_labels"] == ["acceptable-use"]
    assert "content_labels" not in projection["args"]
    assert content_labels_admitted(valid["admission"]["content_labels"], pipeline["content_policy"])

    denied = cases["policy_denied"]
    assert not content_labels_admitted(denied["admission"]["content_labels"], pipeline["content_policy"])
    with pytest.raises(LocalActionError, match="content label"):
        local_action_projection(
            denied,
            destination_site="site-a",
            controller_id="controller-a",
            current_pipeline=pipeline,
        )

    for value in (None, ["acceptable-use", "acceptable-use"], [1], [""]):
        invalid = copy.deepcopy(valid)
        invalid["admission"]["content_labels"] = value
        with pytest.raises(LocalActionError):
            local_action_projection(invalid, destination_site="site-a", controller_id="controller-a")

    unlabeled = copy.deepcopy(valid)
    del unlabeled["admission"]["content_labels"]
    unlabeled_projection = local_action_projection(unlabeled, destination_site="site-a", controller_id="controller-a")
    assert unlabeled_projection["content_labels"] == []


def test_local_action_vector_hash_is_canonical_and_binds_execution_fields() -> None:
    case = _load_p01()["local_action"]
    request_value = case["request"]
    projection = local_action_projection(
        request_value,
        destination_site=case["destination_site"],
        controller_id=case["controller_id"],
    )
    canonical, digest = canonical_local_action_payload(
        request_value,
        destination_site=case["destination_site"],
        controller_id=case["controller_id"],
    )
    assert projection == case["projection"]
    assert canonical.decode("utf-8") == case["canonical_utf8"]
    assert digest == case["payload_hash"]

    for mutation in case["negative_mutations"]:
        changed = copy.deepcopy(request_value)
        target = changed
        for key in mutation["path"][:-1]:
            target = target[key]
        target[mutation["path"][-1]] = mutation["value"]
        _, changed_digest = canonical_local_action_payload(
            changed,
            destination_site=case["destination_site"],
            controller_id=case["controller_id"],
        )
        assert changed_digest != digest

    transport_only = copy.deepcopy(request_value)
    transport_only["request_id"] = "another-request"
    transport_only["request_fingerprint"] = "b" * 64
    transport_only["admission"]["ingress"]["authenticated_peer"] = "another-peer"
    transport_only["admission"]["approval"] = {"approval_id": "approval-new", "required": True, "consume_atomically": True}
    _, transport_digest = canonical_local_action_payload(
        transport_only,
        destination_site=case["destination_site"],
        controller_id=case["controller_id"],
    )
    assert transport_digest == digest

    missing_labels = copy.deepcopy(request_value)
    del missing_labels["admission"]["content_labels"]
    explicit_empty = copy.deepcopy(missing_labels)
    explicit_empty["admission"]["content_labels"] = []
    _, absent_digest = canonical_local_action_payload(
        missing_labels,
        destination_site=case["destination_site"],
        controller_id=case["controller_id"],
    )
    _, empty_digest = canonical_local_action_payload(
        explicit_empty,
        destination_site=case["destination_site"],
        controller_id=case["controller_id"],
    )
    assert absent_digest == empty_digest
