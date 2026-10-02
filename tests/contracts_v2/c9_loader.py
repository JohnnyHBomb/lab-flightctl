"""Amendment 3 rev 3 (Sol 6.1 amd3r2): reference oracle for C9w's seam LOADER, its three-state enablement model and its
import trust boundary. STDLIB ONLY on purpose: the production helper runs as `python3 -I -S -B`, and this module is
exercised under exactly that interpreter by tests/contracts_v2/test_v2_amendment3_boundary.py.

States (c9_loader_state):
  off     every friend flag false and no valid enablement window: no C9 package is read or imported.
  window  every friend flag false and a valid, operator-opened, time-bounded ENABLEMENT WINDOW on this host: only the
          bodies the pre-enablement runbook needs may load (session_probe_body, binding_measure). Friend creation stays
          refused.
  on      the helper's three friend flags all true: the friend bodies may load; the probe ADD path may not (its policy
          refuses while friend_sessions is on).
A mixed flag state (some true, some false: a disable in progress, or a mis-set helper.json) is OFF: fail closed.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import importlib.abc
import importlib.machinery
import json
import os
import stat as st
import sys
from typing import Any, Callable, Iterator, Mapping, Sequence

C9_PACKAGES = ("flightctl/c9/", "helper/c9/")
C9_LOADER_STATES = ("off", "window", "on")
# body -> the loader states in which it may load (every other cell is the refusing default)
C9_BODY_STATES: dict[str, frozenset[str]] = {
    "session_open_body": frozenset({"on"}),
    "friend_unit_start_body": frozenset({"on"}),
    "claim_create_body": frozenset({"on"}),
    "claim_clear_body": frozenset({"on"}),
    "session_probe_body": frozenset({"window"}),
    "binding_measure": frozenset({"window", "on"}),
}
C9_WINDOW_MAX_TTL_S = 3600
PROBE_PATH = "/usr/local/libexec/flightctl-session-probe"
WINDOW_FILE = "c9-enablement-window.json"


# ---------------------------------------------------------------- the enablement window (P1: the probe's working state)
def _flags(cfg: Mapping[str, Any]) -> tuple[bool, bool, bool]:
    return (cfg.get("friend_sessions") is True, cfg.get("friend_sessions_global") is True, cfg.get("friend_sessions_host") is True)


def _operator(invocation: Mapping[str, Any], cfg: Mapping[str, Any], operator_uid: int) -> list[str]:
    problems = []
    if invocation.get("program") != PROBE_PATH or invocation.get("ruid") != 0 or invocation.get("euid") != 0:
        problems.append("the window is opened only through the operator's sudo route to the session-probe program, as root")
    if invocation.get("sudo_user") != cfg.get("operator_account") or invocation.get("sudo_uid") != operator_uid:
        problems.append("started by sudo for the configured operator account only")
    return problems


def c9_window_valid(record: Mapping[str, Any] | None, *, host_id: str, now: float, mono_now: float, boot_id: str) -> bool:
    """A window counts only on its own host, in the same boot, before BOTH its wall and its monotonic deadline."""
    if not record:
        return False
    return (record.get("host_id") == host_id and record.get("boot_id") == boot_id and "expires_mono" in record
            and now < record.get("expires_at", float("-inf")) and mono_now < record["expires_mono"])


def c9_window_read(state_dir: str) -> dict[str, Any] | None:
    path = os.path.join(state_dir, WINDOW_FILE)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError:
        return {"unreadable": True}  # never valid: c9_window_valid needs host_id and boot_id
    with os.fdopen(fd, encoding="utf-8") as handle:
        try:
            return json.load(handle)
        except ValueError:
            return {"unreadable": True}


def c9_window_open(state_dir: str, cfg: Mapping[str, Any], invocation: Mapping[str, Any], operator_uid: int, *, host_id: str,
                   ttl_s: int, now: float, mono_now: float, boot_id: str, audit: list[dict[str, Any]]) -> bool:
    """`sudo flightctl-session-probe --window-open --ttl <s>` (operator route, re-authenticating, as claim-clear). Refused
    while ANY friend flag is true (the window precedes enablement), for any other caller, with ttl outside (0, 3600], and
    while a valid window exists (no extension). Every call is an audit event."""
    reasons = _operator(invocation, cfg, operator_uid)
    if any(_flags(cfg)):
        reasons.append("a friend flag is on: the enablement window exists only before enablement")
    if not (0 < ttl_s <= C9_WINDOW_MAX_TTL_S):
        reasons.append(f"ttl_s must be in (0, {C9_WINDOW_MAX_TTL_S}]")
    if c9_window_valid(c9_window_read(state_dir), host_id=host_id, now=now, mono_now=mono_now, boot_id=boot_id):
        reasons.append("a window is already open (no extension: close it first)")
    if reasons:
        audit.append({"event": "window-open-refused", "reasons": reasons, "at": now, "caller": invocation.get("sudo_user")})
        return False
    record = {"host_id": host_id, "opened_at": now, "expires_at": now + ttl_s, "expires_mono": mono_now + ttl_s, "boot_id": boot_id,
              "opened_by": invocation.get("sudo_user")}
    tmp = os.path.join(state_dir, WINDOW_FILE + ".tmp")
    with contextlib.suppress(FileNotFoundError):
        os.unlink(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(record, handle)
    os.replace(tmp, os.path.join(state_dir, WINDOW_FILE))
    audit.append({"event": "window-opened", "at": now, "expires_at": record["expires_at"], "caller": invocation.get("sudo_user")})
    return True


def c9_window_close(state_dir: str, *, reason: str, now: float, audit: list[dict[str, Any]], caller: str | None) -> bool:
    """Closes the window: `--window-close --result success|failure` (operator), the rollback unit's
    `--sweep --all --close-window` (root) and `--sweep --expired` once it is no longer valid (c9_window_sweep). Rev 4: the
    operator's step-7 `--sweep --all` removes the probe key and leaves a VALID window open for step 8(a)'s measurement; it
    also removes a window record that is already invalid (which refuses measurement anyway; Sol 6.1 amd3r4).
    Idempotent; every call is an audit event."""
    if reason not in {"success", "failure", "expired", "rollback"}:
        raise ValueError(reason)
    path = os.path.join(state_dir, WINDOW_FILE)
    present = os.path.lexists(path)
    if present:
        os.unlink(path)
    audit.append({"event": "window-closed" if present else "window-none-present", "reason": reason, "at": now, "caller": caller})
    return present


def c9_window_sweep(state_dir: str, *, close_window: bool, host_id: str, now: float, mono_now: float, boot_id: str,
                    audit: list[dict[str, Any]], caller: str | None = None) -> bool:
    """The window side of the session-probe program's root-only sweep (rev 4). With --close-window (the rollback unit) the
    window is closed whatever its state; otherwise (--expired, and the plain --all the operator runs in step 7) the window
    is closed only when it is no longer valid (expired, another boot, another host, unreadable)."""
    record = c9_window_read(state_dir)
    if close_window:
        return c9_window_close(state_dir, reason="rollback", now=now, audit=audit, caller=caller)
    if record is not None and not c9_window_valid(record, host_id=host_id, now=now, mono_now=mono_now, boot_id=boot_id):
        return c9_window_close(state_dir, reason="expired", now=now, audit=audit, caller=caller)
    return False


def c9_loader_state(cfg: Mapping[str, Any], window: Mapping[str, Any] | None, *, host_id: str, now: float, mono_now: float,
                    boot_id: str) -> str:
    flags = _flags(cfg)
    if all(flags):
        return "on"
    if any(flags):
        return "off"  # mixed: fail closed, and no window either
    return "window" if c9_window_valid(window, host_id=host_id, now=now, mono_now=mono_now, boot_id=boot_id) else "off"


def c9_body_allowed(body: str, state: str) -> bool:
    if state not in C9_LOADER_STATES:
        raise ValueError(state)
    return state in C9_BODY_STATES.get(body, frozenset())


# ---------------------------------------------------------------- installed artefact (R1)
def c9_absent_audit(installed_relpaths: Sequence[str]) -> list[str]:
    """R1 install audit: no file under a C9 package exists in the installed tree."""
    return [f"{p}: a C9 package file is installed in an R1 release" for p in sorted(installed_relpaths)
            if any(p.startswith(pkg) or p == pkg.rstrip("/") for pkg in C9_PACKAGES)]


def release_stale_removals(installed_relpaths: Sequence[str], manifest_relpaths: Sequence[str]) -> list[str]:
    """Release backend activate (and rollback): every installed file that the new release manifest does not list is removed."""
    keep = set(manifest_relpaths)
    return sorted(p for p in installed_relpaths if p not in keep)


# ---------------------------------------------------------------- import trust boundary (P1)
THIRD_PARTY_DIRS = ("site-packages", "dist-packages")


def _under(path: str, roots: Sequence[str]) -> bool:
    real = os.path.realpath(path)
    return any(real == r or real.startswith(r.rstrip(os.sep) + os.sep) for r in (os.path.realpath(x) for x in roots))


def third_party_locations() -> list[str]:
    """Rev 4: the interpreter's own third-party install locations (sysconfig purelib/platlib). Measured on this host:
    /usr/lib/python3.14/site-packages, NESTED inside the stdlib directory /usr/lib/python3.14."""
    import sysconfig
    paths = sysconfig.get_paths()
    return sorted({paths["purelib"], paths["platlib"]})


def stdlib_trusted(path: str, stdlib_dirs: Sequence[str], excluded: Sequence[str] = ()) -> bool:
    """Rev 4 (Sol 6.1 amd3r3): a file is stdlib only when it lies under one of the strict-startup stdlib entries (the
    stdlib zip, the stdlib directory, lib-dynload: exactly sys.path under python3 -I -S, measured) AND under none of the
    third-party locations (sysconfig purelib/platlib) AND no directory between that entry and the file is named
    site-packages or dist-packages (a nested third-party tree)."""
    real = os.path.realpath(path)
    if excluded and _under(real, excluded):
        return False
    for root in stdlib_dirs:
        r = os.path.realpath(root)
        if real == r or real.startswith(r.rstrip(os.sep) + os.sep):
            between = os.path.relpath(real, r).split(os.sep)[:-1]
            if not any(part in THIRD_PARTY_DIRS for part in between):
                return True
    return False


class C9ImportGuard(importlib.abc.MetaPathFinder):
    """Installed at sys.meta_path[0] before any C9 import. AUTHORITATIVE: it resolves every import itself (builtin, frozen,
    then the path finder over sys.path) and returns that spec only when it is builtin/frozen or its origin (or every
    search location of a namespace package) lies under the stdlib directories or the verified prefix; anything else,
    including a module no trusted finder can locate, raises ImportError, so no later finder is consulted."""

    def __init__(self, stdlib_dirs: Sequence[str], prefix: str, excluded: Sequence[str] | None = None) -> None:
        self.stdlib = list(stdlib_dirs)
        self.prefix = os.path.realpath(prefix)
        self.excluded = third_party_locations() if excluded is None else list(excluded)
        self.refused: list[str] = []

    def trusted(self, location: str) -> bool:
        return _under(location, [self.prefix]) or stdlib_trusted(location, self.stdlib, self.excluded)

    def find_spec(self, name, path, target=None):  # noqa: ANN001
        for finder in (importlib.machinery.BuiltinImporter, importlib.machinery.FrozenImporter):
            spec = finder.find_spec(name, path)
            if spec is not None:
                return spec
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        locations = []
        if spec is not None:
            if spec.origin not in (None, "namespace"):
                locations.append(spec.origin)
            locations += list(spec.submodule_search_locations or [])
        if spec is None or not locations or not all(self.trusted(p) for p in locations):
            self.refused.append(name)
            raise ImportError(f"C9 import guard: {name} is neither stdlib nor inside the verified prefix")
        return spec


@contextlib.contextmanager
def trusted_import_context(stdlib_dirs: Sequence[str], prefix: str, excluded: Sequence[str] | None = None) -> Iterator[C9ImportGuard]:
    """sys.path = stdlib + the verified prefix only; the guard first on sys.meta_path; no bytecode written. Restored on
    exit (production does this once at startup, `python3 -I -S -B`, and never restores)."""
    guard = C9ImportGuard(stdlib_dirs, prefix, excluded)
    saved_path, saved_meta, saved_bc = list(sys.path), list(sys.meta_path), sys.dont_write_bytecode
    sys.path[:] = [*stdlib_dirs, prefix]
    sys.meta_path.insert(0, guard)
    sys.dont_write_bytecode = True
    importlib.invalidate_caches()
    try:
        yield guard
    finally:
        sys.path[:] = saved_path
        sys.meta_path[:] = saved_meta
        sys.dont_write_bytecode = saved_bc
        importlib.invalidate_caches()


def _purge(module: str) -> list[str]:
    gone = [m for m in list(sys.modules) if m == module or m.startswith(module + ".")]
    for m in gone:
        del sys.modules[m]
    return gone


def _verify_tree(prefix: str, pkg: str, manifest: Mapping[str, str], expected_uid: int, trust_root: str) -> str | None:
    """None if the install prefix, every ancestor up to trust_root, and every entry under the prefix pass; else the reason."""
    real = os.path.realpath(prefix)
    stop = os.path.realpath(trust_root)
    if not (real == stop or real.startswith(stop.rstrip(os.sep) + os.sep)):
        return "the prefix is not under the trust root"
    d = real
    while True:  # the prefix itself and every ancestor directory up to the trust root
        info = os.lstat(d)
        if not st.S_ISDIR(info.st_mode) or info.st_uid != expected_uid or info.st_mode & 0o022:
            return f"{d}: prefix or ancestor not an owner-only-writable directory of the expected owner"
        if d == stop:
            break
        if os.path.dirname(d) == d:  # reached / without meeting the trust root (unreachable after the check above)
            return "the prefix is not under the trust root"
        d = os.path.dirname(d)
    if not any(p.startswith(pkg) for p in manifest):
        return f"{pkg} is not in the release manifest (stale or foreign package)"
    seen = set()

    def fail(exc: OSError) -> None:
        raise exc

    for dirpath, dirnames, filenames in os.walk(real, followlinks=False, onerror=fail):
        for name in [*dirnames, *filenames]:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, real).replace(os.sep, "/")
            info = os.lstat(full)
            if st.S_ISLNK(info.st_mode):
                return f"{rel}: symlink in the import tree"
            if info.st_uid != expected_uid or info.st_mode & 0o022:
                return f"{rel}: wrong owner or group/other-writable"
            if st.S_ISDIR(info.st_mode):
                continue
            if not st.S_ISREG(info.st_mode) or rel not in manifest:
                return f"{rel}: not in the release manifest (stale, extra or cache file)"
            fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as handle:
                if hashlib.sha256(handle.read()).hexdigest() != manifest[rel]:
                    return f"{rel}: content differs from the release manifest"
            seen.add(rel)
    missing = sorted(set(manifest) - seen)
    if missing:
        return f"manifest files missing from the install: {missing[:3]}"
    return None


def preloaded_outside_modules(modules: Mapping[str, Any], stdlib_dirs: Sequence[str], prefix: str, excluded: Sequence[str] | None = None) -> list[str]:
    """Rev 4 (Sol 6.1 amd3r2/r3): a module already in sys.modules is returned by import WITHOUT consulting any finder, so
    the guard cannot see it. Every loaded module with a file origin must be stdlib (stdlib_trusted) or inside the
    canonical prefix. Builtin and frozen modules are accepted because they come from the trusted -I -S -B startup;
    a file-less module carries no provenance (it can hold executable code), so accepting one rests entirely on that
    controlled startup, not on the module itself (Sol 6.1 amd3r4)."""
    excl = third_party_locations() if excluded is None else list(excluded)
    real_prefix = os.path.realpath(prefix)
    bad = []
    for name, mod in list(modules.items()):
        spec = getattr(mod, "__spec__", None)
        origin = getattr(spec, "origin", None) or getattr(mod, "__file__", None)
        if origin in (None, "built-in", "frozen", "namespace") or not isinstance(origin, str):
            continue
        if not (_under(origin, [real_prefix]) or stdlib_trusted(origin, stdlib_dirs, excl)):
            bad.append(name)
    return sorted(bad)


def c9_seam_load(prefix: str, package: str, *, state: str, manifest: Mapping[str, str], expected_uid: int, hooks: Sequence[str],
                 stdlib_dirs: Sequence[str] = (), trust_root: str = "/", importer: Callable[[], Any] | None = None,
                 modules: Mapping[str, Any] | None = None, excluded: Sequence[str] | None = None) -> dict[str, Any]:
    """Decide which C9 hooks load. Order:
    (1) only the hooks C9_BODY_STATES allows in `state` are candidates; none (always so in 'off') -> refusing defaults,
        the tree is not even read;
    (2) any cached sys.modules entry for the package is PURGED, so only a verified file can satisfy the import;
    (3) the prefix, its ancestors up to trust_root, and every entry under it are verified against the manifest; any
        OSError refuses;
    (4) only then the import, inside trusted_import_context (stdlib + prefix, guard first); ANY BaseException at import
        time refuses (the helper is non-interactive; refusing is the outcome either way, so the loader never raises) and
        the package is purged again;
    (5) every candidate hook must exist and be callable; only candidates are returned.
    Rev 4: the prefix is canonicalised ONCE; verification and every import use that canonical path (an alias is never
    imported through), and before any import every already-loaded module (sys.modules, or `modules`) must be stdlib or
    inside the canonical prefix (preloaded_outside_modules), else refuse."""
    if state not in C9_LOADER_STATES:
        return {"loaded": False, "hooks": None, "state": state, "reason": f"unknown loader state {state!r}"}
    candidates = [h for h in hooks if c9_body_allowed(h, state)]
    refusing = {"loaded": False, "hooks": None, "state": state}
    if not candidates:
        return {**refusing, "reason": f"no body may load in state {state}: the package is neither read nor imported"}
    pkg = package.strip("/") + "/"
    module = pkg.rstrip("/").replace("/", ".")
    purged = _purge(module)
    real_prefix = os.path.realpath(prefix)
    outside = preloaded_outside_modules(sys.modules if modules is None else modules, stdlib_dirs, real_prefix, excluded)
    if outside:
        return {**refusing, "reason": f"modules loaded before the guard come from outside stdlib and the prefix: {outside[:3]}", "purged": purged}
    try:
        problem = _verify_tree(real_prefix, pkg, manifest, expected_uid, trust_root)
    except OSError as exc:
        problem = f"filesystem error during verification: {type(exc).__name__}"
    if problem:
        return {**refusing, "reason": problem, "purged": purged}
    try:
        with trusted_import_context(stdlib_dirs, real_prefix, excluded):
            loaded = importer() if importer is not None else importlib.import_module(module)
        found = {h: getattr(loaded, h) for h in candidates}
    except BaseException as exc:  # noqa: BLE001 - import-time SystemExit/KeyboardInterrupt also refuse
        _purge(module)
        return {**refusing, "reason": f"import failed: {type(exc).__name__}", "purged": purged}
    if not all(callable(f) for f in found.values()):
        _purge(module)
        return {**refusing, "reason": "a hook is not callable", "purged": purged}
    return {"loaded": True, "hooks": found, "state": state, "reason": "verified and imported", "purged": purged}
