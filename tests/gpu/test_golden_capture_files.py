"""The A3i golden captures replay through the production OccupancyProbe to their stored expected observation,
and the frozen oracle agrees with them (status and emptiness)."""

import datetime
import json
from pathlib import Path

import pytest

from flightctl.gpu import NvidiaOccupancyProbe
from tests.contracts_v2.validation import errors, occupancy_from_capture, occupancy_semantics
from tests.fakes.replay import ReplayCommandRunner

CAPTURES = Path(__file__).resolve().parents[1] / "fakes" / "captures" / "gpu"
EXPECTED = sorted(CAPTURES.glob("*.expected.json"))


class _FixedClock:
    def utc(self):
        return datetime.datetime(2026, 10, 4, tzinfo=datetime.timezone.utc)


def test_one_capture_per_lab_card_model() -> None:
    models = {json.loads(p.read_text())["card_model"].split(" (")[0] for p in EXPECTED}
    assert models == {"NVIDIA TITAN RTX", "Quadro RTX 8000", "Tesla T4"}


@pytest.mark.parametrize("path", EXPECTED, ids=lambda p: p.name)
def test_capture_replays_to_its_expected_observation(path) -> None:
    doc = json.loads(path.read_text())
    probe = NvidiaOccupancyProbe(ReplayCommandRunner(str(CAPTURES / doc["capture"])), clock=_FixedClock(),
                                 local_host_id=doc["local_host_id"])
    obs = probe.occupancy(doc["host_id"], doc["lane_id"], doc["lane_uuids"], noise_allowlist=doc["noise_allowlist"],
                          noise_cap_mib=doc["noise_cap_mib"], lane_noise_mib=doc["lane_noise_mib"], timeout_s=doc["timeout_s"])
    assert obs == doc["expected"]
    assert errors(obs, "gpu-probe", "occupancy_observation") == [] and occupancy_semantics(obs) == []


@pytest.mark.parametrize("path", EXPECTED, ids=lambda p: p.name)
def test_oracle_agrees_on_status_and_emptiness(path) -> None:
    doc = json.loads(path.read_text())
    lines = [json.loads(line) for line in (CAPTURES / doc["capture"]).read_text().splitlines()]
    gpus = next(r for r in lines if any(a.startswith("--query-gpu=uuid,memory.used") for a in r["argv"]))
    procs = next(r for r in lines if any(a.startswith("--query-compute-apps") for a in r["argv"]))
    oracle = occupancy_from_capture(gpus["stdout"], procs["stdout"], returncode=0, lane_id=doc["lane_id"], host_id=doc["host_id"],
                                    observed_at=doc["observed_at"], lane_uuids=doc["lane_uuids"])
    assert (oracle["status"], oracle["empty"]) == (doc["expected"]["status"], doc["expected"]["empty"])
