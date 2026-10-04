"""OccupancyProbe real twin for conformance (A3 part 1): NvidiaOccupancyProbe over LocalCommandRunner (target
host-local) or SshCommandRunner to the target. The lane's cards come from FLIGHTCTL_CONFORMANCE_GPU_UUIDS
(comma-separated); the conformance lane has no noise allow-list. The fake (golden captures) is part 2."""

import os

from flightctl.clock import RealClock
from flightctl.commands import LocalCommandRunner, SshCommandRunner
from flightctl.gpu import NvidiaOccupancyProbe

from . import registry


def _real(target):
    runner = LocalCommandRunner() if target == "host-local" else SshCommandRunner({target: target})
    probe = NvidiaOccupancyProbe(runner, clock=RealClock(), local_host_id="host-local")
    probe.test_host, probe.test_lane, probe.test_noise_allowlist = target, "conformance", []
    probe.test_uuids = os.environ.get("FLIGHTCTL_CONFORMANCE_GPU_UUIDS", "").split(",")
    return probe


registry.register("occupancy_probe", "real", _real)
