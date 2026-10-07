"""Inhibitor port twins (A11): keep this host awake for exactly the lease. The real twin holds an idle-block inhibitor
in a transient `systemd --user` unit over a CommandRunner, the dryrun twin reads for real and only records its
mutations, the fake keeps a set. One deadline per call; a failure, timeout or unparsable output is a typed error,
never "released" or an empty list; only invalid arguments raise."""

import math as _math
import re as _re
import time as _time
from collections.abc import Mapping as _Mapping

_LANE = _re.compile(r"[a-z][a-z0-9._-]{0,63}")
_UNIT = _re.compile(r"flightctl-awake-[a-z0-9._-]+-g[0-9]+\.service")
_STOP = ["systemctl", "--user", "stop"]
_LIST = ["systemctl", "--user", "list-units", "--plain", "--no-legend", "--full", "flightctl-awake-*.service"]


def _err(code, message, cause=None):
    return {"code": code, "message": message[:1024] or code, "layer": "runner", "cause": cause}


def _result(unit, held, error=None, dry_run=False):
    return {"held": held, "unit": unit, "error": error, "dry_run": dry_run}


def _hold_command(unit, why):
    return ["systemd-run", "--user", "--unit=" + unit.removesuffix(".service"), "--collect", "systemd-inhibit",
            "--what=idle", "--mode=block", "--who=flightctl", "--why=" + why, "sleep", "infinity"]


def _unit(lane_id, generation):
    if not isinstance(lane_id, str):
        raise TypeError("lane_id must be a str")
    if not _LANE.fullmatch(lane_id):
        raise ValueError(f"lane_id {lane_id[:80]!r} is not valid")
    if isinstance(generation, bool) or not isinstance(generation, int):
        raise TypeError("generation must be an int")
    if generation < 1:
        raise ValueError("generation must be >= 1")
    return f"flightctl-awake-{lane_id}-g{generation}.service"


def _check_timeout(timeout_s):
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        raise TypeError("timeout_s must be an int or float")
    try:
        finite = _math.isfinite(timeout_s)
    except OverflowError:  # an int too large for a float
        finite = False
    if not finite or timeout_s <= 0:
        raise ValueError("timeout_s must be finite and > 0")


class _Port:
    """The Inhibitor port's argument rules, shared by the three twins; a twin implements _hold, _release and _list."""

    what = "idle"

    def hold(self, lane_id, generation, *, why, timeout_s):
        unit = _unit(lane_id, generation)
        if not isinstance(why, str):
            raise TypeError("why must be a str")
        if not why or "\x00" in why:
            raise ValueError("why must be non-empty and hold no NUL")
        _check_timeout(timeout_s)
        return self._hold(unit, why, timeout_s)

    def release(self, lane_id, generation, *, timeout_s):
        unit = _unit(lane_id, generation)
        _check_timeout(timeout_s)
        return self._release(unit, timeout_s)

    def list(self, *, timeout_s):
        _check_timeout(timeout_s)
        return self._list(timeout_s)


class SystemdInhibitor(_Port):
    """Inhibitor real twin over a CommandRunner. With `runtime_dir` every command runs under
    `env XDG_RUNTIME_DIR=<runtime_dir>` (the runner's environment has no user bus). It keeps no state between calls."""

    def __init__(self, runner, *, runtime_dir=None):
        if not callable(getattr(runner, "run", None)):
            raise TypeError("runner must have a callable run")
        if runtime_dir is not None and not isinstance(runtime_dir, str):
            raise TypeError("runtime_dir must be None or str")
        if runtime_dir is not None and (not runtime_dir.startswith("/") or "\x00" in runtime_dir):
            raise ValueError("runtime_dir must start with / and hold no NUL")
        self._runner, self._runtime_dir = runner, runtime_dir

    def _prefixed(self, argv):  # always a new list, so the runner never holds one of this module's constants
        return (["env", "XDG_RUNTIME_DIR=" + self._runtime_dir] if self._runtime_dir is not None else []) + argv

    def _run(self, argv, deadline):
        """Run one command with the time left: (stdout, None) when it succeeded, else (None, a typed error)."""
        what, left = " ".join(argv[:3]), deadline - _time.monotonic()
        if left <= 0:
            return None, _err("timeout", f"no time left to run {what}")
        try:
            res = self._runner.run(self._prefixed(argv), timeout_s=left)
        except Exception as exc:  # a runner that raises proves nothing about the unit
            return None, _err("inhibitor_failed", f"{what} could not run: {exc!r}")
        res = res if isinstance(res, _Mapping) else {}
        error = res.get("error")
        cause = error if isinstance(error, _Mapping) else None
        if res.get("timed_out") is True or (cause or {}).get("code") == "timeout":
            return None, _err("timeout", f"{what} timed out", cause)
        if error is not None or res.get("returncode") != 0 or not isinstance(res.get("stdout"), str):
            lines = [line for line in str(res.get("stderr") or "").splitlines() if line.strip()]
            detail = f"{what} failed (exit {res.get('returncode')}): {lines[-1] if lines else ''}"
            return None, _err("inhibitor_failed", detail, cause)
        return res["stdout"], None

    def _units(self, deadline):
        """The units of `systemctl list-units`: {"units": distinct names, ascending, "error": None} or an error."""
        out, error = self._run(_LIST, deadline)
        if error is not None:
            return {"units": None, "error": error}
        names = [fields[0] for fields in map(str.split, out.splitlines()) if fields]
        if not all(_UNIT.fullmatch(name) for name in names):
            return {"units": None, "error": _err("inhibitor_failed", "unparsable systemctl list-units output")}
        return {"units": sorted(set(names)), "error": None}

    def _hold(self, unit, why, timeout_s):
        _, error = self._run(_hold_command(unit, why), _time.monotonic() + timeout_s)
        return _result(unit, error is None, error)

    def _release(self, unit, timeout_s):
        deadline = _time.monotonic() + timeout_s
        _, stop_error = self._run(_STOP + [unit], deadline)  # never decisive: a unit that is gone makes it exit 5
        listed = self._units(deadline)
        if listed["error"] is not None:
            return _result(unit, True, listed["error"])
        if unit in listed["units"]:
            return _result(unit, True, _err("inhibitor_failed", f"{unit} is still listed after stop", stop_error))
        return _result(unit, False)

    def _list(self, timeout_s):
        return self._units(_time.monotonic() + timeout_s)


class DryRunInhibitor(SystemdInhibitor):
    """Inhibitor dryrun twin: list is the real twin's read; hold and release run no command, they append to `recorded`
    the argv the real twin would pass to runner.run and return the success-shaped result with dry_run True."""

    def __init__(self, runner, *, runtime_dir=None):
        super().__init__(runner, runtime_dir=runtime_dir)
        self.recorded = []

    def _hold(self, unit, why, timeout_s):
        self.recorded.append(self._prefixed(_hold_command(unit, why)))
        return _result(unit, True, dry_run=True)

    def _release(self, unit, timeout_s):
        self.recorded.append(self._prefixed(_STOP + [unit]))
        return _result(unit, False, dry_run=True)


class FakeInhibitor(_Port):
    """Inhibitor fake: an in-memory set of held units, no command and no clock. script_next("refused" or "timeout")
    queues a failure for the next hold or release call, first in, first out: that call changes nothing."""

    def __init__(self):
        self._held, self._scripted = set(), []

    def script_next(self, outcome):
        if outcome not in ("refused", "timeout"):
            raise ValueError("outcome must be 'refused' or 'timeout'")
        self._scripted.append(outcome)

    def _change(self, unit, apply):
        outcome = self._scripted.pop(0) if self._scripted else None
        if outcome is None:
            apply(unit)
            return _result(unit, unit in self._held)
        code = "timeout" if outcome == "timeout" else "inhibitor_failed"
        return _result(unit, unit in self._held, _err(code, f"scripted {outcome}"))

    def _hold(self, unit, why, timeout_s):
        return self._change(unit, self._held.add)

    def _release(self, unit, timeout_s):
        return self._change(unit, self._held.discard)

    def _list(self, timeout_s):
        return {"units": sorted(self._held), "error": None}
