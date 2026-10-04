"""InventoryProbe conformance registrations for the real twin and replay fake (A3i)."""

from flightctl.clock import RealClock
from flightctl.commands import LocalCommandRunner, SshCommandRunner
from flightctl.gpu import NvidiaInventoryProbe
from tests.fakes.gpu_replay import ReplayGPUProbe

from . import registry


def _real(target):
    runner = LocalCommandRunner() if target == "host-local" else SshCommandRunner({target: target})
    probe = NvidiaInventoryProbe(runner, clock=RealClock(), local_host_id="host-local")
    probe.test_host = target
    return probe


registry.register("inventory_probe", "real", _real)
registry.register("inventory_probe", "fake", lambda target: ReplayGPUProbe())
