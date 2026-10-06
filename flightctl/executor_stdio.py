"""Executor stdio entry point (A4): one JSON request on stdin, one JSON reply on stdout, fences kept in --state.

Usage: executor_stdio.py --state <absolute path> --controller <id>. This is the forced command of the executor's ssh
key and the argv of the local transport. Reaching it is the authentication: the controller identity comes only from
--controller. Exit codes: 0 reply written; 2 unparsable request (nothing written anywhere); 64 usage; 1 any other
failure (nothing on stdout). The request and reply pass through unchanged.
"""

import os as _os
import sys as _sys

if __name__ == "__main__" and not __package__:  # run as a script: our directory holds flightctl.py, use the checkout
    _sys.path[0] = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))

import fcntl as _fcntl
import json as _json

MAX_REQUEST_BYTES = 1048576
_USAGE = "usage: executor_stdio.py --state <absolute path> --controller <id>"


class _Unavailable:
    """Systemd and GPU stand-in until the real twins are wired: every call fails closed."""

    def _fail(self, *args, **kwargs):
        return {"ok": False, "status": "unknown", "error": "not wired in the executor stdio entry point"}

    start = stop = inspect = _fail


def _parse_args(argv):
    """Return {"--state": path, "--controller": id}, or None for anything else."""
    if len(argv) != 4:
        return None
    options = {}
    for name, value in (argv[0:2], argv[2:4]):
        if name not in ("--state", "--controller") or name in options:
            return None
        options[name] = value
    if not _os.path.isabs(options["--state"]) or not options["--controller"]:
        return None
    return options


def _reject_constant(name):
    raise ValueError(f"{name} is not allowed")


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _parse_request(raw):
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError(f"request is longer than {MAX_REQUEST_BYTES} bytes")
    request = _json.loads(raw.decode("utf-8"), parse_constant=_reject_constant, object_pairs_hook=_no_duplicates)
    if not isinstance(request, dict):
        raise ValueError("request is not a JSON object")
    return request


def _one_line(prefix, exc):
    return f"{prefix}: {type(exc).__name__}: {exc}".replace("\n", " ").replace("\r", " ")[:1024] + "\n"


def main(argv=None) -> int:
    options = _parse_args(_sys.argv[1:] if argv is None else list(argv))
    if options is None:
        _sys.stderr.write(_USAGE + "\n")
        return 64
    try:
        raw = _sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    except Exception as exc:  # stdin closed or unreadable: one line, exit 1
        _sys.stderr.write(_one_line("failed", exc))
        return 1
    try:
        request = _parse_request(raw)
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        _sys.stderr.write(_one_line("unparsable", exc))
        return 2
    state, controller = options["--state"], options["--controller"]
    try:
        from flightctl.clock import RealClock
        from flightctl.executor import Executor

        lock = _os.open(state + ".lock", _os.O_RDWR | _os.O_CREAT | _os.O_CLOEXEC, 0o600)
        try:
            _fcntl.flock(lock, _fcntl.LOCK_EX)
            executor = Executor(RealClock(), _Unavailable(), _Unavailable(), state_path=state,
                                trusted_controller=controller)
            reply = executor.handle(request, authenticated_controller=controller)
        finally:
            _os.close(lock)
        line = _json.dumps(reply, allow_nan=False) + "\n"
        _sys.stdout.write(line)
        _sys.stdout.flush()
    except Exception as exc:
        _sys.stderr.write(_one_line("failed", exc))
        return 1
    return 0


if __name__ == "__main__":
    _status = main()
    try:
        _sys.stdout.flush()
    except Exception:  # the reader has gone: send what is left to nowhere, or the exit-time flush fails and exits 120
        _os.dup2(_os.open(_os.devnull, _os.O_WRONLY), 1)
    _sys.exit(_status)
