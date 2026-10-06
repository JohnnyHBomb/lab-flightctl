from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import time
from pathlib import Path

import pytest

from flightctl.discovery import DiscoveryError, DiscoveryHandler, project_proposal
from tests.contracts.validation import ContractError, validate_discovery, validate_inventory
from tests.fakes import FakeClock, FakeGPUProbe, FakeSSH


ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "tests" / "fakes" / "fixtures"


def _fixtures() -> dict[str, dict]:
    return {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in FIXTURES.glob("*.json")}


def _policy() -> dict:
    return json.loads((ROOT / "config" / "policy.json.example").read_text(encoding="utf-8"))


def _current() -> dict:
    return json.loads((ROOT / "config" / "inventory.json.example").read_text(encoding="utf-8"))


def _handler(hosts: list[str], *, scripted_gpu: list[dict] | None = None, current: dict | None = None, transport_script: list[dict] | None = None) -> DiscoveryHandler:
    fixtures = _fixtures()
    transport = FakeSSH(transport_script or [{"outcome": "success"} for _ in hosts])
    probe = FakeGPUProbe(fixtures, scripted=scripted_gpu)
    return DiscoveryHandler(transport, FakeClock(), probe, current_inventory=current)


def _lane(lane_id: str, device_ids: list[str]) -> dict:
    lane = copy.deepcopy(_current()["lanes"][0])
    lane["lane_id"] = lane_id
    lane["device_ids"] = list(device_ids)
    return lane


def _identity_current(devices: list[dict], lanes: list[dict]) -> dict:
    current = _current()
    current["policy"] = _policy()
    current["hosts"][0]["ssh_endpoint"] = "host-1"
    current["hosts"][0]["devices"] = copy.deepcopy(devices)
    current["hosts"][0]["gpu_count"] = len(devices)
    current["lanes"] = copy.deepcopy(lanes)
    current["chat_lane_order"] = [lane["lane_id"] for lane in lanes]
    return current


def test_partial_and_transport_unknowns_are_not_zero() -> None:
    names = ["empty", "malformed", "negative", "partial", "missing-tool", "denied", "timeout", "unreachable"]
    scripted_gpu = [
        {"raw": ""},
        {"raw": "not,a,complete,row"},
        {"status": "missing", "error": "nvidia-smi not found"},
        {"raw": "Tesla T4,-1,535.1,nvidia"},
        {"devices": [{"device_id": "gpu0", "vendor": "nvidia", "model": "Partial", "vram_bytes": None, "driver": "535.1"}], "count": 1},
    ]
    transport_script = [{"outcome": "denied"}] + [{"outcome": "success"} for _ in scripted_gpu] + [
        {"outcome": "timeout"},
        {"outcome": "unknown", "error": "host unreachable"},
    ]
    proposal = _handler(names, scripted_gpu=scripted_gpu, transport_script=transport_script).propose({"hosts": names, "controller": _current()["controller"], "policy": _policy()})
    by_host = {host["host_id"]: host for host in proposal["hosts"]}
    for name in names:
        host = by_host[name]
        assert host["gpu_count"] is None
        assert host["gpu_count_reason"]
        assert host["gpu_count"] != 0
        assert not host["admissible"]
        for device in host["devices"]:
            for field in ("vendor", "model", "vram_bytes", "driver"):
                if device[field] is None:
                    assert device["unknown_reasons"].get(field)
    partial = by_host["partial"]["devices"][0]
    assert partial["vendor"] == "nvidia"
    assert partial["model"] == "Partial"
    assert partial["driver"] == "535.1"
    assert partial["vram_bytes"] is None
    assert proposal["lanes"] == []
    validate_discovery(proposal)


def test_retained_configuration_and_missing_host_is_disabled() -> None:
    current = _current()
    current["policy"] = _policy()
    current["timezone"] = "Europe/London"
    current["lanes"][0]["enabled"] = False
    current["lanes"][0]["policy"]["provenance"] = ["operator-choice"]
    proposal = _handler([], current=current, transport_script=[]).propose({"tailnet_status": {"Peer": {}}, "current": current})

    assert proposal["controller"] == current["controller"]
    assert proposal["timezone"] == current["timezone"]
    assert proposal["policy"] == current["policy"]
    assert proposal["identity_mapping"] == current["identity_mapping"]
    assert proposal["hosts"][0]["host_id"] == current["hosts"][0]["host_id"]
    assert proposal["hosts"][0]["reachability"] == "unknown"
    assert proposal["hosts"][0]["gpu_count"] is None
    assert proposal["lanes"][0]["lane_id"] == current["lanes"][0]["lane_id"]
    assert proposal["lanes"][0]["enabled"] is False
    assert proposal["lanes"][0]["action"] == "retain"
    validate_discovery(proposal)
    projected = project_proposal(proposal)
    validate_inventory(projected)


def test_failed_probe_retains_unknown_device_records_and_lane_reference() -> None:
    current = _current()
    current["policy"] = _policy()
    transport = FakeSSH([{"outcome": "timeout"}])
    handler = DiscoveryHandler(transport, FakeClock(), FakeGPUProbe({}), current_inventory=current)

    proposal = handler.propose({"hosts": ["host-1"], "current": current, "policy": _policy()})
    host = proposal["hosts"][0]
    assert [device["device_id"] for device in host["devices"]] == ["gpu0"]
    assert all(device[field] is None for field in ("vendor", "model", "vram_bytes", "driver") for device in host["devices"])
    assert all(device["unknown_reasons"] for device in host["devices"])
    assert proposal["lanes"][0]["enabled"] is False
    assert proposal["lanes"][0]["device_ids"] == ["gpu0"]
    validate_discovery(proposal)
    validate_inventory(project_proposal(proposal))


def test_configured_ssh_user_is_retained_unless_explicitly_overridden() -> None:
    current = _current()
    current["policy"] = _policy()
    current["hosts"][0]["ssh_user"] = "customuser"
    transport = FakeSSH([{"outcome": "timeout"}])
    handler = DiscoveryHandler(transport, FakeClock(), FakeGPUProbe({}), current_inventory=current)
    proposal = handler.propose({"hosts": ["host-1"], "current": current, "policy": _policy()})
    assert proposal["hosts"][0]["ssh_user"] == "customuser"
    assert transport.calls[0]["message"]["ssh_user"] == "customuser"

    override_transport = FakeSSH([{"outcome": "timeout"}])
    override = DiscoveryHandler(override_transport, FakeClock(), FakeGPUProbe({}), current_inventory=current).propose(
        {"hosts": [{"host_id": "host-1", "ssh_endpoint": "host-1.example", "ssh_user": "operator"}], "current": current, "policy": _policy()}
    )
    assert override["hosts"][0]["ssh_user"] == "operator"
    assert override_transport.calls[0]["message"]["ssh_user"] == "operator"


def test_transport_diagnostics_are_safe_categories() -> None:
    class LeakyTransport:
        def request(self, endpoint: str, message: dict, timeout_s: float) -> dict:
            raise RuntimeError("Authorization: Bearer SUPERSECRET")

    proposal = DiscoveryHandler(LeakyTransport(), FakeClock(), FakeGPUProbe({})).propose(
        {"hosts": ["host-1"], "controller": _current()["controller"], "policy": _policy()}
    )
    encoded = json.dumps(proposal, sort_keys=True)
    assert "SUPERSECRET" not in encoded
    assert "Authorization" not in encoded
    assert proposal["hosts"][0]["observation_error"] == "host probe failed"
    assert all(observation["reason"] == "host probe failed" for observation in proposal["observations"])


def test_real_projection_drops_proposal_fields_and_forces_draft() -> None:
    names = ["t4"]
    proposal = _handler(names).propose({"hosts": names, "controller": _current()["controller"], "policy": _policy()})
    assert proposal["stage"] == "draft"
    proposal["stage"] = "confirmed"
    projected = project_proposal(proposal)
    assert projected["stage"] == "draft"
    assert "policy" not in projected
    assert "generated_at" not in projected
    assert "diff" not in projected
    assert "observations" not in projected
    assert "status" not in projected
    for key in ("schema_version", "site_id", "revision", "controller", "timezone", "identity_mapping", "chat_lane_order"):
        assert projected[key] == proposal[key]
    for key in ("host_id", "ssh_endpoint", "ssh_user", "reachability", "observed_at", "observation_error", "gpu_count", "gpu_count_reason", "devices"):
        assert projected["hosts"][0][key] == proposal["hosts"][0][key]
    for key in ("lane_id", "host_id", "device_ids", "class", "enabled", "policy"):
        assert projected["lanes"][0][key] == proposal["lanes"][0][key]
    assert "admissible" not in projected["hosts"][0]
    assert "action" not in projected["lanes"][0]
    validate_inventory(projected)


def test_unknown_admission_boundary_is_left_to_inventory_semantics() -> None:
    current = _current()
    current["policy"] = _policy()
    proposal = _handler([], current=current, transport_script=[]).propose({"tailnet_status": {"Peer": {}}, "current": current})
    projected = project_proposal(proposal)
    validate_inventory(projected)
    enabled = copy.deepcopy(projected)
    enabled["lanes"][0]["enabled"] = True
    with pytest.raises(ContractError, match="enabled lane uses unavailable host"):
        validate_inventory(enabled)
    unresolved = copy.deepcopy(projected)
    unresolved["stage"] = "draft"
    unresolved["controller"] = {"controller_id": "controller", "endpoint": None, "account": None, "paths": [], "auth_state": "unresolved"}
    validate_inventory(unresolved)

    confirmed_unknown = copy.deepcopy(projected)
    confirmed_unknown["stage"] = "confirmed"
    with pytest.raises(ContractError, match="confirmed inventory has unknown device"):
        validate_inventory(confirmed_unknown)

    confirmed_unresolved = copy.deepcopy(_current())
    confirmed_unresolved["controller"] = {"controller_id": "controller", "endpoint": None, "account": None, "paths": [], "auth_state": "unresolved"}
    with pytest.raises(ContractError, match="confirmed inventory has unresolved controller"):
        validate_inventory(confirmed_unresolved)


def test_deterministic_diff_and_external_current_default(tmp_path: Path) -> None:
    current_path = tmp_path / "current.json"
    current_path.write_text(json.dumps(_current(), sort_keys=True), encoding="utf-8")
    names = ["t4", "titan-rtx"]
    first = _handler(names, transport_script=[{"outcome": "success"}] * 2)
    second = _handler(list(reversed(names)), transport_script=[{"outcome": "success"}] * 2)
    options = {"hosts": names, "current": current_path, "policy": _policy()}
    one = first.propose(options)
    two = second.propose({**options, "hosts": list(reversed(names))})
    encoded_one = json.dumps(one, sort_keys=True, separators=(",", ":"))
    encoded_two = json.dumps(two, sort_keys=True, separators=(",", ":"))
    assert encoded_one == encoded_two
    assert [entry["sort_key"] for entry in one["diff"]] == sorted(entry["sort_key"] for entry in one["diff"])

    default_handler = DiscoveryHandler(
        FakeSSH([{"outcome": "success"}] * 2),
        FakeClock(),
        FakeGPUProbe(_fixtures()),
        current_inventory_path=current_path,
    )
    default_proposal = default_handler.propose({"hosts": list(reversed(names)), "policy": _policy(), "controller": _current()["controller"]})
    assert json.dumps(one, sort_keys=True, separators=(",", ":")) == json.dumps(default_proposal, sort_keys=True, separators=(",", ":"))

    output = tmp_path / "proposal.json"
    before_hash = hashlib.sha256(current_path.read_bytes()).hexdigest()
    _handler(names, transport_script=[{"outcome": "success"}] * 2).discover({**options, "output": output})
    assert output.read_bytes() == json.dumps(one, sort_keys=True, indent=2).encode() + b"\n"
    assert hashlib.sha256(current_path.read_bytes()).hexdigest() == before_hash


def test_driver_change_and_disappearance_are_review_diffs() -> None:
    current = _current()
    current["policy"] = _policy()
    changed = copy.deepcopy(current)
    changed["hosts"][0]["devices"][0]["driver"] = "535.1"
    changed_proposal = _handler(["host-1"], current=changed, scripted_gpu=[{"devices": [{"device_id": "gpu0", "vendor": "nvidia", "model": "Generic Accelerator", "vram_bytes": 17179869184, "driver": "550.1"}], "count": 1}], transport_script=[{"outcome": "success"}]).propose(
        {"hosts": [{"host_id": "host-1", "ssh_endpoint": "host-1.example"}], "current": changed}
    )
    assert changed_proposal["diff"] == [
        {
            "sort_key": "driver/gpu0",
            "kind": "driver",
            "id": "gpu0",
            "change": "changed",
            "before": "535.1",
            "after": "550.1",
            "review_required": True,
        },
        {
            "sort_key": "lane/lane-gpu0",
            "kind": "lane",
            "id": "lane-gpu0",
            "change": "retained",
            "before": {"enabled": True, "device_ids": ["gpu0"]},
            "after": {"enabled": False, "device_ids": ["gpu0"]},
            "review_required": True,
        },
    ]
    assert changed_proposal["lanes"][0]["enabled"] is False

    disappeared = copy.deepcopy(current)
    disappeared["hosts"][0]["devices"].append({"device_id": "gpu1", "vendor": "nvidia", "model": "Second", "vram_bytes": 2, "driver": "550.1", "unknown_reasons": {}})
    disappeared["hosts"][0]["gpu_count"] = 2
    disappeared_proposal = _handler(["host-1"], current=disappeared, scripted_gpu=[{"devices": [{"device_id": "gpu0", "vendor": "nvidia", "model": "Generic Accelerator", "vram_bytes": 17179869184, "driver": "550.1"}], "count": 1}], transport_script=[{"outcome": "success"}]).propose(
        {"hosts": [{"host_id": "host-1", "ssh_endpoint": "host-1.example"}], "current": disappeared}
    )
    host = disappeared_proposal["hosts"][0]
    assert host["gpu_count"] is None
    assert "gpu1" in {item["device_id"] for item in host["devices"]}
    assert disappeared_proposal["diff"] == [
        {
            "sort_key": "host/host-1",
            "kind": "host",
            "id": "host-1",
            "change": "changed",
            "before": {"ssh_endpoint": "host-1.example", "ssh_user": "runner", "reachability": "confirmed", "observation_error": None, "gpu_count": 2, "gpu_count_reason": None},
            "after": {"ssh_endpoint": "host-1.example", "ssh_user": "runner", "reachability": "confirmed", "observation_error": None, "gpu_count": None, "gpu_count_reason": "device set changed; retained missing device records"},
            "review_required": True,
        },
        {
            "sort_key": "lane/lane-gpu0",
            "kind": "lane",
            "id": "lane-gpu0",
            "change": "retained",
            "before": {"enabled": True, "device_ids": ["gpu0"]},
            "after": {"enabled": False, "device_ids": ["gpu0"]},
            "review_required": True,
        },
        {
            "sort_key": "unknown/gpu1",
            "kind": "unknown",
            "id": "gpu1",
            "change": "unknown",
            "before": {"vendor": "nvidia", "model": "Second", "vram_bytes": 2, "driver": "550.1"},
            "after": {"vendor": None, "model": None, "vram_bytes": None, "driver": None},
            "review_required": True,
        },
    ]
    validate_discovery(disappeared_proposal)


def test_custom_lane_grouping_is_preserved() -> None:
    current = _current()
    current["policy"] = _policy()
    second_device = {"device_id": "gpu1", "vendor": "nvidia", "model": "Second", "vram_bytes": 2, "driver": "550.1", "unknown_reasons": {}}
    current["hosts"][0]["devices"].append(second_device)
    current["hosts"][0]["gpu_count"] = 2
    second_lane = copy.deepcopy(current["lanes"][0])
    second_lane["lane_id"] = "lane-gpu1"
    second_lane["device_ids"] = ["gpu1"]
    current["lanes"].append(second_lane)
    probe = {"devices": [{"device_id": "gpu0", "vendor": "nvidia", "model": "Generic Accelerator", "vram_bytes": 17179869184, "driver": "550.1"}, second_device], "count": 2}
    proposal = _handler(["host-1"], current=current, scripted_gpu=[probe], transport_script=[{"outcome": "success"}]).propose({"hosts": ["host-1"], "current": current})
    groups = {lane["lane_id"]: lane["device_ids"] for lane in proposal["lanes"]}
    assert groups["lane-gpu0"] == ["gpu0"]
    assert groups["lane-gpu1"] == ["gpu1"]
    assert all(lane["enabled"] is True for lane in proposal["lanes"])
    validate_discovery(proposal)


def test_known_unassigned_device_stays_outside_custom_lane() -> None:
    current = _current()
    current["hosts"][0]["devices"].append(
        {"device_id": "spare", "vendor": "nvidia", "model": "Spare", "vram_bytes": 1024**2, "driver": "550.1", "unknown_reasons": {}}
    )
    current["hosts"][0]["gpu_count"] = 2
    transport = FakeSSH([{"outcome": "success", "response": {"devices": current["hosts"][0]["devices"], "count": 2}}])
    proposal = DiscoveryHandler(transport, FakeClock(), None, current_inventory=current).propose(
        {"hosts": [{"host_id": "host-1", "ssh_endpoint": "host-1.example"}]}
    )
    assert proposal["lanes"] == [{**current["lanes"][0], "action": "retain"}]
    assert proposal["diff"] == []
    validate_discovery(proposal)
    validate_inventory(project_proposal(proposal))


@pytest.mark.parametrize("count", [None, -1, True, "invalid"])
def test_missing_or_invalid_gpu_count_has_reason_and_preserves_fields(count: object) -> None:
    payload = {"devices": _current()["hosts"][0]["devices"]}
    if count is not None:
        payload["count"] = count
    transport = FakeSSH([{"outcome": "success", "response": payload}])
    proposal = DiscoveryHandler(transport, FakeClock(), None).propose(
        {"hosts": ["host-1"], "controller": _current()["controller"], "policy": _policy()}
    )
    host = proposal["hosts"][0]
    assert host["gpu_count"] is None
    assert host["gpu_count_reason"]
    assert host["admissible"] is False
    assert proposal["lanes"] == []
    for field in ("vendor", "model", "vram_bytes", "driver"):
        assert host["devices"][0][field] == payload["devices"][0][field]
    validate_discovery(proposal)
    validate_inventory(project_proposal(proposal))


def test_current_host_aliases_probe_once_and_are_order_independent() -> None:
    current = _current()
    proposals = []
    for hosts in (["host-1", "host-1.example"], ["host-1.example", "host-1"]):
        transport = FakeSSH([{"outcome": "success", "response": {"devices": current["hosts"][0]["devices"], "count": 1}}])
        proposal = DiscoveryHandler(transport, FakeClock(), None, current_inventory=current).propose({"hosts": hosts})
        assert len(transport.calls) == 1
        assert transport.calls[0]["endpoint"] == "host-1.example"
        assert [host["host_id"] for host in proposal["hosts"]] == ["host-1"]
        assert proposal["diff"] == []
        validate_discovery(proposal)
        validate_inventory(project_proposal(proposal))
        proposals.append(proposal)
    assert proposals[0] == proposals[1]


@pytest.mark.parametrize("hosts", [
    [{"host_id": "same", "ssh_endpoint": "one.example"}, {"host_id": "same", "ssh_endpoint": "two.example"}],
    [{"host_id": "one", "ssh_endpoint": "same.example"}, {"host_id": "two", "ssh_endpoint": "same.example"}],
    ["alice@host.example", "bob@host.example"],
])
def test_conflicting_host_specs_rejected_before_probing(hosts: list) -> None:
    transport = FakeSSH()
    with pytest.raises(DiscoveryError, match="conflicting"):
        DiscoveryHandler(transport, FakeClock(), None).propose({"hosts": hosts})
    assert transport.calls == []


def test_confirmed_unreachable_fixture_retains_known_hardware_but_denies_lane() -> None:
    # The frozen contract permits retained known measurements in confirmed
    # inventory; freshly unknown device fields must instead remain draft.
    inventory = _current()
    assert inventory["stage"] == "confirmed"
    inventory["hosts"][0].update(reachability="unknown", observed_at=None, observation_error="host unreachable")
    inventory["lanes"][0]["enabled"] = False
    validate_inventory(inventory)
    inventory["lanes"][0]["enabled"] = True
    with pytest.raises(ContractError, match="enabled lane uses unavailable host"):
        validate_inventory(inventory)


def test_reachable_human_disabled_lane_is_never_reenabled() -> None:
    current = _current()
    current["policy"] = _policy()
    current["lanes"][0]["enabled"] = False
    probe = {"devices": [{"device_id": "gpu0", "vendor": "nvidia", "model": "Generic Accelerator", "vram_bytes": 17179869184, "driver": "550.1"}], "count": 1}
    proposal = _handler(["host-1"], current=current, scripted_gpu=[probe], transport_script=[{"outcome": "success"}]).propose(
        {"hosts": ["host-1"], "current": current, "policy": _policy()}
    )
    assert proposal["lanes"][0]["enabled"] is False
    assert proposal["lanes"][0]["action"] == "retain"
    validate_discovery(proposal)


def test_bounded_range_has_no_retry_or_unrequested_scan() -> None:
    transport = FakeSSH()
    handler = DiscoveryHandler(transport, FakeClock(), FakeGPUProbe({}), max_hosts=3)
    proposal = handler.propose({"ranges": "host-01..host-99", "controller": _current()["controller"], "policy": _policy(), "max_hosts": 3})
    assert len(transport.calls) == 3
    assert len(proposal["hosts"]) == 3
    assert all(host["gpu_count"] is None for host in proposal["hosts"])
    assert all(call["timeout_s"] == 5.0 for call in transport.calls)


def test_delayed_transport_reply_is_a_bounded_unknown() -> None:
    transport = FakeSSH([{"outcome": "delayed", "delay_s": 99}])
    handler = DiscoveryHandler(transport, FakeClock(), FakeGPUProbe({}))
    proposal = handler.propose({"hosts": ["slow-host"], "controller": _current()["controller"], "policy": _policy(), "timeout_s": 0.25})
    host = proposal["hosts"][0]
    assert host["reachability"] == "unknown"
    assert host["gpu_count"] is None
    assert host["observation_error"] == "probe timed out"
    assert transport.calls[0]["timeout_s"] == 0.25
    validate_discovery(proposal)


def test_delayed_transport_returns_within_the_supplied_deadline() -> None:
    class DeadlineTransport:
        def __init__(self) -> None:
            self.calls: list[float] = []

        def request(self, endpoint: str, message: dict, timeout_s: float) -> dict:
            self.calls.append(timeout_s)
            if timeout_s > 0.05:
                raise AssertionError("transport received an unbounded timeout")
            time.sleep(timeout_s)
            return {"status": "timeout", "error": "reply exceeded deadline"}

    transport = DeadlineTransport()
    started = time.monotonic()
    proposal = DiscoveryHandler(transport, FakeClock(), None).propose(
        {"hosts": ["slow-host"], "controller": _current()["controller"], "policy": _policy(), "timeout_s": 0.02}
    )
    elapsed = time.monotonic() - started
    assert elapsed < 0.20
    assert transport.calls == [0.02]
    assert proposal["hosts"][0]["reachability"] == "unknown"
    assert proposal["hosts"][0]["observation_error"] == "probe timed out"
    validate_discovery(proposal)


def test_output_aliases_and_failures_never_overwrite_current_or_repo(tmp_path: Path) -> None:
    current = tmp_path / "current.json"
    current.write_text(json.dumps(_current()), encoding="utf-8")
    handler = DiscoveryHandler(FakeSSH(), FakeClock(), FakeGPUProbe({}), current_inventory_path=current)
    proposal = handler.propose({"current": current})
    before = current.read_bytes()
    with pytest.raises(DiscoveryError, match="aliases"):
        handler.write_proposal(proposal, current, current_path=current)
    with pytest.raises(DiscoveryError, match="checkout"):
        handler.write_proposal(proposal, ROOT / "config" / "proposal.json", current_path=current)
    link = tmp_path / "current-link.json"
    link.symlink_to(current)
    with pytest.raises(DiscoveryError, match="symlink"):
        handler.write_proposal(proposal, link, current_path=current)
    with pytest.raises(DiscoveryError, match="directory"):
        handler.write_proposal(proposal, tmp_path / "missing" / "proposal.json", current_path=current)
    with pytest.raises(DiscoveryError, match="file"):
        handler.write_proposal(proposal, tmp_path, current_path=current)
    assert current.read_bytes() == before


def test_discover_requires_external_output_before_any_probe() -> None:
    transport = FakeSSH()
    handler = DiscoveryHandler(transport, FakeClock(), FakeGPUProbe({}))
    options = {"hosts": ["host-1"], "controller": _current()["controller"], "policy": _policy()}
    with pytest.raises(DiscoveryError, match="output path is required"):
        handler.discover(options)
    assert transport.calls == []


def test_hardlink_output_alias_is_rejected_without_replacing_either_file(tmp_path: Path) -> None:
    current = tmp_path / "current.json"
    current.write_text(json.dumps(_current()), encoding="utf-8")
    alias = tmp_path / "alias.json"
    alias.hardlink_to(current)
    before = current.read_bytes()
    handler = DiscoveryHandler(FakeSSH(), FakeClock(), None, current_inventory_path=current)
    with pytest.raises(DiscoveryError, match="aliases"):
        handler.write_proposal(handler.propose(), alias, current_path=current)
    assert alias.samefile(current)
    assert alias.read_bytes() == current.read_bytes() == before


def test_unwritable_output_is_rejected_without_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    handler = DiscoveryHandler(FakeSSH(), FakeClock(), FakeGPUProbe({}))
    proposal = handler.propose({"hosts": ["host-1"], "controller": _current()["controller"], "policy": _policy()})
    monkeypatch.setattr("flightctl.discovery.os.access", lambda _path, _mode: False)
    with pytest.raises(DiscoveryError, match="writable"):
        handler.write_proposal(proposal, tmp_path / "proposal.json")


def test_no_side_effects_writes_only_external_proposal_and_forbids_authority_calls(tmp_path: Path) -> None:
    class SideEffectGuard:
        def __init__(self) -> None:
            self.request_calls: list[dict] = []
            self.forbidden_calls: list[str] = []

        def request(self, endpoint: str, message: dict, timeout_s: float) -> dict:
            self.request_calls.append({"endpoint": endpoint, "message": dict(message), "timeout_s": timeout_s})
            return {"status": "ok", "response": {"nvidia_smi": "Tesla T4,16384,535.1,nvidia\n"}}

        def grant(self, *args: object, **kwargs: object) -> None:
            self.forbidden_calls.append("grant")
            raise AssertionError("grant must not be called")

        def start(self, *args: object, **kwargs: object) -> None:
            self.forbidden_calls.append("start")
            raise AssertionError("start must not be called")

        def install(self, *args: object, **kwargs: object) -> None:
            self.forbidden_calls.append("install")
            raise AssertionError("install must not be called")

        def stage(self, *args: object, **kwargs: object) -> None:
            self.forbidden_calls.append("stage")
            raise AssertionError("stage must not be called")

        def confirm(self, *args: object, **kwargs: object) -> None:
            self.forbidden_calls.append("confirm")
            raise AssertionError("confirm must not be called")

    current = tmp_path / "current.json"
    current_bytes = json.dumps(_current(), sort_keys=True).encode() + b"\n"
    current.write_bytes(current_bytes)
    output = tmp_path / "proposal.json"
    before_files = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    tracked_files = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).splitlines()
    repo_files = {ROOT / relative: (ROOT / relative).read_bytes() for relative in tracked_files if (ROOT / relative).is_file()}
    transport = SideEffectGuard()
    handler = DiscoveryHandler(transport, FakeClock(), None, current_inventory_path=current)
    proposal = handler.discover(
        {
            "hosts": [{"host_id": "host-1", "ssh_endpoint": "host-1.example"}],
            "controller": _current()["controller"],
            "policy": _policy(),
            "output": output,
        }
    )

    after_files = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    assert set(after_files) == set(before_files) | {"proposal.json"}
    assert after_files["current.json"] == current_bytes
    assert output.read_bytes() == json.dumps(proposal, sort_keys=True, indent=2).encode() + b"\n"
    assert transport.forbidden_calls == []
    assert len(transport.request_calls) == 1
    assert {path: path.read_bytes() for path in repo_files} == repo_files
