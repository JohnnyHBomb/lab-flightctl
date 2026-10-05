"""A3b named acceptance tests (part 1): inventory v2 card binding and discovery v2 on the InventoryProbe."""

import copy
import json
import os
from pathlib import Path

import pytest

from flightctl.clock import RealClock
from flightctl.commands import LocalCommandRunner, SshCommandRunner
from flightctl.discovery import DiscoveryError, ProjectionError, project_v2, propose_v2
from flightctl.gpu import NvidiaInventoryProbe
from flightctl.inventory import device_from_probe, inventory_problems
from tests.contracts_v2.validation import assert_valid, inventory_semantics
from tests.fakes.clock import FakeClock
from tests.fakes.gpu_replay import CAPTURE_DIR, ReplayGPUProbe

EXAMPLE = json.loads((Path(__file__).resolve().parents[2] / "config" / "inventory-v2.json.example").read_text(encoding="utf-8"))
CAPTURES = sorted(path.name.removesuffix(".expected.json") for path in CAPTURE_DIR.glob("*.expected.json"))
TARGET = os.environ.get("FLIGHTCTL_CONFORMANCE_TARGET") or None


class _Recording:  # the probe seam: records each call and a copy of each observation returned
    def __init__(self, probe):
        self.probe, self.calls, self.returned = probe, [], []

    def inventory(self, host_id, *, timeout_s):
        self.calls.append((host_id, timeout_s))
        observation = self.probe.inventory(host_id, timeout_s=timeout_s)
        self.returned.append(copy.deepcopy(observation))
        return observation


def _containers(value):  # the ids of every dict and list inside value
    if not isinstance(value, (dict, list)):
        return set()
    return {id(value)}.union(*map(_containers, value.values() if isinstance(value, dict) else value))


def _current(probe, cards):
    """The example inventory: host-1 becomes the capture's host with `cards` (ids gpu-<index>) in the enabled lane-gpu1;
    host-0 loses its gpu role (the replay knows one host) and keeps gpu-a in a new enabled lane-gpu0."""
    current = copy.deepcopy(EXAMPLE)
    current["hosts"][0]["roles"] = ["controller"]
    host, lane = current["hosts"][1], current["lanes"][0]
    host["host_id"] = lane["host_id"] = probe.test_host
    host["devices"] = [device_from_probe(f"gpu-{card['index']}", card, True) for card in cards]
    host["gpu_count"] = len(cards)
    lane["device_ids"] = [device["device_id"] for device in host["devices"]]
    current["lanes"].append(dict(copy.deepcopy(lane), lane_id="lane-gpu0", host_id="host-0", device_ids=["gpu-a"]))
    assert_valid(current, "inventory")
    return current


def _propose(current, probe):
    """propose_v2 on a fake clock: a valid proposal, current untouched and unshared, one probe call per gpu host."""
    before, spy = copy.deepcopy(current), _Recording(probe)
    proposal = propose_v2(current, spy, clock=FakeClock(), timeout_s=7)
    assert_valid(proposal, "discovery")
    assert current == before and not _containers(proposal) & _containers(current)
    gpu_hosts = [host["host_id"] for host in current["hosts"] if "gpu" in host["roles"]]
    assert spy.calls == [(host_id, 7) for host_id in gpu_hosts] and set(proposal["host_review"]) == set(gpu_hosts)
    assert set(proposal["lane_review"]) == {lane["lane_id"] for lane in current["lanes"]}
    assert proposal["observations"] == spy.returned and proposal["diff"] == []
    assert (proposal["generated_at"], proposal["current_revision"]) == ("2026-09-27T20:00:00Z", current["revision"])
    clean = set(proposal["host_review"].values()) <= {"admissible"} and set(proposal["lane_review"].values()) <= {"retain"}
    assert proposal["status"] == ("proposed" if clean else "needs_review")
    return proposal


def _expected(current, enabled, **host):
    """current as proposed: next revision, draft, host-1 updated, lane-gpu1 enabled or not, the rest unchanged."""
    expected = copy.deepcopy(current)
    expected.update(revision=current["revision"] + 1, stage="draft")
    expected["hosts"][1].update(host)
    expected["lanes"][0]["enabled"] = enabled
    return expected


def test_v2_projection_takes_embedded_inventory():
    probe = ReplayGPUProbe("titan-rtx")
    current = _current(probe, probe.inventory(probe.test_host, timeout_s=10)["devices"])
    proposal = _propose(current, probe)
    inventory = project_v2(proposal)
    assert inventory == proposal["inventory"] and inventory is not proposal["inventory"]
    assert_valid(inventory, "inventory")
    assert inventory["stage"] == "draft" and inventory["revision"] == current["revision"] + 1
    unchanged = copy.deepcopy(proposal)
    inventory["hosts"][1]["devices"][0]["model"] = "changed"
    inventory["lanes"].clear()
    assert proposal == unchanged
    confirmed = copy.deepcopy(proposal)
    confirmed["inventory"]["stage"] = "confirmed"
    for refused in (dict(proposal, schema_version=1), confirmed, current, [proposal]):
        with pytest.raises(ProjectionError):
            project_v2(refused)


def test_enabled_lane_requires_uuid():
    inventory = copy.deepcopy(EXAMPLE)
    assert inventory_problems(inventory) == [] == inventory_semantics(inventory)
    device = inventory["hosts"][1]["devices"][0]
    device["uuid"], device["unknown_reasons"]["uuid"] = None, "probe returned no uuid"
    assert_valid(inventory, "inventory")
    problems = inventory_problems(inventory)
    assert len(problems) == 1 == len(inventory_semantics(inventory)) and "lane-gpu1" in problems[0] and "gpu-b" in problems[0]
    inventory["lanes"][0]["enabled"] = False
    assert inventory_problems(inventory) == [] == inventory_semantics(inventory)
    for breaks in (lambda i: i["hosts"][0].update(host_id="host-1"), lambda i: i["hosts"][0]["devices"][0].update(device_id="gpu-b"),
                   lambda i: i["hosts"][0]["devices"][0].update(pci_bus_id=None),
                   lambda i: i["lanes"].append(dict(i["lanes"][0], host_id="host-0", device_ids=["gpu-a"])),
                   lambda i: i["lanes"][0].update(host_id="host-9"), lambda i: i["lanes"][0].update(device_ids=["gpu-a"]),
                   lambda i: i["lanes"].append(dict(i["lanes"][0], lane_id="lane-gpu2")),
                   lambda i: i["hosts"][1].update(reachability="unknown"), lambda i: i["controller"].update(auth_state="unresolved"),
                   lambda i: i["endpoint_lane_order"].append("lane-gpu9")):  # one broken rule each: both checkers object
        broken = copy.deepcopy(EXAMPLE)
        breaks(broken)
        assert_valid(broken, "inventory")
        assert inventory_problems(broken) and inventory_semantics(broken)

    probe = ReplayGPUProbe("quadro-rtx-8000")
    current = _current(probe, probe.inventory(probe.test_host, timeout_s=10)["devices"])
    device = current["hosts"][1]["devices"][0]
    device["uuid"], device["unknown_reasons"]["uuid"] = None, "probe returned no uuid"
    proposal = _propose(current, probe)
    assert proposal["inventory"]["lanes"][0]["enabled"] is False
    assert proposal["lane_review"] == {"lane-gpu1": "review", "lane-gpu0": "retain"} and proposal["status"] == "needs_review"
    assert inventory_problems(proposal["inventory"]) == [] == inventory_semantics(proposal["inventory"])


def test_discover_against_replayed_real_captures():
    assert CAPTURES == ["quadro-rtx-8000", "tesla-t4", "titan-rtx", "titan-rtx-fault"]
    for capture in CAPTURES:
        probe = ReplayGPUProbe(capture)
        observation = probe.inventory(probe.test_host, timeout_s=10)
        cards, seen = observation["devices"], observation["observed_at"]
        assert observation["status"] == "ok" and cards, capture
        current = _current(probe, cards)
        same = _propose(current, probe)
        assert same["observations"] == [observation] and same["status"] == "proposed"
        assert same["inventory"] == _expected(current, True, reachability="confirmed", observed_at=seen, gpu_count=len(cards))
        assert same["host_review"] == {probe.test_host: "admissible"}
        assert same["lane_review"] == {"lane-gpu1": "retain", "lane-gpu0": "retain"}
        assert inventory_problems(same["inventory"]) == [] == inventory_semantics(same["inventory"])

        extra = copy.deepcopy(current)  # a recorded card the probe does not see is kept unchanged, for review
        gone = dict(cards[0], uuid="GPU-00000000-0000-0000-0000-000000000011", pci_bus_id="0000:ff:00.0")
        extra["hosts"][1]["devices"].append(device_from_probe("gpu-9", gone, True))
        proposal = _propose(extra, probe)
        assert proposal["inventory"] == _expected(extra, True, reachability="confirmed", observed_at=seen, gpu_count=len(cards))
        assert proposal["host_review"] == {probe.test_host: "review"} and proposal["lane_review"]["lane-gpu1"] == "retain"

        for key, recorded in (("device_minor", 99), ("pci_bus_id", "0000:ff:00.0"), ("numa_node", 9)):
            moved = copy.deepcopy(current)  # one recorded binding field of gpu-0 alone differs; lane-gpu1 holds gpu-0 only
            moved["hosts"][1]["devices"][0][key] = recorded
            moved["lanes"][0]["device_ids"] = ["gpu-0"]
            proposal = _propose(moved, probe)
            assert proposal["inventory"] == _expected(moved, False, reachability="confirmed", observed_at=seen,
                                                      gpu_count=len(cards), devices=current["hosts"][1]["devices"])
            assert proposal["host_review"] == {probe.test_host: "review"} and proposal["lane_review"]["lane-gpu1"] == "review"

        unknown = copy.deepcopy(current)  # the current inventory knows none of the cards
        unknown["hosts"][1]["devices"] = []
        proposal = _propose(unknown, probe)
        devices = proposal["inventory"]["hosts"][1]["devices"]
        assert devices == [{"device_id": "gpu-" + card["uuid"].removeprefix("GPU-"), "vendor": "nvidia", "model": card["name"],
                            "vram_bytes": card["memory_total_mib"] * 1048576, "driver": card["driver_version"],
                            "uuid": card["uuid"], "pci_bus_id": card["pci_bus_id"], "numa_node": card["numa_node"],
                            "device_minor": card["device_minor"], "drives_display": None,
                            "unknown_reasons": {"drives_display": device["unknown_reasons"].get("drives_display")}}
                           for card, device in zip(sorted(cards, key=lambda card: card["uuid"]), devices, strict=True)]
        assert proposal["host_review"] == {probe.test_host: "review"} and proposal["inventory"]["lanes"][0]["enabled"] is False

        for fault in ("timeout", "exit-9", "unparsable", "na-only"):
            probe.script_next(fault)
            proposal = _propose(current, probe)
            failed = proposal["observations"][0]
            assert failed["status"] == "unknown" and failed["reason"], fault
            assert proposal["inventory"] == _expected(current, False, reachability="unknown", observed_at=failed["observed_at"],
                                                      observation_error=failed["reason"], gpu_count=None,
                                                      gpu_count_reason=failed["reason"])
            assert proposal["host_review"] == {probe.test_host: "unknown"} and proposal["lane_review"]["lane-gpu1"] == "review"
            again = _propose(project_v2(proposal), probe)  # the host answers again: errors clear, the lane is retained disabled
            assert again["inventory"]["hosts"] == same["inventory"]["hosts"] and again["lane_review"]["lane-gpu1"] == "retain"
            assert again["inventory"]["lanes"][0]["enabled"] is False  # discovery never enables a lane

    record = device_from_probe("gpu-x", dict(cards[0], numa_node=None, device_minor=None), False)
    assert (record["numa_node"], record["device_minor"], sorted(record["unknown_reasons"])) == (None, None, ["device_minor", "numa_node"])
    spy = _Recording(ReplayGPUProbe())
    for timeout_s in (0, -1.5, float("nan"), float("inf"), True, "10", None, 10**400):
        with pytest.raises(DiscoveryError):
            propose_v2(current, spy, clock=FakeClock(), timeout_s=timeout_s)
    assert spy.calls == []


@pytest.mark.realtime
@pytest.mark.onlab
@pytest.mark.skipif(TARGET is None, reason="on-lab: set FLIGHTCTL_CONFORMANCE_TARGET to a real GPU host (read-only)")
def test_discover_live_readonly():
    runner = LocalCommandRunner() if TARGET == "host-local" else SshCommandRunner({TARGET: TARGET})
    probe = NvidiaInventoryProbe(runner, clock=RealClock(), local_host_id="host-local")
    current = copy.deepcopy(EXAMPLE)
    host = dict(current["hosts"][0], host_id=TARGET, roles=["gpu"], ssh_endpoint=TARGET, devices=[], gpu_count=0)
    current.update(hosts=[host], lanes=[], endpoint_lane_order=[])
    proposal = propose_v2(current, probe, clock=RealClock(), timeout_s=30)
    print("DISCOVERY-PROPOSAL " + json.dumps(proposal))
    if os.environ.get("FLIGHTCTL_CONFORMANCE_EVIDENCE"):
        evidence = Path(os.environ["FLIGHTCTL_CONFORMANCE_EVIDENCE"])
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / f"discovery-proposal-{TARGET}.json").write_text(json.dumps(proposal, indent=2) + "\n", encoding="utf-8")
    assert_valid(proposal, "discovery")
    (observation,) = proposal["observations"]
    assert observation["status"] == "ok" and observation["devices"], observation
    devices = proposal["inventory"]["hosts"][0]["devices"]
    assert {card["uuid"] for card in observation["devices"]} <= {device["uuid"] for device in devices}
    assert inventory_problems(proposal["inventory"]) == [] == inventory_semantics(proposal["inventory"])
