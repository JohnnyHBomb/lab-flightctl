"""Inhibitor twins (A11): the fake always; real and dryrun on the target host (local with the runtime dir, else ssh)."""

import os

from flightctl.commands import LocalCommandRunner
from flightctl.power import DryRunInhibitor, FakeInhibitor, SystemdInhibitor

from . import registry
from .impl_workload_runner import SshToTarget


def _on_target(twin):
    def factory(target):
        if target == "host-local":
            return twin(LocalCommandRunner(), runtime_dir=f"/run/user/{os.getuid()}")
        return twin(SshToTarget(target))
    return factory


registry.register("inhibitor", "fake", lambda target: FakeInhibitor())
registry.register("inhibitor", "real", _on_target(SystemdInhibitor))
registry.register("inhibitor", "dryrun", _on_target(DryRunInhibitor))
