"""WorkloadRunner fake and dryrun twin (A6b). The fake's crashing program is unit B's in the frozen crash case; the dryrun
twin reads the target as the real twin does (impl_workload_runner.py): local with the login's runtime dir, or over ssh."""

import os

from flightctl.clock import RealClock
from flightctl.commands import LocalCommandRunner
from flightctl.runner import DryRunSystemdUserRunner
from tests.fakes.runner import FakeRunner

from . import registry
from .impl_workload_runner import SshToTarget


def fake(target):
    runner = FakeRunner(crashing=[["sh", "-c", "exit 3"]])
    runner.test_lane = "conformance"
    return runner


def dryrun(target):
    if target == "host-local":
        runner = DryRunSystemdUserRunner(LocalCommandRunner(), clock=RealClock(), runtime_dir=f"/run/user/{os.getuid()}")
    else:
        runner = DryRunSystemdUserRunner(SshToTarget(target), clock=RealClock())
    runner.test_lane = "conformance"
    return runner


registry.register("workload_runner", "fake", fake)
registry.register("workload_runner", "dryrun", dryrun)
