"""CommandRunner real twins (A2): the single, bounded, no-shell seam through which real twins start processes.

LocalCommandRunner runs an argv list on this host; SshCommandRunner runs it on a named host through
`ssh -o BatchMode=yes -o ConnectTimeout=N destination -- <argv, each element shell-quoted once>`;
RecordingCommandRunner wraps either and appends golden captures for the replay fake (tests/fakes/replay.py).
Every expected failure is returned as a result with a typed error (common.schema.json#/$defs/typed_error);
only invalid arguments raise.
"""

import hashlib as _hashlib
import json as _json
import math as _math
import os as _os
import re as _re
import shlex as _shlex
import signal as _signal
import subprocess as _subprocess
import time as _time
from collections.abc import Mapping as _Mapping

SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_PART = _re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _check_args(argv, timeout_s, stdin, host_id) -> list[str]:
    """Validate run() arguments (raise before anything starts); return a new argv list."""
    if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(a, str) for a in argv):
        raise TypeError("argv must be a non-empty list or tuple of str")
    if not argv[0] or any("\x00" in a for a in argv):
        raise ValueError("argv[0] must be non-empty and no element may contain NUL")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        raise TypeError("timeout_s must be an int or float")
    if not _math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("timeout_s must be finite and > 0")
    if stdin is not None and not isinstance(stdin, bytes):
        raise TypeError("stdin must be None or bytes")
    if host_id is not None and not isinstance(host_id, str):
        raise TypeError("host_id must be None or str")
    return list(argv)


def _error(code: str, message: str, layer: str) -> dict:
    return {"code": code, "message": message[:1024] or code, "layer": layer, "cause": None}


def _result(argv, host_id, *, returncode=None, stdout="", stderr="", timed_out=False, duration_s=0.0, error=None) -> dict:
    return {"argv": list(argv), "host_id": host_id, "returncode": returncode, "stdout": stdout, "stderr": stderr,
            "timed_out": timed_out, "duration_s": duration_s, "error": error}


def _execute(command, argv, host_id, timeout_s, stdin, env) -> dict:
    """Start `command` (no shell, own session, clean env, cwd /) and bound it by timeout_s."""
    started = _time.monotonic()
    try:
        proc = _subprocess.Popen(command, shell=False, env=env, cwd="/", close_fds=True, start_new_session=True,
                                 stdin=_subprocess.DEVNULL if stdin is None else _subprocess.PIPE,
                                 stdout=_subprocess.PIPE, stderr=_subprocess.PIPE)
    except OSError as exc:
        return _result(argv, host_id, stderr=str(exc), duration_s=_time.monotonic() - started,
                       error=_error("unavailable", f"cannot start {command[0]!r}: {exc}", "runner"))
    timed_out = False
    try:
        out, err = proc.communicate(stdin, timeout=timeout_s)
    except _subprocess.TimeoutExpired:
        timed_out = True
        try:
            _os.killpg(proc.pid, _signal.SIGKILL)
        except OSError:
            pass
        try:  # a grandchild that left the group may still hold the pipes: bound the drain too
            out, err = proc.communicate(timeout=1)
        except _subprocess.TimeoutExpired as exc:
            out, err = exc.output or b"", exc.stderr or b""
    finally:
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass
        proc.wait()
    result = _result(argv, host_id, returncode=None if timed_out else proc.returncode, timed_out=timed_out,
                     stdout=out.decode("utf-8", errors="replace"), stderr=err.decode("utf-8", errors="replace"),
                     duration_s=_time.monotonic() - started)
    if timed_out:
        result["error"] = _error("timeout", f"{argv[0]!r} did not finish within timeout_s={timeout_s}", "runner")
    return result


def _local_env() -> dict:
    return {"PATH": SAFE_PATH, "LC_ALL": "C"}


class LocalCommandRunner:
    """Runs argv on this host; a host_id that is not None is refused as invalid."""

    def run(self, argv, *, timeout_s, stdin=None, host_id=None) -> dict:
        argv = _check_args(argv, timeout_s, stdin, host_id)
        if host_id is not None:
            return _result(argv, host_id, error=_error("invalid", f"LocalCommandRunner cannot reach host {host_id!r}", "runner"))
        return _execute(argv, argv, None, timeout_s, stdin, _local_env())


def _bounded_int(value, low, high, name) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int")
    if not low <= value <= high:
        raise ValueError(f"{name} must be {low}-{high}")


class SshCommandRunner:
    """Runs argv on a host named in `endpoints` (host_id -> 'host' or 'user@host') over BatchMode ssh;
    host_id None runs locally exactly as LocalCommandRunner."""

    def __init__(self, endpoints, *, connect_timeout_s=5, port=None, ssh_config=None, ssh_program="ssh"):
        if not isinstance(endpoints, _Mapping):
            raise TypeError("endpoints must map host_id to an ssh destination")
        for host_id, destination in endpoints.items():
            if not isinstance(host_id, str) or not isinstance(destination, str):
                raise TypeError("endpoints must map str host_id to str destination")
            parts = destination.split("@")
            if len(parts) > 2 or not all(_PART.fullmatch(part) for part in parts):
                raise ValueError(f"ssh destination {destination!r} must be host or user@host")
        _bounded_int(connect_timeout_s, 1, 3600, "connect_timeout_s")
        if port is not None:
            _bounded_int(port, 1, 65535, "port")
        if ssh_config is not None and not isinstance(ssh_config, str):
            raise TypeError("ssh_config must be None or str")
        if not isinstance(ssh_program, str) or not ssh_program:
            raise ValueError("ssh_program must be a non-empty str")
        self._endpoints, self._connect_timeout_s = dict(endpoints), connect_timeout_s
        self._port, self._ssh_config, self._ssh_program = port, ssh_config, ssh_program

    def run(self, argv, *, timeout_s, stdin=None, host_id=None) -> dict:
        argv = _check_args(argv, timeout_s, stdin, host_id)
        if host_id is None:
            return _execute(argv, argv, None, timeout_s, stdin, _local_env())
        if host_id not in self._endpoints:
            return _result(argv, host_id, error=_error("invalid", f"no ssh endpoint for host {host_id!r}", "runner"))
        command = [self._ssh_program] + (["-F", self._ssh_config] if self._ssh_config is not None else [])
        command += ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={self._connect_timeout_s}"]
        command += (["-p", str(self._port)] if self._port is not None else [])
        command += [self._endpoints[host_id], "--", " ".join(_shlex.quote(a) for a in argv)]
        env = _local_env()
        if "SSH_AUTH_SOCK" in _os.environ:
            env["SSH_AUTH_SOCK"] = _os.environ["SSH_AUTH_SOCK"]
        result = _execute(command, argv, host_id, timeout_s, stdin, env)
        if result["returncode"] == 255:
            lines = [line for line in result["stderr"].splitlines() if line.strip()]
            result["error"] = _error("transport_failed", lines[-1] if lines else "ssh exited 255", "transport")
        return result


class RecordingCommandRunner:
    """Passes each call to `inner` and appends one golden capture line (eight result keys + stdin_sha256)."""

    def __init__(self, inner, capture_path):
        self._inner, self._capture_path = inner, capture_path

    def run(self, argv, *, timeout_s, stdin=None, host_id=None) -> dict:
        _check_args(argv, timeout_s, stdin, host_id)
        result = self._inner.run(argv, timeout_s=timeout_s, stdin=stdin, host_id=host_id)
        capture = dict(result, stdin_sha256=None if stdin is None else _hashlib.sha256(stdin).hexdigest())
        with open(self._capture_path, "a", encoding="utf-8") as handle:
            handle.write(_json.dumps(capture, sort_keys=True) + "\n")
        return result


def register_command_runners(registry, *, endpoints, capture_path=None, connect_timeout_s=5, ssh_config=None) -> None:
    """Register command_runner=real (and =record when capture_path is given) with an adapters.Registry."""

    def real():
        return SshCommandRunner(endpoints, connect_timeout_s=connect_timeout_s, ssh_config=ssh_config)

    registry.register("command_runner", "real", real)
    if capture_path is not None:
        registry.register("command_runner", "record", lambda: RecordingCommandRunner(real(), capture_path))
