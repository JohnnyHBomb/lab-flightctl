"""Site configuration (A4u): the site files are used only when their sha256 matches the manifest (SHA256SUMS) published with them in
the deploy directory on the store host, and every host keeps a verified local copy for when that host is asleep. A mismatch, a failed
read or a malformed manifest is refused and never falls back to the local copy. lane_occupancy takes a lane's card UUIDs from the
hash-verified, confirmed inventory only, never from the probe's own output."""

import hashlib as _hashlib
import json as _json
import os as _os
import re as _re
import sys as _sys
import tempfile as _tempfile
import time as _time

_MANIFEST = "SHA256SUMS"
_LINE = _re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._-]{0,127})")
_GPU = _re.compile(r"GPU-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class SiteConfigRefused(Exception):
    """The site configuration is refused: nothing unverified is used and nothing falls back."""


class _Unreachable(Exception):
    """The store host did not answer a read in time: the verified local copy is used instead."""


def _check_arguments(runner, deploy_dir, local_dir, host_id, timeout_s) -> None:
    if not callable(getattr(runner, "run", None)):
        raise TypeError("runner must have a callable run")
    if host_id is not None and not isinstance(host_id, str):
        raise TypeError("host_id must be None or str")
    for name, path in (("deploy_dir", deploy_dir), ("local_dir", local_dir)):
        if not isinstance(path, str):
            raise TypeError(f"{name} must be a str")
        if not path.startswith("/") or "\x00" in path:
            raise ValueError(f"{name} must start with / and contain no NUL")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
        raise TypeError("timeout_s must be an int or float")
    if not 0 < timeout_s <= _sys.float_info.max:  # false for NaN, infinity and an int too large for a float
        raise ValueError("timeout_s must be finite and > 0")


def _entries(manifest: bytes, where: str) -> dict:
    """The manifest's {name: sha256 hex} in manifest order; SiteConfigRefused when any line is malformed."""
    entries = {}
    for number, line in enumerate(manifest.decode("utf-8", errors="replace").splitlines(), 1):  # bytes that are not UTF-8 never match
        match = _LINE.fullmatch(line)
        if not match or match[2] == _MANIFEST or match[2] in entries:
            raise SiteConfigRefused(f"{where}: {_MANIFEST} line {number} is malformed, repeats a name or names {_MANIFEST}")
        entries[match[2]] = match[1]
    if not entries:
        raise SiteConfigRefused(f"{where}: {_MANIFEST} names no file")
    return entries


def _gather(read, where: str):
    """(manifest bytes, entries, {name: bytes}): the manifest and every file it names, each read by read(name) and verified."""
    manifest = read(_MANIFEST)
    entries = _entries(manifest, where)
    files = {}
    for name, digest in entries.items():
        files[name] = read(name)
        if _hashlib.sha256(files[name]).hexdigest() != digest:
            raise SiteConfigRefused(f"{where}: {name} does not match its sha256 in {_MANIFEST}")
    return manifest, entries, files


def _fetch(runner, deploy_dir, host_id, deadline, name) -> bytes:
    """One `cat` of a deploy file with the time left: _Unreachable when the store host cannot answer, SiteConfigRefused when it fails."""
    left = deadline - _time.monotonic()
    if left <= 0:
        raise _Unreachable()
    result = runner.run(["cat", _os.path.join(deploy_dir, name)], timeout_s=left, host_id=host_id)
    error = result.get("error")
    if result.get("timed_out") or (isinstance(error, dict) and error.get("code") in ("timeout", "transport_failed")):
        raise _Unreachable()
    if result.get("returncode") != 0 or error is not None or not isinstance(result.get("stdout"), str):
        code, stderr = result.get("returncode"), str(result.get("stderr", ""))[:200]
        raise SiteConfigRefused(f"deploy directory: cannot read {name} (exit code {code}): {stderr!r}")
    return result["stdout"].encode("utf-8")


def _read_local(local_dir, name) -> bytes:
    try:
        with open(_os.path.join(local_dir, name), "rb") as handle:
            return handle.read()
    except OSError as exc:
        raise SiteConfigRefused(f"local copy: cannot read {name}: {exc}") from None


def _publish(local_dir, name, data: bytes) -> None:
    """Write one file of the local copy: a temporary file in local_dir, renamed over `name` (nothing outside local_dir is touched)."""
    tmp = None
    try:
        fd, tmp = _tempfile.mkstemp(prefix=".new-", dir=local_dir)
        with _os.fdopen(fd, "wb") as handle:
            handle.write(data)
        _os.replace(tmp, _os.path.join(local_dir, name))
    except OSError as exc:
        raise SiteConfigRefused(f"local copy: cannot write {name}: {exc}") from None
    finally:
        if tmp is not None and _os.path.exists(tmp):
            _os.unlink(tmp)


def load_site(runner, deploy_dir, local_dir, *, host_id=None, timeout_s) -> dict:
    """Read the site files with `cat` through `runner` (one budget of timeout_s for all reads), verify them against SHA256SUMS, keep
    them as the local copy in `local_dir` (the files first, SHA256SUMS last) and return {"source": "deploy", "files": {name: bytes},
    "sha256": {name: hex}}. When the store host does not answer in time the verified local copy is returned ("source": "local")."""
    _check_arguments(runner, deploy_dir, local_dir, host_id, timeout_s)
    deadline = _time.monotonic() + timeout_s
    try:
        manifest, entries, files = _gather(lambda name: _fetch(runner, deploy_dir, host_id, deadline, name), "deploy directory")
    except _Unreachable:
        pass
    else:
        for name, data in (*files.items(), (_MANIFEST, manifest)):
            _publish(local_dir, name, data)
        return {"source": "deploy", "files": files, "sha256": entries}
    _, entries, files = _gather(lambda name: _read_local(local_dir, name), "local copy")
    return {"source": "local", "files": files, "sha256": entries}


def _get(obj, key, kind, what):
    """obj[key], which must be a `kind` (a bool is no int); SiteConfigRefused when obj is no dict or the key or the type is wrong."""
    if not isinstance(obj, dict) or key not in obj:
        raise SiteConfigRefused(f"{what} has no {key}")
    if not isinstance(obj[key], kind) or (kind is int and isinstance(obj[key], bool)):
        raise SiteConfigRefused(f"{what} {key} must be a {kind.__name__}")
    return obj[key]


def _one(entries, key, value, what):
    """The one entry of `entries` whose `key` equals `value`."""
    found = [entry for entry in entries if _get(entry, key, str, what) == value]
    if len(found) != 1:
        raise SiteConfigRefused(f"{len(found)} {what} entries have {key} {repr(value)[:80]}, not exactly one")
    return found[0]


def _bind_lane(site, lane_id) -> tuple:
    """(host_id, uuids, noise_allowlist, noise_cap_mib, lane_noise_mib) of the lane in the verified, confirmed inventory.json."""
    raw = _get(_get(site, "files", dict, "site"), "inventory.json", bytes, "site files")
    if _hashlib.sha256(raw).hexdigest() != _get(_get(site, "sha256", dict, "site"), "inventory.json", str, "site sha256"):
        raise SiteConfigRefused("inventory.json does not match its sha256")
    try:
        inventory = _json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        raise SiteConfigRefused("inventory.json is not JSON") from None
    if _get(inventory, "stage", str, "inventory.json") != "confirmed":
        raise SiteConfigRefused("inventory.json is not confirmed")
    lane = _one(_get(inventory, "lanes", list, "inventory.json"), "lane_id", lane_id, "lane")
    host_id = _get(lane, "host_id", str, "lane")
    host = _one(_get(inventory, "hosts", list, "inventory.json"), "host_id", host_id, "host")
    devices = _get(host, "devices", list, "host")
    uuids = [_get(_one(devices, "device_id", device_id, "device"), "uuid", str, "device")
             for device_id in _get(lane, "device_ids", list, "lane")]
    if not uuids or not all(_GPU.fullmatch(uuid) for uuid in uuids) or len(set(uuids)) != len(uuids):
        raise SiteConfigRefused(f"lane {repr(lane_id)[:80]} needs one or more distinct card UUIDs of the form GPU-<uuid>")
    tenant = _get(_get(lane, "rules", dict, "lane"), "external_tenant", dict, "lane rules")
    allow = [{"argv0": _get(entry, "argv0", str, "noise_allowlist entry"), "uid": _get(entry, "uid", int, "noise_allowlist entry")}
             for entry in _get(tenant, "noise_allowlist", list, "external_tenant")]
    cap, noise = _get(tenant, "noise_cap_mib", int, "external_tenant"), _get(tenant, "lane_noise_mib", int, "external_tenant")
    return host_id, uuids, allow, cap, noise


def lane_occupancy(site, probe, lane_id, *, timeout_s):
    """The occupancy of a lane: the probe is asked once, with the lane's host, card UUIDs and noise thresholds taken from the
    hash-verified, confirmed inventory.json of `site` (as load_site returns it), and its result is returned unchanged."""
    host_id, uuids, allow, cap, noise = _bind_lane(site, lane_id)
    return probe.occupancy(host_id, lane_id, uuids, noise_allowlist=allow, noise_cap_mib=cap, lane_noise_mib=noise, timeout_s=timeout_s)
