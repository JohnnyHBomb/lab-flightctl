"""Amendment 14 (G3 evidence names the host it came from; drift plan P15).

The 7 Nov G3 sweep (finding F1, measured) wrote clock evidence with proven:true for a remote target whose boot id was
the RUNNER's: the real clock twin ignored the target. Strict evidence now records the identity actually observed on the
target, through the transport the real twins use (local reads for host-local, BatchMode ssh otherwise), and the gauge
accepts a file only if that identity matches the target it names.

TARGET normalisation: 'host-local' stays as it is. For 'host' or 'user@host', a bare host name (no dot, not an IP
address) becomes the resolver's canonical name when that name is the bare name plus a domain (bare 'host-a' ->
'host-a.example.net'); anything else, or a lookup error, leaves the target unchanged. The default resolver is
getaddrinfo with AI_CANONNAME, which follows the host's resolver search domains.

Identity match: host-local evidence must carry the local identity (the harness reads it on the runner). A remote
target matches when its host part (without user@) equals the observed hostname, or their first DNS labels are equal
(case-insensitive): 'host-a.example.net' matches a kernel hostname 'host-a'. An IP-address target never matches;
name the host.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Mapping
from typing import Any

from flightctl.clock import BOOT_ID_PATH

HOSTNAME_PATH = "/proc/sys/kernel/hostname"
_BOOT_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

Resolver = Callable[[str], str | None]


def canonical_name(name: str) -> str | None:
    return socket.getaddrinfo(name, None, flags=socket.AI_CANONNAME)[0][3] or None


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def normalise_target(raw: str | None, resolve: Resolver = canonical_name) -> str | None:
    if not raw:
        return None
    if raw == "host-local":
        return raw
    user, _, host = raw.rpartition("@")
    if "." in host or _is_ip(host):
        return raw
    try:
        canonical = resolve(host)
    except (OSError, UnicodeError):
        return raw
    if not canonical or not canonical.lower().startswith(host.lower() + "."):
        return raw
    return f"{user}@{canonical}" if user else canonical


def local_identity(hostname_path: str = HOSTNAME_PATH, boot_id_path: str = BOOT_ID_PATH) -> dict[str, str]:
    with open(hostname_path, encoding="utf-8") as handle:
        hostname = handle.read().strip()
    with open(boot_id_path, encoding="ascii") as handle:
        boot_id = handle.read().strip()
    return {"hostname": hostname, "boot_id": boot_id}


def remote_identity(target: str, runner: Any = None) -> dict[str, str]:
    from flightctl.commands import SshCommandRunner

    runner = runner or SshCommandRunner({target: target})
    result = runner.run(["cat", HOSTNAME_PATH, BOOT_ID_PATH], timeout_s=30, host_id=target)
    lines = (result.get("stdout") or "").split()
    if result.get("returncode") != 0 or len(lines) != 2:
        raise OSError(f"cannot read the identity of {target}: returncode {result.get('returncode')}, "
                      f"{(result.get('stderr') or '').strip()[-200:]!r}")
    return {"hostname": lines[0], "boot_id": lines[1]}


def observe(target: str) -> dict[str, str]:
    """The identity of the host the real twins act on for `target` (raises OSError if it cannot be read)."""
    return local_identity() if target == "host-local" else remote_identity(target)


def hostname_matches(target: str, observed_hostname: str) -> bool:
    host = target.rpartition("@")[2].lower()
    seen = observed_hostname.lower()
    if not host or not seen or _is_ip(host):
        return False
    return host == seen or host.split(".")[0] == seen.split(".")[0]


def identity_problems(target: str, observed: Any) -> list[str]:
    if not isinstance(observed, Mapping):
        return ["no observed host identity (hostname, boot_id) recorded"]
    problems = []
    hostname, boot_id = observed.get("hostname"), observed.get("boot_id")
    if not isinstance(hostname, str) or not hostname:
        problems.append("observed hostname missing")
    if not isinstance(boot_id, str) or not _BOOT_ID.fullmatch(boot_id):
        problems.append("observed boot_id missing or not a kernel boot id")
    if not problems and target != "host-local" and not hostname_matches(target, hostname):
        problems.append(f"observed host {hostname!r} is not the target {target!r}")
    return problems


def evidence_problems(record: Mapping[str, Any]) -> list[str]:
    """Why the gauge must NOT count this conformance evidence file for G3 (empty list = it counts)."""
    problems = []
    if record.get("strict") is not True:
        problems.append("not strict")
    if record.get("implementation") not in {"real", "dryrun"}:
        problems.append(f"implementation {record.get('implementation')!r} is not a real or dryrun twin")
    cases = record.get("cases")
    if not isinstance(cases, Mapping) or not cases or not all(v in {"passed", "n/a"} for v in cases.values()):
        problems.append("cases missing, or not all passed / n/a")
    if record.get("proven") is not True:
        problems.append("proven is not true")
    target = record.get("host")
    if not isinstance(target, str) or not target:
        problems.append("no target host")
    else:
        problems += identity_problems(target, record.get("observed"))
    return problems
