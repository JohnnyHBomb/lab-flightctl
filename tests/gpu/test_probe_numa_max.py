"""The probe's numa_node range matches the inventory's (owner ruling, 6 Oct 2026): Linux has at most 1024 NUMA nodes
(MAX_NUMNODES = 1 << CONFIG_NODES_SHIFT, NODES_SHIFT at most 10), so a node id is 0-1023 in gpu-probe.schema.json as in
inventory.schema.json, and the real twin reports a sysfs value past that range as null, never as a probe-invalid id."""

import pytest

from tests.contracts_v2.validation import ContractError, assert_valid
from tests.gpu.test_a3i_inventory import ROW, _inventory, _minor, _numa, _smi

BUS = "0000:8d:00.0"


def _observation(numa):
    return {"kind": "gpu-inventory", "host_id": "host-1", "observed_at": "2026-10-06T00:00:00Z", "status": "ok",
            "reason": None, "vendor": "nvidia",
            "devices": [{"index": 0, "uuid": "GPU-00000000-0000-0000-0000-000000000000", "pci_bus_id": BUS,
                         "name": "NVIDIA TITAN RTX", "memory_total_mib": 24576, "driver_version": "610.57.04",
                         "numa_node": numa, "device_minor": 0}]}


def test_probe_schema_numa_node_is_0_to_1023() -> None:
    for numa in (0, 1023, None):
        assert_valid(_observation(numa), "gpu-probe", "inventory_observation")
    for numa in (1024, -1):
        with pytest.raises(ContractError):
            assert_valid(_observation(numa), "gpu-probe", "inventory_observation")


@pytest.mark.parametrize("sysfs,expected", [("1023\n", 1023), ("1024\n", None), ("99999999999999999999\n", None)])
def test_real_twin_reports_a_numa_node_past_1023_as_null(sysfs, expected) -> None:
    answers = {_smi(): (0, ROW.format(0, "00000000:8D:00.0")), _numa(BUS): (0, sysfs), _minor(BUS): (0, "Device Minor: 3\n")}
    obs, _ = _inventory(answers)  # _inventory asserts the observation is gpu-probe#inventory_observation valid
    assert obs["status"] == "ok" and obs["devices"][0]["numa_node"] == expected
