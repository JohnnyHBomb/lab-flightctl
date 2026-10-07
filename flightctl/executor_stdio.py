"""Executor stdio entry point (A4, A5a3): one JSON request on stdin, one JSON reply on stdout, fences kept between invocations.

Serve: executor_stdio.py --state <absolute path> --controller <id> [--host-id <id> --site-dir <absolute path>
[--nvidia-smi <absolute path>]]. This is the forced command of the executor's ssh key and the argv of the local transport.
Reaching it is the authentication: the controller identity comes only from --controller. A schema_version 1 request goes
to the v1 executor (fences in --state). A schema_version 2 request goes to ExecutorV2 as host --host-id (fences in
<--state>.v2) and needs --host-id and --site-dir: its lanes' cards come from the confirmed inventory.json of the
hash-verified site copy in --site-dir, its occupancy probe runs --nvidia-smi (default nvidia-smi), and its controller_id
must be --controller.
Enforce: executor_stdio.py --enforce --state <absolute path> --host-id <id> --site-dir <absolute path> [--nvidia-smi
<absolute path>] is the host timer's one-shot: it reads no stdin, runs the v2 executor's deadline enforcer once and
prints the list of leases it acted on.
Every invocation that answers or enforces holds an exclusive lock on <--state>.lock until its result is written.
Exit codes: 0 one JSON line written; 1 any other failure (one stderr line, nothing on stdout); 2 unparsable request,
including a schema_version that is not the integer 1 or 2 (nothing written anywhere); 64 usage (nothing read or
written); 255 a v2 request from another controller (Permission denied: nothing run, nothing written).
"""

import os as _os
import sys as _sys

if __name__ == "__main__" and not __package__:  # run as a script: our directory holds flightctl.py, use the checkout
    _sys.path[0] = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))

import fcntl as _fcntl
import json as _json
import re as _re

MAX_REQUEST_BYTES = 1048576
_USAGE = ("usage: executor_stdio.py --state <absolute path> --controller <id> [--host-id <id> --site-dir <absolute path> "
          "[--nvidia-smi <absolute path>]] | executor_stdio.py --enforce --state <absolute path> --host-id <id> "
          "--site-dir <absolute path> [--nvidia-smi <absolute path>]")
_OPTIONS = ("--enforce", "--state", "--controller", "--host-id", "--site-dir", "--nvidia-smi")
_FORMS = (  # (required options, optional options): serve without the site, serve with it, enforce
    ({"--state", "--controller"}, set()),
    ({"--state", "--controller", "--host-id", "--site-dir"}, {"--nvidia-smi"}),
    ({"--enforce", "--state", "--host-id", "--site-dir"}, {"--nvidia-smi"}),
)
_HOST_ID = _re.compile(r"[a-z][a-z0-9._-]{0,63}")


class _Unavailable:
    """Systemd and GPU stand-in until the real twins are wired: every call fails closed."""

    def _fail(self, *args, **kwargs):
        return {"ok": False, "status": "unknown", "error": "not wired in the executor stdio entry point"}

    start = stop = inspect = _fail


def _parse_args(argv):
    """Return {option: value} (--enforce: True) of the serve or the enforce form, or None for anything else."""
    options, rest = {}, list(argv)
    while rest:
        name = rest.pop(0)
        if name in options or name not in _OPTIONS:
            return None
        if name == "--enforce":
            options[name] = True
        elif rest and rest[0]:
            options[name] = rest.pop(0)
        else:
            return None
    names = set(options)
    if not any(required <= names <= required | optional for required, optional in _FORMS):
        return None
    if any(not _os.path.isabs(options[name]) for name in ("--state", "--site-dir", "--nvidia-smi") if name in options):
        return None
    if "--host-id" in options and not _HOST_ID.fullmatch(options["--host-id"]):
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
    if type(request.get("schema_version")) is not int or request["schema_version"] not in (1, 2):  # not true, not 2.0
        raise ValueError("schema_version must be the integer 1 or 2")
    return request


def _one_line(prefix, exc):
    return f"{prefix}: {type(exc).__name__}: {exc}".replace("\n", " ").replace("\r", " ")[:1024] + "\n"


def _lane_cards(site_dir, host_id):
    """{lane_id: card UUIDs} of host_id's lanes in the confirmed inventory.json of the verified local site copy in site_dir.

    The copy is verified as siteconfig verifies a local copy and each lane is bound as lane_occupancy binds it; any refusal raises.
    Nothing is written into site_dir."""
    from flightctl import siteconfig as site

    _, entries, files = site._gather(lambda name: site._read_local(site_dir, name), "local copy")
    verified = {"files": files, "sha256": entries}
    inventory = _json.loads(site._get(files, "inventory.json", bytes, "site files").decode("utf-8"))
    if site._get(inventory, "stage", str, "inventory.json") != "confirmed":
        raise site.SiteConfigRefused("inventory.json is not confirmed")
    lanes = site._get(inventory, "lanes", list, "inventory.json")
    ids = [site._get(lane, "lane_id", str, "lane") for lane in lanes if site._get(lane, "host_id", str, "lane") == host_id]
    return {lane_id: site._bind_lane(verified, lane_id)[1] for lane_id in ids}


def _executor_v2(options, lane_cards):
    from flightctl.clock import RealClock
    from flightctl.commands import LocalCommandRunner
    from flightctl.executor import ExecutorV2, JsonStateStore
    from flightctl.gpu import NvidiaOccupancyProbe

    clock, host_id = RealClock(), options["--host-id"]
    probe = NvidiaOccupancyProbe(LocalCommandRunner(), clock=clock, nvidia_smi=options.get("--nvidia-smi", "nvidia-smi"),
                                 local_host_id=host_id)
    return ExecutorV2(clock, host_id=host_id, store=JsonStateStore(options["--state"] + ".v2"), lane_cards=lane_cards, occupancy=probe)


def _answer(options, request, lane_cards):
    """The v1 executor's reply to a schema_version 1 request, ExecutorV2's to a v2 request, or (no request) the enforcer's list."""
    if request is None:
        return _executor_v2(options, lane_cards).enforce_deadlines()
    if request["schema_version"] == 2:
        return _executor_v2(options, lane_cards).handle(request)
    from flightctl.clock import RealClock
    from flightctl.executor import Executor

    controller = options["--controller"]
    executor = Executor(RealClock(), _Unavailable(), _Unavailable(), state_path=options["--state"], trusted_controller=controller)
    return executor.handle(request, authenticated_controller=controller)


def _respond(options, request):
    """Answer the request (None: run the enforcer) holding the --state lock and write the result as one line: 0, else 1."""
    try:
        v1 = request is not None and request["schema_version"] == 1
        cards = None if v1 else _lane_cards(options["--site-dir"], options["--host-id"])  # a refused copy creates nothing
        lock = _os.open(options["--state"] + ".lock", _os.O_RDWR | _os.O_CREAT | _os.O_CLOEXEC, 0o600)
        try:
            _fcntl.flock(lock, _fcntl.LOCK_EX)
            _sys.stdout.write(_json.dumps(_answer(options, request, cards), allow_nan=False) + "\n")
            _sys.stdout.flush()
        finally:
            _os.close(lock)
    except Exception as exc:
        _sys.stderr.write(_one_line("failed", exc))
        return 1
    return 0


def main(argv=None) -> int:
    options = _parse_args(_sys.argv[1:] if argv is None else list(argv))
    if options is None:
        _sys.stderr.write(_USAGE + "\n")
        return 64
    if "--enforce" in options:
        return _respond(options, None)
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
    if request["schema_version"] == 2:
        if request.get("controller_id") != options["--controller"]:
            _sys.stderr.write("Permission denied: the request's controller_id is not this entry point's controller\n")
            return 255
        if "--host-id" not in options:
            _sys.stderr.write("failed: a schema_version 2 request needs --host-id and --site-dir\n")
            return 1
    return _respond(options, request)


if __name__ == "__main__":
    _status = main()
    try:
        _sys.stdout.flush()
    except Exception:  # the reader has gone: send what is left to nowhere, or the exit-time flush fails and exits 120
        _os.dup2(_os.open(_os.devnull, _os.O_WRONLY), 1)
    _sys.exit(_status)
