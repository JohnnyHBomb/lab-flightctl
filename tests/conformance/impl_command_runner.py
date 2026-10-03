"""CommandRunner twins for conformance (A2): ReplayCommandRunner over recorded golden captures as the fake;
LocalCommandRunner (target host-local) or SshCommandRunner to the target (the gauge's G3 over ssh) as the real twin."""

from pathlib import Path

from flightctl.commands import LocalCommandRunner, SshCommandRunner
from tests.fakes.replay import ReplayCommandRunner

from . import registry

_CAPTURES = Path(__file__).resolve().parent.parent / "fakes" / "captures" / "command_runner" / "conformance.jsonl"


class _SshToTarget:
    def __init__(self, target):
        self._target, self._runner = target, SshCommandRunner({target: target})

    def run(self, argv, *, timeout_s, stdin=None, host_id=None):
        return self._runner.run(argv, timeout_s=timeout_s, stdin=stdin, host_id=self._target)


registry.register("command_runner", "fake", lambda target: ReplayCommandRunner(str(_CAPTURES)))
registry.register("command_runner", "real", lambda target: LocalCommandRunner() if target == "host-local" else _SshToTarget(target))
