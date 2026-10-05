"""FakeRunner (A6b): the WorkloadRunner real twin over an in-memory systemd user manager, per unit and fail-closed.

The fake models only the host: every refusal, command, parse rule, result and decision is SystemdUserRunner's, so a
never-started unit reads absent with an empty cgroup and stopping one unit never changes another. script_next can only
make a command fail: the v1 fake's "unrecognised outcome = success" and "stop clears every occupant" are gone."""

import itertools

from flightctl.runner import SystemdUserRunner
from tests.fakes.clock import FakeClock

_CG = "/user.slice/user-1000.slice/user@1000.service/app.slice/"
_RUN_ID = "--setenv=FLIGHTCTL_RUN_ID="
_KINDS = {"start": ["systemd-run", "--user"], "stop": ["systemctl", "--user", "stop"], "inspect": ["systemctl", "--user", "show"]}
_SHOW = ("LoadState={}\nActiveState={}\nSubState={}\nResult=success\nInvocationID={}\nControlGroup={}\nMainPID={}\n"
         "ExecMainStatus=0\nEnvironment={}\n")


def _result(argv, returncode, stdout="", stderr=""):
    return {"argv": argv, "host_id": None, "returncode": returncode, "stdout": stdout, "stderr": stderr,
            "timed_out": False, "duration_s": 0.0, "error": None}


class _UserManager:
    """The CommandRunner under FakeRunner: answers the real twin's commands per unit as systemd does; others exit 1."""

    def __init__(self, crashing):
        self.units, self.queued, self._starts = {}, {kind: [] for kind in _KINDS}, itertools.count(1)
        self._crashing = [list(program) for program in crashing]

    def run(self, argv, *, timeout_s):
        argv = list(argv)
        queue = next((self.queued[kind] for kind, head in _KINDS.items() if argv[:len(head)] == head), None)
        if queue:
            outcome = queue.pop(0)
            if outcome == "timeout":
                return dict(_result(argv, None), timed_out=True, error={
                    "code": "timeout", "message": f"{argv[0]} timed out (scripted)", "layer": "runner", "cause": None})
            return _result(argv, 1, stderr=outcome)
        match argv:
            case ["systemd-run", "--user", option, *rest] if option.startswith("--unit=") and "--" in rest:
                unit, cut = option[len("--unit="):], rest.index("--")
                if unit in self.units:
                    return _result(argv, 1, stderr=f"Unit {unit} was already loaded or has a fragment file.")
                if rest[cut + 1:] not in self._crashing:  # a crashing program exits at once and --collect removes the unit
                    n, env = next(self._starts), " ".join(o[len("--setenv="):] for o in rest[:cut] if o.startswith(_RUN_ID))
                    self.units[unit] = (f"{n:032x}", 1000 + n, env)
                return _result(argv, 0)
            case ["systemctl", "--user", "show", unit, props] if props.startswith("--property="):
                if unit not in self.units:
                    return _result(argv, 0, _SHOW.format("not-found", "inactive", "dead", "", "", 0, ""))
                invocation, pid, env = self.units[unit]
                return _result(argv, 0, _SHOW.format("loaded", "active", "running", invocation, _CG + unit, pid, env))
            case ["cat", path]:
                pids = [pid for unit, (_, pid, _) in self.units.items() if path == f"/sys/fs/cgroup{_CG}{unit}/cgroup.procs"]
                return _result(argv, 0, f"{pids[0]}\n") if pids else _result(argv, 1, stderr=f"cat: {path}: No such file")
            case ["systemctl", "--user", "stop", unit]:
                if self.units.pop(unit, None) is None:
                    return _result(argv, 5, stderr=f"Failed to stop {unit}: Unit {unit} not loaded.")
                return _result(argv, 0)
        return _result(argv, 1, stderr="not a command of the real twin")


class FakeRunner(SystemdUserRunner):
    """The fake WorkloadRunner: SystemdUserRunner (runtime_dir None) over _UserManager. A start whose program equals one of
    `crashing` exits 0 and leaves the unit not loaded, as --collect does on a real host (start: ok false, unit_absent)."""

    def __init__(self, *, clock=None, crashing=()):
        self._manager = _UserManager(crashing)
        super().__init__(self._manager, clock=FakeClock() if clock is None else clock)

    def script_next(self, method, outcome):
        """Queue `outcome` for the next command of `method`'s kind (start: systemd-run; stop: systemctl stop; inspect:
        systemctl show, those inside start and stop included), first in, first out: "timeout" makes it time out, any
        other outcome makes it exit 1 with `outcome` as stderr; either way the command has no effect."""
        if not isinstance(method, str) or not isinstance(outcome, str):
            raise TypeError("method and outcome must be str")
        if method not in _KINDS:
            raise ValueError(f"method must be start, stop or inspect, not {method[:80]!r}")
        self._manager.queued[method].append(outcome)
