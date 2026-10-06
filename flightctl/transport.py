"""ExecutorTransport real twins (A4): run the executor stdio entry point, one JSON request in, one JSON reply out.

LocalSubprocessTransport runs the entry point's argv on this host; SshForcedCommandTransport runs it through a
forced-command ssh key chosen only by the ssh config file (-F). Both go through flightctl.commands and return
{"status", "reply", "error"}: status ok carries the parsed reply object unchanged; timeout, denied, lost, unparsable
and failed carry a typed error (layer transport) whose cause is the runner's own error. Only bad arguments raise.
"""

import json as _json
import math as _math
import re as _re
from collections.abc import Mapping as _Mapping

from flightctl.commands import LocalCommandRunner as _LocalCommandRunner
from flightctl.commands import SshCommandRunner as _SshCommandRunner

_HOST_ID = _re.compile(r"[a-z][a-z0-9._-]{0,63}")
_CODES = {"timeout": "timeout", "denied": "denied", "lost": "reply_lost", "unparsable": "reply_unparsable",
          "failed": "transport_failed"}


def _payload(host_id, request, timeout_s) -> bytes:
    """Validate call() arguments (raise before anything runs) and return the request as UTF-8 JSON."""
    if not isinstance(host_id, str):
        raise TypeError("host_id must be a str")
    if not isinstance(request, _Mapping):
        raise TypeError("request must be a Mapping")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        raise TypeError("timeout_s must be an int or float")
    try:
        finite = _math.isfinite(timeout_s)
    except OverflowError:  # an int too large for a float
        finite = False
    if not finite or timeout_s <= 0:
        raise ValueError("timeout_s must be finite and > 0")
    try:
        return _json.dumps(dict(request), allow_nan=False).encode("utf-8")
    except RecursionError as exc:
        raise ValueError("request is nested too deeply to serialise") from exc


def _failure(status, message, cause=None) -> dict:
    error = {"code": _CODES[status], "message": (message or status)[:1024], "layer": "transport", "cause": cause}
    return {"status": status, "reply": None, "error": error}


def _classify(result) -> dict:
    """Map one runner result to a transport result (the same order for both transports)."""
    cause, returncode, stderr = result["error"], result["returncode"], result["stderr"]
    last = ([line.strip() for line in stderr.splitlines() if line.strip()] or [""])[-1]
    if result["timed_out"] or (cause or {}).get("code") == "timeout":
        return _failure("timeout", (cause or {}).get("message") or "executor call timed out", cause)
    if returncode == 255:
        denied = "Permission denied" in stderr or "Host key verification failed" in stderr
        return _failure("denied" if denied else "lost", last or "ssh exited 255", cause)
    if returncode == 2:
        return _failure("unparsable", last or "the executor could not parse the request", cause)
    if returncode != 0:
        detail = (cause or {}).get("message") or last
        return _failure("failed", detail or f"executor exited with returncode {returncode}", cause)
    try:
        reply = _json.loads(result["stdout"])
    except (ValueError, RecursionError) as exc:  # deeply nested garbage raises RecursionError
        return _failure("unparsable", f"executor reply is not JSON: {exc}", cause)
    if not isinstance(reply, dict):
        return _failure("unparsable", "executor reply is not a JSON object", cause)
    return {"status": "ok", "reply": reply, "error": None}


class LocalSubprocessTransport:
    """Runs the entry point's argv (`command`) on this host for calls addressed to `host_id`."""

    def __init__(self, command, *, host_id):
        if not isinstance(command, (list, tuple)) or not command or not all(isinstance(a, str) and a for a in command):
            raise TypeError("command must be a non-empty list or tuple of non-empty str")
        if any("\x00" in a for a in command):
            raise ValueError("command elements must not contain NUL")
        if not isinstance(host_id, str) or not _HOST_ID.fullmatch(host_id):
            raise ValueError("host_id must match ^[a-z][a-z0-9._-]{0,63}$")
        self._command, self._host_id = list(command), host_id

    def call(self, host_id, request, *, timeout_s) -> dict:
        payload = _payload(host_id, request, timeout_s)
        if host_id != self._host_id:
            return _failure("failed", f"local transport serves host {self._host_id!r}, not {host_id!r}")
        return _classify(_LocalCommandRunner().run(self._command, timeout_s=timeout_s, stdin=payload))


class SshForcedCommandTransport:
    """Runs the entry point on the host named by `endpoints[host_id]` through the forced-command key of `ssh_config`."""

    def __init__(self, endpoints, *, ssh_config, connect_timeout_s=5, port=None):
        if not isinstance(ssh_config, str) or not ssh_config:
            raise TypeError("ssh_config must be a non-empty str")
        self._runner = _SshCommandRunner(endpoints, connect_timeout_s=connect_timeout_s, port=port,
                                         ssh_config=ssh_config)

    def call(self, host_id, request, *, timeout_s) -> dict:
        payload = _payload(host_id, request, timeout_s)
        result = self._runner.run(["flightctl-executor"], timeout_s=timeout_s, stdin=payload, host_id=host_id)
        return _classify(result)
