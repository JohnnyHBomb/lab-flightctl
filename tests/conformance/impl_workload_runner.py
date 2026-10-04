"""WorkloadRunner real twin (A6): local with the login's runtime dir (host-local) or over ssh; dryrun and fake are A6b."""

import os

from flightctl.clock import RealClock
from flightctl.commands import LocalCommandRunner, SshCommandRunner
from flightctl.runner import SystemdUserRunner

from . import registry


class SshToTarget:
    def __init__(self, target):
        self._target = target
    def run(self, argv, *, timeout_s):
        return SshCommandRunner({self._target: self._target}).run(argv, timeout_s=timeout_s, host_id=self._target)


def factory(target):
    if target == "host-local":
        runner = SystemdUserRunner(LocalCommandRunner(), clock=RealClock(), runtime_dir=f"/run/user/{os.getuid()}")
    else:
        runner = SystemdUserRunner(SshToTarget(target), clock=RealClock())
    runner.test_lane = "conformance"
    return runner


registry.register("workload_runner", "real", factory)
