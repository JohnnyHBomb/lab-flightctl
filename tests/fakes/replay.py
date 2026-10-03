"""ReplayCommandRunner: the CommandRunner fake (A2). Replays golden captures written by
flightctl.commands.RecordingCommandRunner; starts no process, never sleeps, and an uncaptured call is
never a success (returncode None, 'no capture' in stderr, typed error 'unknown')."""

import copy as _copy
import hashlib as _hashlib
import json as _json
import math as _math
import re as _re

from flightctl.commands import _check_args, _error, _result

_RESULT_KEYS = ("argv", "host_id", "returncode", "stdout", "stderr", "timed_out", "duration_s", "error")
_HEX = _re.compile(r"[0-9a-f]{64}")


class CaptureError(ValueError):
    """A capture file that is missing, unreadable, not UTF-8, or holds a bad line."""


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require(ok: bool, reason: str) -> None:
    if not ok:
        raise ValueError(reason)


def _unique_keys(pairs) -> dict:
    """json object_pairs_hook: a duplicate key is refused, never silently resolved to its last value."""
    obj = {}
    for key, value in pairs:
        _require(key not in obj, f"duplicate key {key!r}")
        obj[key] = value
    return obj


def _validate(c) -> None:
    """Raise ValueError naming the first part of capture `c` that breaks the capture format."""
    _require(isinstance(c, dict) and set(c) == {*_RESULT_KEYS, "stdin_sha256"}, "not an object with exactly the nine capture keys")
    _require(isinstance(c["argv"], list) and bool(c["argv"]) and all(isinstance(a, str) for a in c["argv"]),
             "argv must be a non-empty list of str")
    _require(c["host_id"] is None or isinstance(c["host_id"], str), "host_id must be str or null")
    _require(c["stdin_sha256"] is None or (isinstance(c["stdin_sha256"], str) and bool(_HEX.fullmatch(c["stdin_sha256"]))),
             "stdin_sha256 must be 64 lowercase hex characters or null")
    _require(c["returncode"] is None or _is_int(c["returncode"]), "returncode must be int or null")
    _require(isinstance(c["stdout"], str) and isinstance(c["stderr"], str), "stdout and stderr must be str")
    _require(isinstance(c["timed_out"], bool), "timed_out must be bool")
    _require((_is_int(c["duration_s"]) or isinstance(c["duration_s"], float))
             and _math.isfinite(c["duration_s"]) and c["duration_s"] >= 0, "duration_s must be a finite number >= 0")
    _require(c["error"] is None or (isinstance(c["error"], dict) and isinstance(c["error"].get("code"), str)
                                    and isinstance(c["error"].get("message"), str)),
             "error must be null or an object with str code and str message")


class ReplayCommandRunner:
    """Key: (host_id, argv, sha256 of stdin or None). Captures of one key replay in file order; the last repeats."""

    def __init__(self, capture_path):
        try:
            with open(capture_path, "rb") as handle:
                raw = handle.read()
        except OSError as exc:
            raise CaptureError(f"{capture_path}: cannot read captures: {exc}") from exc
        self._captures: dict[tuple, list[dict]] = {}
        self._next: dict[tuple, int] = {}
        for number, line in enumerate(raw.split(b"\n"), 1):
            try:
                text = line.decode("utf-8")
                if not text.strip():
                    continue
                capture = _json.loads(text, object_pairs_hook=_unique_keys)
                _validate(capture)
            except (ValueError, RecursionError) as exc:  # UnicodeDecodeError and JSONDecodeError are ValueErrors
                raise CaptureError(f"{capture_path}: line {number}: {exc}") from exc
            key = (capture["host_id"], tuple(capture["argv"]), capture["stdin_sha256"])
            self._captures.setdefault(key, []).append(capture)

    def run(self, argv, *, timeout_s, stdin=None, host_id=None) -> dict:
        argv = _check_args(argv, timeout_s, stdin, host_id)
        key = (host_id, tuple(argv), None if stdin is None else _hashlib.sha256(stdin).hexdigest())
        found = self._captures.get(key)
        if not found:
            message = f"no capture for argv {argv!r} on host {host_id!r}"
            return _result(argv, host_id, stderr=message, error=_error("unknown", message, "runner"))
        index = self._next.get(key, 0)
        self._next[key] = min(index + 1, len(found) - 1)
        capture = found[index]
        if capture["timed_out"] or capture["duration_s"] > timeout_s:
            return _result(argv, host_id, timed_out=True, duration_s=timeout_s,
                           error=_error("timeout", f"{argv[0]!r} did not finish within timeout_s={timeout_s}", "runner"))
        return {name: _copy.deepcopy(capture[name]) for name in _RESULT_KEYS}
