"""OccupancyProbe and InventoryProbe conformance (gpu-probe.schema.json). Real cases are READ-ONLY
nvidia-smi queries on the target host's lane cards; they never allocate GPU memory. The collision case
(a deliberate CUDA context) is not here: it is acceptance test T10 under a held lane."""

import pytest

from tests.contracts_v2.validation import assert_valid

from .conftest import port_params

OCC = pytest.mark.parametrize("kind,factory", port_params("occupancy_probe"))
INV = pytest.mark.parametrize("kind,factory", port_params("inventory_probe"))


def _probe(kind, factory):
    from . import registry
    return factory(registry.target())


@OCC
def test_observation_validates_and_is_filtered_to_lane_uuids(kind, factory) -> None:
    probe = _probe(kind, factory)
    obs = probe.occupancy(probe.test_host, probe.test_lane, probe.test_uuids, process_noise_mib=512, lane_noise_mib=1024, timeout_s=10)
    assert_valid(obs, "gpu-probe", "occupancy_observation")
    if obs["status"] == "ok":
        assert {g["uuid"] for g in obs["gpus"]} == set(probe.test_uuids)
        assert all(p["gpu_uuid"] in probe.test_uuids for p in obs["processes"])


@OCC
def test_small_processes_are_noise_never_tenants(kind, factory) -> None:
    probe = _probe(kind, factory)
    obs = probe.occupancy(probe.test_host, probe.test_lane, probe.test_uuids, process_noise_mib=512, lane_noise_mib=1024, timeout_s=10)
    for proc in obs["tenants"]:
        assert proc["used_memory_mib"] is None or proc["used_memory_mib"] >= 512
    for proc in obs["noise"]:
        assert proc["used_memory_mib"] is not None and proc["used_memory_mib"] < 512


@OCC
@pytest.mark.fake_only
def test_tool_failure_and_timeout_are_unknown_not_empty(kind, factory) -> None:
    probe = _probe(kind, factory)
    for fault in ("timeout", "exit-9", "unparsable", "na-only"):
        probe.script_next(fault)
        obs = probe.occupancy(probe.test_host, probe.test_lane, probe.test_uuids, process_noise_mib=512, lane_noise_mib=1024, timeout_s=1)
        assert obs["status"] == "unknown" and obs["tenants"] == [] and obs["reason"], fault


@OCC
@pytest.mark.fake_only
def test_golden_captures_parse_for_every_lab_card_model(kind, factory) -> None:
    probe = _probe(kind, factory)
    for capture in probe.golden_captures():  # one per card model, recorded by the 'record' wrapper (slice A3)
        obs = probe.parse_capture(capture)
        assert_valid(obs, "gpu-probe", "occupancy_observation")
        assert obs == capture["expected"]


@INV
def test_inventory_shape_carries_uuid_bus_and_numa(kind, factory) -> None:
    probe = _probe(kind, factory)
    inv = probe.inventory(probe.test_host, timeout_s=10)
    assert_valid(inv, "gpu-probe", "inventory_observation")
    if inv["status"] == "ok" and inv["vendor"] == "nvidia":
        assert all(d["uuid"].startswith("GPU-") and d["pci_bus_id"].count(":") == 2 for d in inv["devices"])
