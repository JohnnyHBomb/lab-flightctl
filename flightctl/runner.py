"""WorkloadRunner real twin (A6): transient `systemd --user` units over a CommandRunner (unit.schema.json).
run_id travels in the unit environment (FLIGHTCTL_RUN_ID); invocation_id is systemd's InvocationID. One deadline per call;
failures, timeouts and unparsable output are `unknown` or ok=False with a typed error; only invalid arguments raise."""

import math as _math
import re as _re
import shlex as _shlex
import time as _time
from collections.abc import Mapping as _Mapping
from datetime import timezone as _timezone

_UNIT = _re.compile(r"flightctl-[a-z][a-z0-9._-]{0,63}-g[1-9][0-9]{0,17}\.service")
_PUBLIC_ID = _re.compile(r"[a-z]{2,10}-[A-Za-z0-9_-]{6,64}")
_INVOCATION = _re.compile(r"[0-9a-f]{32}")
_LANE = _re.compile(r"[a-z][a-z0-9._-]{0,63}")
_ENV_KEY = _re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_GPU = _re.compile(r"GPU-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_DIGITS = _re.compile(r"[0-9]+")
_STATUS = _re.compile(r"-?[0-9]+")
_PROPS = "LoadState,ActiveState,SubState,Result,InvocationID,ControlGroup,MainPID,ExecMainStatus,Environment".split(",")
_STATES = {"active": "active", "reloading": "active", "activating": "starting", "deactivating": "deactivating",
           "inactive": "inactive", "failed": "failed"}


def _match(value, pattern, name, optional=False):
    if optional and value is None:
        return
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a str")
    if not pattern.fullmatch(value):
        raise ValueError(f"{name} {value[:80]!r} is not valid")


def _check_timeout(timeout_s):
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        raise TypeError("timeout_s must be an int or float")
    if not _math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("timeout_s must be finite and > 0")


def _err(code, message, cause=None):
    return {"code": code, "message": message[:1024] or code, "layer": "runner", "cause": cause}


def _parse_show(text):
    """The nine properties of `systemctl show`, or None when the output is unparsable or contradictory."""
    seen = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition("=")
        if not sep or key not in _PROPS or key in seen:
            return None
        seen[key] = value
    if len(seen) != len(_PROPS) or seen["LoadState"] not in ("loaded", "not-found") or seen["ActiveState"] not in _STATES:
        return None
    inv, cg, status = seen["InvocationID"], seen["ControlGroup"], seen["ExecMainStatus"]
    if (len(seen["SubState"]) > 64 or len(seen["Result"]) > 64 or (inv and not _INVOCATION.fullmatch(inv))
            or not _DIGITS.fullmatch(seen["MainPID"]) or (status and not _STATUS.fullmatch(status))
            or (cg and (not cg.startswith("/") or ".." in cg.split("/") or len(cg) > 4096))):
        return None
    try:
        tokens = _shlex.split(seen["Environment"])
    except ValueError:
        return None
    run_id = next((t[len("FLIGHTCTL_RUN_ID="):] for t in tokens if t.startswith("FLIGHTCTL_RUN_ID=")), None)
    if (run_id is not None and not _PUBLIC_ID.fullmatch(run_id)) or (seen["LoadState"] == "not-found" and (inv or cg)):
        return None
    seen.update(run_id=run_id, inv=inv or None, cg=cg or None, status=int(status) if status else None)
    return seen


class SystemdUserRunner:
    """WorkloadRunner over transient units of the user manager; `runner` is a CommandRunner, `clock` gives utc()."""

    def __init__(self, runner, *, clock, runtime_dir=None):
        if not callable(getattr(runner, "run", None)) or not callable(getattr(clock, "utc", None)):
            raise TypeError("runner must have a callable run and clock a callable utc")
        if runtime_dir is not None and not isinstance(runtime_dir, str):
            raise TypeError("runtime_dir must be None or str")
        if runtime_dir is not None and (not runtime_dir.startswith("/") or "\x00" in runtime_dir):
            raise ValueError("runtime_dir must start with / and hold no NUL")
        self._runner, self._clock, self._runtime_dir = runner, clock, runtime_dir

    def _run(self, argv, deadline, fail_code, prefix=True):
        """Run one command with the time left; return (stdout, None) on exit 0 or (None, typed error)."""
        left = deadline - _time.monotonic()
        if left <= 0:
            return None, _err("timeout", f"no time left to run {argv[0]}")
        if prefix and self._runtime_dir is not None:
            argv = ["env", "XDG_RUNTIME_DIR=" + self._runtime_dir] + argv
        res = self._runner.run(argv, timeout_s=left)
        res = res if isinstance(res, _Mapping) else {}
        cause = res.get("error") if isinstance(res.get("error"), _Mapping) else None
        what = " ".join(argv[:5])[:200]
        if res.get("timed_out") is True or (cause or {}).get("code") == "timeout":
            return None, _err("timeout", f"{what} timed out", cause)
        if cause is not None or res.get("returncode") != 0 or not isinstance(res.get("stdout"), str):
            lines = [x for x in str(res.get("stderr", "")).splitlines() if x.strip()]
            return None, _err(fail_code, f"{what} failed (exit {res.get('returncode')}): {lines[-1] if lines else ''}", cause)
        return res["stdout"], None

    def _observation(self, unit, state, **fields):
        obs = {"kind": "unit-observation", "unit": unit, "run_id": None, "invocation_id": None, "state": state,
               "load_state": None, "active_state": None, "sub_state": None, "result": None, "main_pid": None,
               "exit_status": None, "cgroup": None, "cgroup_pids": [], "cgroup_empty": None,
               "observed_at": self._clock.utc().astimezone(_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "error": None}
        obs.update(fields)
        return obs

    def _inspect(self, unit, run_id, deadline):
        out, error = self._run(["systemctl", "--user", "show", unit, "--property=" + ",".join(_PROPS)], deadline, "probe_failed")
        seen = None if error else _parse_show(out)
        if seen is None:
            return self._observation(unit, "unknown", error=error or _err("unknown", "unparsable or contradictory systemctl show output"))
        absent = seen["LoadState"] == "not-found"
        if not absent and run_id is not None and seen["run_id"] != run_id:
            return self._observation(unit, "unknown", error=_err("identity_mismatch", f"unit run_id {seen['run_id']!r} is not {run_id!r}"))
        fields = {"run_id": seen["run_id"], "load_state": seen["LoadState"], "active_state": seen["ActiveState"],
                  "sub_state": seen["SubState"] or None, "result": seen["Result"] or None,
                  "main_pid": int(seen["MainPID"]), "exit_status": seen["status"], "cgroup_empty": True}
        if absent:
            return self._observation(unit, "absent", **fields)
        fields.update(invocation_id=seen["inv"], cgroup=seen["cg"])
        if seen["cg"] is not None:
            out, error = self._run(["cat", f"/sys/fs/cgroup{seen['cg']}/cgroup.procs"], deadline, "probe_failed", prefix=False)
            tokens = [] if error else out.split()
            if error or not all(_DIGITS.fullmatch(t) and int(t) >= 1 for t in tokens):
                return self._observation(unit, "unknown", error=error or _err("probe_failed", "unparsable cgroup.procs"))
            fields.update(cgroup_pids=sorted({int(t) for t in tokens}), cgroup_empty=not tokens)
        return self._observation(unit, _STATES[seen["ActiveState"]], **fields)

    def inspect(self, unit, run_id, *, timeout_s):
        _match(unit, _UNIT, "unit")
        _match(run_id, _PUBLIC_ID, "run_id", optional=True)
        _check_timeout(timeout_s)
        return self._inspect(unit, run_id, _time.monotonic() + timeout_s)

    def start(self, unit, run_id, argv, *, work_id, lease_id, lane_id, parent_lease_id, env, run_as, workdir, cards,
              grace_s, timeout_s):
        _match(unit, _UNIT, "unit")
        for name, value in (("run_id", run_id), ("work_id", work_id), ("lease_id", lease_id)):
            _match(value, _PUBLIC_ID, name)
        _match(parent_lease_id, _PUBLIC_ID, "parent_lease_id", optional=True)
        _match(lane_id, _LANE, "lane_id")
        if not unit.startswith(f"flightctl-{lane_id}-g"):
            raise ValueError(f"unit {unit!r} does not belong to lane {lane_id!r}")
        if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(a, str) for a in argv):
            raise TypeError("argv must be a non-empty list or tuple of str")
        if not all(a and "\x00" not in a for a in argv):
            raise ValueError("argv elements must be non-empty and hold no NUL")
        if not isinstance(env, _Mapping) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise TypeError("env must map str to str")
        env = dict(env)
        for key, value in env.items():
            if not _ENV_KEY.fullmatch(key) or key in ("FLIGHTCTL_RUN_ID", "CUDA_VISIBLE_DEVICES") or "\x00" in value or "\n" in value:
                raise ValueError(f"env entry {key[:80]!r} is not allowed")
        if not isinstance(run_as, str) or not isinstance(workdir, str):
            raise TypeError("run_as and workdir must be str")
        if run_as or workdir:
            raise ValueError("the user-manager twin never changes account or directory: run_as and workdir must be ''")
        if not isinstance(cards, (list, tuple)):
            raise TypeError("cards must be a list or tuple")
        for card in cards:
            _match(card, _GPU, "card")
        if len(set(cards)) != len(cards):
            raise ValueError("cards must be distinct")
        if isinstance(grace_s, bool) or not isinstance(grace_s, int):
            raise TypeError("grace_s must be an int")
        if not 1 <= grace_s <= 3600:
            raise ValueError("grace_s must be 1-3600")
        _check_timeout(timeout_s)
        deadline = _time.monotonic() + timeout_s
        command = ["systemd-run", "--user", f"--unit={unit}", "--collect", "--property=KillMode=control-group",
                   f"--property=TimeoutStopSec={grace_s}", f"--setenv=FLIGHTCTL_RUN_ID={run_id}",
                   "--setenv=CUDA_VISIBLE_DEVICES=" + ",".join(cards)]
        command += [f"--setenv={key}={env[key]}" for key in sorted(env)] + ["--"] + list(argv)
        _, error = self._run(command, deadline, "unit_failed")
        result = {"kind": "unit-start", "ok": False, "unit": unit, "run_id": run_id, "invocation_id": None,
                  "observation": None, "error": error, "dry_run": False}
        if error is not None:
            return result
        obs = self._inspect(unit, run_id, deadline)
        result.update(invocation_id=obs["invocation_id"], observation=obs)
        if obs["state"] in ("active", "starting") and obs["invocation_id"]:
            result["ok"] = True
        elif obs["state"] == "unknown":
            result["error"] = obs["error"]
        elif obs["state"] == "absent":
            result["error"] = _err("unit_absent", f"{unit} exited and was collected right after start")
        else:
            result["error"] = _err("unit_failed", f"{unit} is {obs['state']} right after start")
        return result

    def stop(self, unit, invocation_id, *, timeout_s):
        _match(unit, _UNIT, "unit")
        _match(invocation_id, _INVOCATION, "invocation_id", optional=True)
        _check_timeout(timeout_s)
        deadline = _time.monotonic() + timeout_s
        first = self._inspect(unit, None, deadline)
        result = {"kind": "unit-stop", "ok": False, "unit": unit, "invocation_id": invocation_id, "observation": first,
                  "error": None, "dry_run": False}
        if first["state"] == "unknown":
            return dict(result, error=first["error"])
        if first["state"] == "absent":
            return dict(result, ok=True)
        if invocation_id is None or invocation_id != first["invocation_id"]:
            return dict(result, error=_err("identity_mismatch", f"{unit} runs invocation {first['invocation_id']!r}, not {invocation_id!r}"))
        _, error = self._run(["systemctl", "--user", "stop", unit], deadline, "unit_failed")
        if error is not None and error["code"] == "timeout":
            return dict(result, observation=None, error=error)
        second = self._inspect(unit, None, deadline)
        if second["state"] in ("absent", "inactive") and second["cgroup_empty"] is True:
            return dict(result, ok=True, observation=second)
        if second["state"] == "unknown":
            error = second["error"]
        elif second["cgroup_empty"] is False:
            error = _err("cgroup_occupied", f"{unit} still has processes in its cgroup after stop")
        else:
            error = _err("unit_failed", f"{unit} is {second['state']} after stop")
        return dict(result, observation=second, error=error)
