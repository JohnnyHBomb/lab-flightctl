"""Schema loading and semantic checks for contract set v2.

Contract-level helpers only: these validate documents and express rules JSON Schema cannot
(cross-field, cross-document). They are not runtime code; slices implement the behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path
import contextlib
from typing import Any, Mapping, Sequence

import jsonschema
from jsonschema import FormatChecker
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[2]
V1_DIR = ROOT / "contracts"
V2_DIR = ROOT / "contracts" / "v2"
V2_SCHEMA_FILES = tuple(sorted(V2_DIR.glob("*.schema.json")))
BASE = "https://flightctl.local/contracts/"


class ContractError(ValueError):
    """A v2 schema or semantic contract violation."""


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _registry() -> Registry:
    resources = []
    for path in sorted(V1_DIR.glob("*.schema.json")):
        schema = load(path)
        resources.append((schema["$id"], Resource.from_contents(schema)))
    for path in V2_SCHEMA_FILES:
        schema = load(path)
        if schema["$id"] != f"{BASE}v2/{path.name}":
            raise ContractError(f"{path.name}: $id must be {BASE}v2/{path.name}")
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


_REGISTRY: Registry | None = None


def registry() -> Registry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _registry()
    return _REGISTRY


def schema_path(name: str) -> Path:
    path = V2_DIR / (name if name.endswith(".schema.json") else f"{name}.schema.json")
    if not path.exists():
        raise FileNotFoundError(path)
    return path


def validator(name: str, definition: str | None = None) -> Any:
    schema = load(schema_path(name))
    if definition is not None:
        schema = {"$schema": schema["$schema"], "$ref": f"{schema['$id']}#/$defs/{definition}"}
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema, registry=registry(), format_checker=FormatChecker())


def errors(instance: Any, name: str, definition: str | None = None) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in validator(name, definition).iter_errors(instance)]


def assert_valid(instance: Any, name: str, definition: str | None = None) -> None:
    found = errors(instance, name, definition)
    if found:
        raise ContractError(f"{name}{'#' + definition if definition else ''}: {found[:3]}")


def assert_invalid(instance: Any, name: str, definition: str | None = None) -> None:
    if not errors(instance, name, definition):
        raise ContractError(f"{name}: expected invalid instance was accepted: {json.dumps(instance)[:200]}")


def examples(name: str) -> dict[str, list[Any]]:
    return load(schema_path(name)).get("x-examples", {"valid": [], "invalid": []})


# ---------------------------------------------------------------- semantic rules

MUTATING_PORTS = frozenset({"workload_runner", "waker", "inhibitor", "notifier", "model_cache", "session_gateway", "release_backend"})
LANE_SCOPED_MUTATING = ("workload_runner", "inhibitor", "waker", "model_cache", "session_gateway", "notifier", "release_backend")
LANE_CRITICAL_PORTS = ("command_runner", "executor_transport", "workload_runner", "occupancy_probe", "inhibitor", "waker")
SHADOW_MUST_BE_DRYRUN = ("workload_runner", "inhibitor", "waker")
SHADOW_MUST_BE_REAL = ("command_runner", "executor_transport", "occupancy_probe")
FEATURE_PORTS = {
    "holder_leases": ("clock", "command_runner", "executor_transport", "occupancy_probe", "inventory_probe", "peer_identity", "inhibitor"),
    "wake": ("waker",),
    "jobs": ("workload_runner", "model_cache"),
    "endpoints": ("workload_runner", "health_probe", "model_cache"),
    "sessions": ("session_gateway",),
    "model_cache": ("model_cache",),
    "notifications": ("notifier",),
    "fido2_approvals": ("signer",),
    "release_backend": ("release_backend",),
    "shadow_observer": ("legacy_observer",),
    "friend_sessions": ("session_gateway",),
}


def required_ports(features: Mapping[str, bool]) -> set[str]:
    return {port for feature, on in features.items() if on for port in FEATURE_PORTS[feature]}


def adapters_semantics(config: Mapping[str, Any]) -> list[str]:
    """Fail-closed rules for the site adapters file (ADAPTERS.md section 3, round 2: B3)."""
    problems: list[str] = []
    profile = config["profile"]
    ports: Mapping[str, str] = config["ports"]
    allow_fake = set(config.get("allow_fake", []))
    required = required_ports(config["features"])
    for port, impl in sorted(ports.items()):
        if profile == "live" and impl == "fake" and port not in allow_fake:
            problems.append(f"live profile refuses fake port {port} (not in allow_fake)")
        if profile == "live" and port in required and impl != "real":
            problems.append(f"live profile: port {port} is required by an enabled feature and is {impl}")
        if profile == "sim" and impl in {"real", "dryrun", "record"}:
            problems.append(f"sim profile refuses {impl} port {port}")
        if profile == "shadow" and port in MUTATING_PORTS and impl in {"real", "record"}:
            problems.append(f"shadow profile: mutating port {port} is {impl} (shadow writes nothing)")
        if impl == "dryrun" and port not in MUTATING_PORTS:
            problems.append(f"port {port} is read-only and has no dryrun twin; use real or fake")
    for item in allow_fake:
        if item in LANE_CRITICAL_PORTS:
            problems.append(f"allow_fake may not list lane-critical port {item}")
        elif item in required:
            problems.append(f"allow_fake may not list {item}: an enabled feature requires it")
    for lane, entry in sorted(config.get("lanes", {}).items()):
        mode = entry["mode"]
        overrides: Mapping[str, str] = entry.get("ports", {})
        effective = {port: overrides.get(port, ports.get(port, "fake")) for port in set(ports) | set(LANE_CRITICAL_PORTS) | set(LANE_SCOPED_MUTATING)}
        wake_needed = entry.get("wake_needed", True)
        shadow_real = set(entry.get("shadow_real", []))
        if shadow_real and mode != "shadow":
            problems.append(f"lane {lane}: shadow_real is only valid on a shadow lane")
        for port in sorted(shadow_real - {"inhibitor"}):  # Amendment 4: the oracle matches the schema's items const
            problems.append(f"lane {lane}: shadow_real may name only the inhibitor, not {port}")
        for port, impl in sorted(overrides.items()):  # Amendment 4 rev 2: rule 6 on lane overrides too
            if impl == "dryrun" and port not in MUTATING_PORTS:
                problems.append(f"lane {lane}: port {port} is read-only and has no dryrun twin; use real or fake")
        if mode == "sim":  # Amendment 4 rev 2: a sim lane's effective lane-scoped ports are fake, in any site profile
            for port in sorted(set(LANE_CRITICAL_PORTS) | set(LANE_SCOPED_MUTATING)):  # explicit shared overrides: the schema
                if effective[port] != "fake":
                    problems.append(f"lane {lane} is sim but port {port} is {effective[port]} (a sim lane runs on fakes)")
        if mode == "live":
            if profile != "live":
                problems.append(f"lane {lane} is live but the site profile is {profile}")
            # round 3 (Sol 6 B3): EFFECTIVE ports per live lane, lane overrides included
            for port in sorted(set(LANE_CRITICAL_PORTS) | required):
                if port == "waker" and not wake_needed:
                    continue
                if effective[port] != "real":
                    problems.append(f"lane {lane} is live but {port} is {effective[port]}")
        if mode == "shadow":
            if profile == "sim":
                problems.append(f"lane {lane} is shadow but the site profile is sim")
            if shadow_real and not entry.get("legacy_lane"):
                problems.append(f"lane {lane}: shadow_real needs legacy_lane (the legacy fence must stand)")
            for port in SHADOW_MUST_BE_DRYRUN:
                if port == "waker" and not wake_needed:
                    continue
                want = "real" if port in shadow_real else "dryrun"
                if effective[port] != want:
                    problems.append(f"lane {lane} is shadow but mutating port {port} is {effective[port]} (must be {want})")
            for port in LANE_SCOPED_MUTATING:
                if port in shadow_real:
                    continue
                if effective[port] in {"real", "record"}:
                    problems.append(f"lane {lane} is shadow but mutating port {port} is {effective[port]} (shadow writes nothing)")
            for port in SHADOW_MUST_BE_REAL:
                if effective[port] != "real":
                    problems.append(f"lane {lane} is shadow but {port} is {effective[port]} (shadow needs real reads)")
        if mode == "off" and overrides:
            problems.append(f"lane {lane} is off but carries port overrides")
    problems += amendment1_adapters_semantics(config)
    return problems


def amendment1_adapters_semantics(config: Mapping[str, Any]) -> list[str]:
    """Amendment 1 (owner decisions of 2 Oct 2026): friend sessions are flag-gated (off in R1) and the A7 listener uses
    a private-CA certificate with a sane rotation window."""
    problems = []
    features = config.get("features", {})
    if features.get("sessions") and not features.get("friend_sessions"):
        problems.append("feature 'sessions' (friend SSH sessions) requires 'friend_sessions'")
    tls = config.get("tls")
    if not tls:
        problems.append("tls block missing (A7 serves a private-CA certificate)")
    else:
        rot = tls["rotation"]
        if not rot["alert_before_s"] < rot["renew_before_s"]:
            problems.append("tls.rotation.alert_before_s must be below renew_before_s (alert only after renewal was due)")
        if not rot["check_interval_s"] < rot["alert_before_s"]:
            problems.append("tls.rotation.check_interval_s must be below alert_before_s (an expiry must be seen before it bites)")
        if len({tls["cert_file"], tls["key_file"], tls["chain_file"]}) != 3:
            problems.append("tls cert_file, key_file and chain_file must be distinct files")
        anchor = tls.get("trust_anchor") or {}
        if set(anchor.get("root_ca_files", [])) & {tls["cert_file"], tls["key_file"], tls["chain_file"]}:
            problems.append("tls trust_anchor root_ca_files must not be the served cert, key or chain file (rev 2)")
        if not anchor.get("root_ca_files"):
            problems.append("tls trust_anchor has no root_ca_files (rev 2: chains are verified only against them)")
    return problems


def tls_cert_decision(*, server_name: str, cert_names: list[str], not_before: float, not_after: float, now: float,
                      alert_before_s: int, chain_ok: bool, key_matches: bool, chain_root_sha256: str | None,
                      anchor_sha256s: set[str], pinned_root_sha256s: set[str] | None = None) -> str:
    """Amendment 1 A7 oracle for ONE certificate file set (at start and on every change). 'refuse' / 'alert' (serve and
    alert) / 'serve'. Revision 2 (Sol 6 amd1): the TRUST ANCHOR is adapters.tls.trust_anchor: the chain must verify
    (chain_ok) and terminate at a root whose SHA-256 is one of the configured root_ca_files (anchor_sha256s); if
    pinned_root_sha256 is configured, the root must also be pinned. A chain rooted anywhere else (including a public
    or system CA) is refused. Exact SAN match only (no wildcard certificates)."""
    if not (chain_ok and key_matches) or chain_root_sha256 is None or chain_root_sha256 not in anchor_sha256s:
        return "refuse"
    if pinned_root_sha256s and chain_root_sha256 not in pinned_root_sha256s:
        return "refuse"
    if server_name not in cert_names or not (not_before <= now < not_after):
        return "refuse"
    return "alert" if not_after - now <= alert_before_s else "serve"


def tls_listener_action(*, candidate: str | None, incumbent: str | None) -> str:
    """Amendment 1 rev 3 (Sol 6 amd1 r2): what the listener serves, decided at start, on every file change, on every
    CONFIG change (server_name, trust_anchor root files, pins) and every check_interval_s. candidate = tls_cert_decision
    of the files on disk under the CURRENT config and time (None if unreadable). incumbent = tls_cert_decision RE-RUN on
    the certificate being served, also under the CURRENT config and time (None if nothing is served): expiry is not the
    only way an incumbent stops being acceptable; a removed root, a removed pin or a changed server_name revoke it too.
    'use-candidate' when the candidate is 'serve'/'alert'; else 'keep-incumbent' only when the re-validated incumbent is
    'serve'/'alert'; else 'stop-tls' (new handshakes refused, fail closed) with an operator alert."""
    if candidate in ("serve", "alert"):
        return "use-candidate"
    if incumbent in ("serve", "alert"):
        return "keep-incumbent"
    return "stop-tls"


FRIEND_CREATING_SUBCOMMANDS = frozenset({"session-open"})  # plus unit-start with --account (friend)
FRIEND_CLEANUP_SUBCOMMANDS = frozenset({"session-close", "account-check", "claim-release", "claim-reconcile"})


C9_BINDING_KEYS = ("host_id", "sshd_host_key_sha256", "machine_id_sha256", "sshd_effective_sha256", "proof_id")


def helper_binding_ok(cfg: Mapping[str, Any], live: Mapping[str, Any] | None) -> bool:
    """Amendment 1 rev 8/9 (Sol 6 amd1 r7, r8): before any friend CREATION the helper compares helper.json c9_binding with
    the LIVE facts it measures on its own host at that moment: host id, sshd_host_keys_sha256 over ALL host keys,
    SHA-256 of /etc/machine-id and sshd_effective_digest (global AND per-relevant-user `sshd -T`). Any difference (sshd config changed since the proof,
    host re-imaged, binding copied from another host) or missing data refuses creation (fail closed)."""
    binding = cfg.get("c9_binding")
    if not isinstance(binding, Mapping) or not live:
        return False
    return all(binding.get(k) is not None and binding.get(k) == live.get(k) for k in C9_BINDING_KEYS if k != "proof_id") and bool(binding.get("proof_id"))


def helper_subcommand_enabled(cfg: Mapping[str, Any], subcommand: str, *, friend: bool = False,
                              live: Mapping[str, Any] | None = None) -> bool:
    """Amendment 1 rev 2/8: the helper refuses only the paths that CREATE friend work (session-open, unit-start
    --account) unless it can see BOTH gates itself: helper.json friend_sessions_global (the site flag) AND
    friend_sessions_host (this host's inventory flag) AND friend_sessions (their conjunction) are all true, and the
    live host facts match c9_binding (helper_binding_ok). Either flag false = off. Cleanup and safety paths stay
    available for earlier friend work: session-close, unit-stop, output-collect, claim-release, claim-reconcile,
    account-check (read-only). Agent work is unaffected. claim-clear is never a helper subcommand."""
    known = {"unit-start", "unit-stop", "output-collect"} | FRIEND_CREATING_SUBCOMMANDS | FRIEND_CLEANUP_SUBCOMMANDS
    if subcommand == "claim-clear" or subcommand not in known:
        return False
    if subcommand in FRIEND_CREATING_SUBCOMMANDS or (subcommand == "unit-start" and friend):
        return (cfg.get("friend_sessions") is True and cfg.get("friend_sessions_global") is True
                and cfg.get("friend_sessions_host") is True and helper_binding_ok(cfg, live))
    return True


def sshd_effective(sshd_T_output: str) -> dict[str, list[str]]:
    """Parse `sshd -T` / `sshd -T -C user=..,host=..,addr=..` output (lower-case 'keyword value...' lines) into
    {keyword: [tokens]} for the key-related keywords the C9 runbook must preserve."""
    out: dict[str, list[str]] = {}
    for line in sshd_T_output.splitlines():
        parts = line.strip().split()
        # rev 10: keywords compared case-insensitively (measured: OpenSSH 10.5p1 prints "AuthorizedKeysFile", older
        # releases print "authorizedkeysfile")
        if parts and parts[0].lower() in ("authorizedkeysfile", "authorizedkeyscommand", "authorizedkeyscommanduser"):
            out[parts[0].lower()] = parts[1:]
    return out


MANAGED_KEYS = "/etc/ssh/flightctl-keys/%u"


def sshd_keys_plan(before: Mapping[str, list[str]]) -> list[str]:
    """C9 runbook step 3 (Amendment 1 rev 2): the new AuthorizedKeysFile value = every EFFECTIVE existing path, in
    order (whatever the host uses: the default '.ssh/authorized_keys .ssh/authorized_keys2', or e.g. /etc/ssh/keys/%u),
    then the managed directory. 'none' (no files) is preserved as no files: the managed dir alone is NOT added on such a
    host (stop: keys come only from AuthorizedKeysCommand there, which the runbook does not change)."""
    files = list(before.get("authorizedkeysfile", []))
    if not files or files == ["none"]:
        raise ValueError("host has no AuthorizedKeysFile (keys via AuthorizedKeysCommand only): stop, needs its own review")
    return files + ([MANAGED_KEYS] if MANAGED_KEYS not in files else [])


def sshd_change_verified(before: Mapping[str, list[str]], after: Mapping[str, list[str]]) -> list[str]:
    """C9 runbook step 4: compare `sshd -T` (global and per relevant user via -C) before and after the drop-in, BEFORE
    the reload. Every previous AuthorizedKeysFile path must still be there in the same order, the managed directory must
    be added, and AuthorizedKeysCommand / AuthorizedKeysCommandUser must be unchanged."""
    problems = []
    old, new = before.get("authorizedkeysfile", []), after.get("authorizedkeysfile", [])
    if new[: len(old)] != old:
        problems.append(f"previous AuthorizedKeysFile paths not preserved in order: {old} -> {new}")
    if MANAGED_KEYS not in new:
        problems.append("managed key directory missing from the effective AuthorizedKeysFile (drop-in not effective, e.g. set after an earlier value)")
    for kw in ("authorizedkeyscommand", "authorizedkeyscommanduser"):
        if before.get(kw) != after.get(kw):
            problems.append(f"{kw} changed: {before.get(kw)} -> {after.get(kw)}")
    return problems


C9_PROOF_TRUE = ("sshd_verified", "probe_login_ok", "probe_key_removed", "no_conditional_key_settings")  # rev 10
PROOF_CLOCK_SKEW_S = 300


def c9_proof_ok(host: Mapping[str, Any]) -> bool:
    """Amendment 1 rev 7/8: shape of the host's recorded C9 runbook proof: proof id, artefact SHA-256, date, runbook
    revision, the HOST BINDING (host_id, sshd host key SHA-256, machine-id SHA-256, effective `sshd -T` SHA-256) and the
    three outcomes, all true."""
    import re

    proof = host.get("c9_proof")
    hexes = ("artefact_sha256", "sshd_host_key_sha256", "machine_id_sha256", "sshd_effective_sha256")
    return (isinstance(proof, Mapping) and bool(re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,63}", str(proof.get("proof_id", ""))))
            and all(re.fullmatch(r"[0-9a-f]{64}", str(proof.get(k, ""))) for k in hexes) and bool(proof.get("recorded_at"))
            and bool(proof.get("host_id")) and all(proof.get(k) is True for k in C9_PROOF_TRUE))


def authority_admits_friend_work(adapters: Mapping[str, Any], host: Mapping[str, Any], artefact_store: Mapping[str, Mapping[str, str]],
                                 now: float) -> tuple[bool, str | None]:
    """Amendment 1 rev 7/8: the authority's admission for a friend session or friend-account job on a host, before
    any helper call. Gates, in order: global flag; openssh host; the host's own flag; a well-formed proof; the proof is
    BOUND to this host (proof host_id == inventory host_id; proof host key, machine-id and effective sshd -T hashes ==
    the values the inventory probe last OBSERVED on this host, so a copied proof, a re-imaged host or a changed sshd
    config is refused); the evidence artefact exists in the authority's artefact store under proof_id with the same
    SHA-256 and host; and the proof is not dated in the future (more than PROOF_CLOCK_SKEW_S ahead of now: a clock
    rollback or a forged date)."""
    import datetime

    if adapters.get("features", {}).get("friend_sessions") is not True:
        return False, "feature friend_sessions is off"
    if not c9_host_eligible(host):
        return False, "host ssh_server is not openssh"
    if host.get("friend_sessions_enabled") is not True:
        return False, "host friend_sessions_enabled is false"
    if not c9_proof_ok(host):
        return False, "host has no valid C9 runbook proof"
    proof = host["c9_proof"]
    if proof["host_id"] != host.get("host_id"):
        return False, "proof is bound to another host"
    for key in ("sshd_host_key_sha256", "machine_id_sha256", "sshd_effective_sha256"):
        if host.get(key) is None or proof[key] != host.get(key):
            return False, f"proof {key} does not match the host's observed value"
    stored = artefact_store.get(proof["proof_id"])
    if not stored or stored.get("sha256") != proof["artefact_sha256"] or stored.get("host_id") != host.get("host_id"):
        return False, "proof artefact missing or does not match the stored artefact"
    recorded = datetime.datetime.fromisoformat(str(proof["recorded_at"]).replace("Z", "+00:00")).timestamp()
    if recorded > now + PROOF_CLOCK_SKEW_S:
        return False, "proof is dated in the future (clock rollback?)"
    return True, None


def friend_session_host_allowed(adapters: Mapping[str, Any], host: Mapping[str, Any], artefact_store: Mapping[str, Mapping[str, str]],
                                now: float) -> bool:
    return authority_admits_friend_work(adapters, host, artefact_store, now)[0]


def helper_flag_expected(adapters: Mapping[str, Any], host: Mapping[str, Any], artefact_store: Mapping[str, Mapping[str, str]],
                         now: float) -> bool:
    return friend_session_host_allowed(adapters, host, artefact_store, now)


def friend_sessions_consistent(adapters: Mapping[str, Any], hosts: list[Mapping[str, Any]], helper_by_host: Mapping[str, Mapping[str, Any]],
                               artefact_store: Mapping[str, Mapping[str, str]], now: float) -> list[str]:
    """Rev 8 (replaces the old global-equality rule): the PER-HOST consistency check that must PASS before activation
    (C9 runbook step 8) and at every C-ASM/deploy check. For each host with a helper.json: friend_sessions_global ==
    the site flag; friend_sessions_host == the host's inventory flag; friend_sessions == the authority's admission
    result for that host; when on, c9_binding equals the proof's binding. A mixed deployment (only proven hosts on) is
    valid."""
    problems = []
    site_flag = adapters.get("features", {}).get("friend_sessions") is True
    for host in hosts:
        cfg = helper_by_host.get(host["host_id"])
        if cfg is None:
            continue
        hid = host["host_id"]
        if (cfg.get("friend_sessions_global") is True) != site_flag:
            problems.append(f"host {hid}: helper friend_sessions_global != site flag {site_flag}")
        if (cfg.get("friend_sessions_host") is True) != (host.get("friend_sessions_enabled") is True):
            problems.append(f"host {hid}: helper friend_sessions_host != inventory friend_sessions_enabled")
        want = helper_flag_expected(adapters, host, artefact_store, now)
        if (cfg.get("friend_sessions") is True) != want:
            problems.append(f"host {hid}: helper friend_sessions={cfg.get('friend_sessions')} but admission for this host is {want}")
        if want:
            proof = host["c9_proof"]
            binding = cfg.get("c9_binding") or {}
            if any(binding.get(k) != proof.get(k) for k in C9_BINDING_KEYS):
                problems.append(f"host {hid}: helper c9_binding does not equal the host's proof binding")
    return problems


SSHD_MAIN_CONFIG = "/etc/ssh/sshd_config"
# Rev 10 (Sol 6 amd1 r9): directives that are permitted inside a Match block (sshd_config(5), OpenSSH 10.5p1, read
# locally 2 Oct 2026) AND that select which keys or credentials authenticate a user, or enable a way in that bypasses the
# managed key path. Any of them in a CONDITIONAL context makes the host ineligible for C9 (refused, not enumerated).
SSHD_KEY_RELEVANT = frozenset({
    # key sources
    "authorizedkeysfile", "authorizedkeyscommand", "authorizedkeyscommanduser",
    # certificate / principal authorisation of a key for a user
    "trustedusercakeys", "authorizedprincipalsfile", "authorizedprincipalscommand", "authorizedprincipalscommanduser",
    # whether and which public keys are accepted
    "pubkeyauthentication", "revokedkeys",
    # other ways in that bypass the managed key path
    "authenticationmethods", "passwordauthentication", "kbdinteractiveauthentication", "permitemptypasswords",
    "pamservicename", "hostbasedauthentication", "hostbasedusesnamefrompacketonly", "ignorerhosts",
    "gssapiauthentication", "kerberosauthentication",
})
# Match-permitted but deliberately NOT listed (they only restrict, or do not choose a credential): AllowUsers/DenyUsers,
# AllowGroups/DenyGroups, RefuseConnection, MaxAuthTries, PubkeyAcceptedAlgorithms, PubkeyAuthOptions,
# CASignatureAlgorithms, HostbasedAcceptedAlgorithms, ForceCommand, ChrootDirectory, PermitRootLogin (root is never a
# friend), ExposeAuthInfo, and every forwarding/session/timeout keyword.


# Rev 11 (Sol 6 amd1 r10): keyword ALIASES, canonicalised before the Match check. Evidence (measured 2 Oct 2026 on
# OpenSSH 10.5p1, scratch alias_sweep.py: every keyword-shaped string in the sshd binary set to a non-default value under
# `sshd -T -f <scratch>`, recording which canonical keyword changed): challengeresponseauthentication and
# skeyauthentication -> kbdinteractiveauthentication (both accepted inside Match and effective there);
# dsaauthentication -> pubkeyauthentication (rejected inside Match by this release); hostdsakey -> hostkey. Recognised
# but not value-tested, mapped by name (inferred): pubkeyacceptedkeytypes, hostbasedacceptedkeytypes. Deprecated and
# ignored on 10.5p1 (measured) but functional on old releases, mapped conservatively: authorizedkeysfile2. The local
# sshd_config(5) documents only ChallengeResponseAuthentication as an alias. No OpenSSH source is installed here.
SSHD_KEYWORD_ALIASES = {
    "challengeresponseauthentication": "kbdinteractiveauthentication",
    "skeyauthentication": "kbdinteractiveauthentication",
    "dsaauthentication": "pubkeyauthentication",
    "authorizedkeysfile2": "authorizedkeysfile",
    "hostdsakey": "hostkey",
    "pubkeyacceptedkeytypes": "pubkeyacceptedalgorithms",
    "hostbasedacceptedkeytypes": "hostbasedacceptedalgorithms",
}
# Keywords sshd_config(5) (OpenSSH 10.5p1, local) permits after a Match line. In a conditional context any keyword that is
# neither one of these nor a known alias is REFUSED (fail closed against aliases of other OpenSSH releases).
SSHD_MATCH_KEYWORDS = frozenset(k.lower() for k in """AcceptEnv AllowAgentForwarding AllowGroups AllowStreamLocalForwarding
AllowTcpForwarding AllowUsers AuthenticationMethods AuthorizedKeysCommand AuthorizedKeysCommandUser AuthorizedKeysFile
AuthorizedPrincipalsCommand AuthorizedPrincipalsCommandUser AuthorizedPrincipalsFile Banner CASignatureAlgorithms
ChannelTimeout ChrootDirectory ClientAliveCountMax ClientAliveInterval DenyGroups DenyUsers DisableForwarding
ExposeAuthInfo ForceCommand GatewayPorts GSSAPIAuthentication HostbasedAcceptedAlgorithms HostbasedAuthentication
HostbasedUsesNameFromPacketOnly IgnoreRhosts Include IPQoS KbdInteractiveAuthentication KerberosAuthentication LogLevel
MaxAuthTries MaxSessions PAMServiceName PasswordAuthentication PermitEmptyPasswords PermitListen PermitOpen
PermitRootLogin PermitTTY PermitTunnel PermitUserRC PubkeyAcceptedAlgorithms PubkeyAuthentication PubkeyAuthOptions
RefuseConnection RekeyLimit RevokedKeys RDomain SetEnv StreamLocalBindMask StreamLocalBindUnlink TrustedUserCAKeys
UnusedConnectionTimeout X11DisplayOffset X11Forwarding X11UseLocalhost""".split())


def _sshd_lines(text: str):
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, rest = line.replace("=", " ", 1).partition(" ") if "=" in line.split()[0] else line.partition(" ")
        key = key.strip().lower()
        yield no, SSHD_KEYWORD_ALIASES.get(key, key), rest.strip()  # rev 11: canonicalise aliases first


def _sshd_include_targets(args: str, config_files: Mapping[str, str]) -> tuple[list[str], list[str]]:
    import fnmatch
    import posixpath

    found, missing = [], []
    for pattern in args.split():
        full = pattern if pattern.startswith("/") else posixpath.join("/etc/ssh", pattern)
        if any(ch in full for ch in "*?["):
            found += sorted(p for p in config_files if fnmatch.fnmatchcase(p, full))  # sshd: lexical order; no match is fine
        elif full in config_files:
            found.append(full)
        else:
            missing.append(full)
    return found, missing


def sshd_config_scan(config_files: Mapping[str, str], main_path: str = SSHD_MAIN_CONFIG) -> dict[str, Any]:
    """Rev 10 (Sol 6 amd1 r9): walk the sshd configuration from main_path, expanding Include (which sshd_config(5) also
    permits INSIDE a Match block). Context is conservative and sticky: from the first Match line onward (in expansion
    order, across Include boundaries, including `Match all`) every line is conditional; a file included from a
    conditional context is conditional throughout. Returns {'order': [paths in expansion order], 'problems': [...]}:
    a problem for every SSHD_KEY_RELEVANT directive in a conditional context, every unresolvable literal Include, a
    missing main file and an Include depth over 16."""
    order: list[str] = []
    problems: list[str] = []

    def walk(path: str, conditional: bool, depth: int) -> bool:
        if depth > 16:
            problems.append(f"Include depth over 16 at {path}")
            return True
        if path not in config_files:
            problems.append(f"configuration file {path} not available to verify")
            return True
        order.append(path)
        for no, key, rest in _sshd_lines(config_files[path]):
            if key == "match":
                conditional = True
            elif key == "include":
                targets, missing = _sshd_include_targets(rest, config_files)
                for m in missing:
                    problems.append(f"{path}:{no}: Include target {m} not available to verify")
                for target in targets:
                    conditional = walk(target, conditional, depth + 1) or conditional
            elif key in SSHD_KEY_RELEVANT and conditional:
                problems.append(f"{path}:{no}: {key} in a Match (conditional) context selects credentials per client context")
            elif conditional and key not in SSHD_MATCH_KEYWORDS:
                # rev 11: an unrecognised keyword (e.g. an alias of another OpenSSH release) cannot be judged: refuse
                problems.append(f"{path}:{no}: unrecognised keyword {key!r} in a Match (conditional) context")
        return conditional

    walk(main_path, False, 0)
    return {"order": order, "problems": problems}


def sshd_context_problems(config_files: Mapping[str, str], main_path: str = SSHD_MAIN_CONFIG) -> list[str]:
    """C9 eligibility (rev 10): [] only if no authentication-key-relevant directive is context-dependent."""
    return sshd_config_scan(config_files, main_path)["problems"]


def sshd_config_file_set(config_files: Mapping[str, str], main_path: str = SSHD_MAIN_CONFIG) -> list[list[str]]:
    import hashlib

    return [[path, hashlib.sha256(config_files[path].encode()).hexdigest()] for path in sshd_config_scan(config_files, main_path)["order"]]


def sshd_host_keys_sha256(host_pubkey_lines: list[str]) -> str:
    """Amendment 1 rev 9 (Sol 6 amd1 r8): WHICH host key is bound when sshd offers several: ALL of them. The digest
    is SHA-256 over the sorted 'type base64' pairs of every public key named by the effective `sshd -T` 'hostkey'
    lines (comments dropped). Rotating, adding or removing ANY host key changes it."""
    import hashlib

    pairs = sorted(" ".join(line.split()[:2]) for line in host_pubkey_lines if line.strip())
    return hashlib.sha256("\n".join(pairs).encode()).hexdigest()


def sshd_effective_digest(global_T: str, per_user_T: Mapping[str, str], config_files: Mapping[str, str],
                          main_path: str = SSHD_MAIN_CONFIG) -> str:
    """Amendment 1 rev 9 (Sol 6 amd1 r8): the bound sshd configuration is the global `sshd -T` output PLUS, for every
    relevant user (the probe account and every helper.json friend account), `sshd -T -C user=<u>,host=<h>,addr=<a>`
    (the same commands as runbook step 3), so a later `Match User`/`Match Group` change for a friend account changes
    the digest. Normalisation: each output's non-empty lines stripped and sorted; SHA-256 of the canonical JSON
    {"global": [...], "users": {user: [...]}}. Adding a friend account therefore also changes it (re-run the runbook)."""
    import hashlib
    import json

    def norm(text: str) -> list[str]:
        return sorted(line.strip() for line in text.splitlines() if line.strip())

    # rev 10 (Sol 6 amd1 r9): also the Include-expanded configuration FILE SET (path + SHA-256 of contents, in expansion
    # order), so ANY change to any sshd configuration file invalidates the binding, whatever client context it affects
    doc = {"global": norm(global_T), "users": {u: norm(per_user_T[u]) for u in sorted(per_user_T)},
           "files": sshd_config_file_set(config_files, main_path)}
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def helper_create_friend_work(cfg: Mapping[str, Any], subcommand: str, *, measure, create, undo, friend: bool = False) -> bool:
    """Amendment 1 rev 9 (Sol 6 amd1 r8): check and creation are ONE helper operation, run under the account's flock:
    measure the live host facts and check the gates and binding; create (write the key, set DeviceAllow, start the
    unit); RE-MEASURE immediately before returning (the commit point); if anything differs from the first measurement
    or no longer matches the binding, undo the creation and refuse. Residual window (stated, not closed): `sshd -T`
    reads the configuration FILES, so a running daemon reloaded with a different configuration whose files were then
    restored without a reload is not detected; and a change made after the commit affects only later creations."""
    live1 = measure()
    if not helper_subcommand_enabled(cfg, subcommand, friend=friend, live=live1):
        return False
    create()
    live2 = measure()
    if live2 != live1 or not helper_binding_ok(cfg, live2):
        undo()
        return False
    return True


def friend_sessions_disable(hosts: list[Mapping[str, Any]], helper_by_host: Mapping[str, Mapping[str, Any]],
                            reachable: set[str], already_off: set[str] | None = None) -> dict[str, Any]:
    """Amendment 1 rev 9 (Sol 6 amd1 r8): FAIL-CLOSED DISABLE ORDERING (chosen over an authority-signed enable token:
    no new key material or crypto dependency in the root helper, no cross-host clock dependence). Turning the site flag
    off is an authority operation, persisted in the authority DB:
      1. at once: the authority refuses all friend work everywhere (state 'disabling');
      2. for every host: write helper.json friend_sessions_global=false and friend_sessions=false, then READ BACK that
         the helper refuses session-open (helper_subcommand_enabled is False); a host that cannot be reached, or does
         not confirm, stays 'disable-pending', and the authority refuses ALL work to it (authority_admits_any_work), so
         the executor never calls its helper;
      3. only when no host is pending is the site flag reported 'off' (and C-ASM's consistency check can pass).
    Residual (stated): a pending host's helper keeps its stale copy until it is reached; it is exploitable only by a
    direct local helper call from the executor account on that host, which the authority no longer drives.
    Returns {'site_state': 'disabling'|'off', 'helpers': updated helper.json map, 'pending': sorted host ids}."""
    helpers = {h: dict(c) for h, c in helper_by_host.items()}
    confirmed = set(already_off or ())
    for host in hosts:
        hid = host["host_id"]
        cfg = helpers.get(hid)
        if cfg is None or hid not in reachable:
            continue
        cfg.update(friend_sessions_global=False, friend_sessions=False)
        if not helper_subcommand_enabled(cfg, "session-open", live={"host_id": hid}) and not helper_subcommand_enabled(cfg, "unit-start", friend=True, live={"host_id": hid}):
            confirmed.add(hid)
    pending = sorted(h["host_id"] for h in hosts if h["host_id"] in helpers and h["host_id"] not in confirmed)
    return {"site_state": "off" if not pending else "disabling", "helpers": helpers, "pending": pending}


def authority_admits_any_work(host_id: str, disable_state: Mapping[str, Any] | None) -> bool:
    """Rev 9: while a disable is in progress, a 'disable-pending' host gets NO work of any kind (agent or friend)."""
    return not (disable_state and host_id in disable_state.get("pending", ()))



def c9_host_eligible(host: Mapping[str, Any]) -> bool:
    """Amendment 1 (Q4): C9 friend sessions may be enabled only on hosts whose system sshd answers (it honours
    AuthorizedKeysFile). Tailscale SSH ignores authorized_keys, so its hosts need a separate, reviewed ACL path."""
    return host.get("ssh_server") == "openssh"


def lease_ceiling(policy: Mapping[str, Any], lane_max_lease_s: int | None, quota_max_lease_s: int | None, cls: str) -> int:
    """N3 oracle (policy.lease description): an explicit quota max_lease_s replaces the class default."""
    lease = policy["lease"]
    base = quota_max_lease_s if quota_max_lease_s is not None else lease["class_ceiling_s"][cls]
    caps = [base, lease["absolute_max_s"]]
    if lane_max_lease_s is not None:
        caps.append(lane_max_lease_s)
    return min(caps)


def quota_eval_semantics(result: Mapping[str, Any]) -> list[str]:
    problems = []
    if result["decision"] == "deny" and not result.get("limit"):
        problems.append("a quota denial must name the limit")
    if result["decision"] == "allow" and result.get("limit"):
        problems.append("an allow names no limit")
    return problems


def inventory_semantics(inventory: Mapping[str, Any]) -> list[str]:
    """Cross-reference rules for inventory v2 (v1 rules carried forward plus G07 card binding)."""
    problems: list[str] = []
    hosts = {h["host_id"]: h for h in inventory["hosts"]}
    if len(hosts) != len(inventory["hosts"]):
        problems.append("duplicate host_id")
    devices: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for host in inventory["hosts"]:
        for device in host["devices"]:
            if device["device_id"] in devices:
                problems.append(f"duplicate device_id {device['device_id']}")
            devices[device["device_id"]] = (host["host_id"], device)
            for key in ("uuid", "pci_bus_id", "numa_node"):
                if device[key] is None and key not in device["unknown_reasons"]:
                    problems.append(f"{device['device_id']}: {key} is null without an unknown reason")
    lane_ids = [lane["lane_id"] for lane in inventory["lanes"]]
    if len(lane_ids) != len(set(lane_ids)):
        problems.append("duplicate lane_id")
    claimed: dict[str, str] = {}
    for lane in inventory["lanes"]:
        if lane["host_id"] not in hosts:
            problems.append(f"lane {lane['lane_id']}: dangling host {lane['host_id']}")
            continue
        for device_id in lane["device_ids"]:
            owner = devices.get(device_id)
            if owner is None or owner[0] != lane["host_id"]:
                problems.append(f"lane {lane['lane_id']}: device {device_id} is not on host {lane['host_id']}")
                continue
            if device_id in claimed:
                problems.append(f"device {device_id} is in lanes {claimed[device_id]} and {lane['lane_id']}")
            claimed[device_id] = lane["lane_id"]
            if lane["enabled"] and owner[1]["uuid"] is None:
                problems.append(f"enabled lane {lane['lane_id']}: device {device_id} has no uuid (the occupancy probe cannot bind it)")
        if lane["enabled"] and inventory["stage"] == "confirmed":
            reach = hosts[lane["host_id"]]["reachability"]
            if reach not in ("confirmed", "asleep"):
                problems.append(f"enabled lane {lane['lane_id']} on host with reachability {reach}")
    for lane_id in inventory["endpoint_lane_order"]:
        if lane_id not in lane_ids:
            problems.append(f"endpoint_lane_order names unknown lane {lane_id}")
    if inventory["stage"] == "confirmed" and inventory["controller"]["auth_state"] != "configured":
        problems.append("a confirmed inventory needs a configured controller")
    return problems


def _num(text: str) -> int | float | None:
    text = text.strip()
    if text.startswith("[") or text in {"", "N/A"}:
        return None
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return None


DYNAMIC_USER_UIDS = range(61184, 65520)  # systemd DynamicUser range (systemd.exec(5)); never a noise identity


def noise_identity_ok(identity: Mapping[str, Any] | None, allowlist: Sequence[Mapping[str, Any]]) -> bool:
    """Amendment 2 (Sol 6.1 cold review P1-1): a process is desktop NOISE only by IDENTITY, never by size. Identity =
    the owner uid of /proc/<pid> (kernel-set, readable by any user), argv[0] from /proc/<pid>/cmdline (forgeable only by
    processes of that same uid, so an entry trusts that uid) and the nvidia-smi context type from `nvidia-smi -q -d PIDS`
    (measured 2 Oct on driver 610.57.04: C, G or C+G; a desktop browser holds a C+G context on a compute card). Noise
    requires an allow-list entry {argv0, uid} for this lane, a graphics-capable context (G or C+G: a pure compute 'C'
    context is never noise), and a uid that is not root and not in the DynamicUser range. Unknown identity = tenant."""
    if not identity or identity.get("context_type") not in ("G", "C+G"):
        return False
    uid = identity.get("uid")
    if not isinstance(uid, int) or uid == 0 or uid in DYNAMIC_USER_UIDS:
        return False
    return any(e.get("argv0") == identity.get("argv0") and e.get("uid") == uid for e in allowlist)


def occupancy_from_capture(gpus_csv: str, procs_csv: str, *, returncode: int, lane_id: str, host_id: str, observed_at: str,
                           lane_uuids: list[str], noise_allowlist: Sequence[Mapping[str, Any]] = (), noise_cap_mib: int = 0,
                           lane_noise_mib: int = 1024, identities: Mapping[int, Mapping[str, Any]] | None = None,
                           context_types: Mapping[tuple[str, int], str] | None = None,
                           attributed_pids: frozenset[int] = frozenset()) -> dict[str, Any]:
    """Contract ORACLE for the occupancy observation (gpu-probe.schema.json emptiness_rule).

    Parses the two real nvidia-smi query outputs (x-real-commands) exactly as the real twin must. It exists so
    that golden captures from real cards fix the rule; slice A3's production parser must agree with it.
    Amendment 2: noise is an explicit per-lane allow-list of desktop identities (noise_identity_ok) with an aggregate
    cap (noise_cap_mib); every other process is a tenant WHATEVER its size. Defaults are fail-closed (no allow-list,
    cap 0): nothing is noise.
    Amendment 2 rev 2 (Sol 6.1 amd2 P1): uid and argv0 are per PROCESS (identities[pid]); the context type is per
    (gpu_uuid, pid) (context_types), because one process can hold a C+G context on one card and a pure C context on
    another (nvidia-smi reports Type per device entry); a missing per-card type is null and never noise."""
    identities = identities or {}
    context_types = context_types or {}
    thresholds = {"lane_noise_mib": lane_noise_mib, "noise_cap_mib": noise_cap_mib, "noise_allowlist": [dict(e) for e in noise_allowlist]}
    unknown = {"kind": "gpu-occupancy", "host_id": host_id, "lane_id": lane_id, "observed_at": observed_at, "status": "unknown", "expected_uuids": list(lane_uuids),
               "gpus": [], "processes": [], "tenants": [], "noise": [], "lane_memory_used_mib": None, "thresholds": thresholds,
               "unexplained_mib": None, "empty": False}
    if returncode != 0:
        return unknown | {"reason": f"nvidia-smi exited {returncode}"}
    gpus = []
    for line in gpus_csv.strip().splitlines():
        cells = [c.strip() for c in line.split(",")]
        if len(cells) != 10:
            return unknown | {"reason": "unparsable gpu row"}
        uuid = cells[0]
        if uuid not in lane_uuids:
            continue
        used, total = _num(cells[1]), _num(cells[2])
        if not isinstance(used, int) or not isinstance(total, int):
            return unknown | {"reason": f"memory.used unknown on {uuid}"}
        slow = None if cells[7].startswith("[") else (cells[7] == "Active" or cells[8] == "Active")
        gpus.append({"uuid": uuid, "memory_used_mib": used, "memory_total_mib": total, "utilization_pct": _num(cells[3]),
                     "temperature_c": _num(cells[4]), "power_draw_w": _num(cells[5]), "power_limit_w": _num(cells[6]),
                     "thermal_slowdown": slow, "ecc_uncorrected": _num(cells[9])})
    if sorted(g["uuid"] for g in gpus) != sorted(lane_uuids):
        return unknown | {"reason": "a lane card is missing from nvidia-smi output"}
    processes, tenants, noise = [], [], []
    for line in procs_csv.strip().splitlines():
        head = line.split(", ", 2)
        if len(head) < 3:
            return unknown | {"reason": "unparsable process row"}
        uuid, pid_text = head[0].strip(), head[1].strip()
        name, _, mem_text = head[2].rpartition(", ")
        if not pid_text.isdigit() or not name:
            return unknown | {"reason": "unparsable process row"}
        if uuid not in lane_uuids:
            continue
        mem = _num(mem_text)
        mem = mem if isinstance(mem, int) else None
        pid = int(pid_text)
        ident = dict(identities.get(pid) or {}, context_type=context_types.get((uuid, pid)))  # rev 2: per card
        if pid in attributed_pids:
            kind = "lease"
        elif mem is not None and noise_identity_ok(ident, noise_allowlist):
            kind = "noise"
        else:
            kind = "external"  # Amendment 2: ANY process not lease and not an allow-listed identity is a tenant
        row = {"gpu_uuid": uuid, "pid": pid, "process_name": name[:4096], "used_memory_mib": mem, "attribution": kind,
               "uid": ident.get("uid"), "argv0": ident.get("argv0"), "context_type": ident.get("context_type")}
        processes.append(row)
        (tenants if kind == "external" else noise if kind == "noise" else []).append(row)
    lane_used = sum(g["memory_used_mib"] for g in gpus)
    for g in gpus:  # the two queries are separate samples: inconsistent numbers are unknown, never empty
        if sum(p["used_memory_mib"] or 0 for p in processes if p["gpu_uuid"] == g["uuid"]) > g["memory_used_mib"]:
            return unknown | {"reason": f"inconsistent sample on {g['uuid']}: process memory exceeds memory.used"}
    explained = sum((p["used_memory_mib"] or 0) for p in processes if p["attribution"] in {"lease", "noise"})
    unexplained = lane_used - explained
    return {"kind": "gpu-occupancy", "host_id": host_id, "lane_id": lane_id, "observed_at": observed_at, "status": "ok", "reason": None, "expected_uuids": list(lane_uuids),
            "gpus": gpus, "processes": processes, "tenants": tenants, "noise": noise, "lane_memory_used_mib": lane_used,
            "thresholds": thresholds, "unexplained_mib": unexplained,
            "empty": (not tenants and not any(p["attribution"] == "lease" for p in processes) and unexplained < lane_noise_mib
                      and sum(p["used_memory_mib"] for p in noise) <= noise_cap_mib)}


def occupancy_semantics(obs: Mapping[str, Any]) -> list[str]:
    """B2: 'empty' must agree with tenants AND aggregate memory; unknown memory is never noise."""
    problems = []
    if obs["status"] != "ok":
        return [] if obs["empty"] is False else ["unknown status cannot be empty"]
    # round 4 (Sol 6 r3 B2): the observed cards must be exactly the lane's expected cards
    expected_ids = list(obs.get("expected_uuids") or [])
    observed_ids = [g["uuid"] for g in obs["gpus"]]
    if not expected_ids:
        problems.append("expected_uuids is missing: emptiness cannot be judged without the lane's card identities")
    if sorted(observed_ids) != sorted(expected_ids):
        problems.append(f"observed cards {sorted(observed_ids)} are not exactly the lane's cards {sorted(expected_ids)}")
    for p in obs["processes"]:
        if p["gpu_uuid"] not in expected_ids:
            problems.append(f"pid {p['pid']} is on card {p['gpu_uuid']}, which is not a lane card")
    # round 7 self-probe: known process memory on a card cannot exceed that card's memory.used (inconsistent sample)
    for g in obs["gpus"]:
        listed = sum(p["used_memory_mib"] or 0 for p in obs["processes"] if p["gpu_uuid"] == g["uuid"])
        if listed > g["memory_used_mib"]:
            problems.append(f"card {g['uuid']}: processes report {listed} MiB but memory.used is {g['memory_used_mib']} MiB")
    lane_used = sum(g["memory_used_mib"] for g in obs["gpus"])
    if obs["lane_memory_used_mib"] != lane_used:
        problems.append("lane_memory_used_mib is not the sum of the lane's cards")
    explained = sum((p["used_memory_mib"] or 0) for p in obs["processes"] if p["attribution"] in {"lease", "noise"})
    if obs["unexplained_mib"] != lane_used - explained:
        problems.append("unexplained_mib does not equal lane memory minus attributed and noise memory")
    allow = obs["thresholds"]["noise_allowlist"]
    for p in obs["processes"]:
        mem, kind = p["used_memory_mib"], p["attribution"]
        if kind == "unattributed":
            problems.append(f"pid {p['pid']} is unattributed: every process must be lease, noise or external")
        if kind == "noise" and (mem is None or not noise_identity_ok(p, allow)):
            problems.append(f"pid {p['pid']} is classed as noise but is not an allow-listed desktop identity with known memory")
    noise_total = sum(p["used_memory_mib"] or 0 for p in obs["processes"] if p["attribution"] == "noise")
    # round 3 (Sol 6 B2 counterexample): tenants and noise are exactly the external / noise processes
    if [p for p in obs["processes"] if p["attribution"] == "external"] != list(obs["tenants"]):
        problems.append("tenants must equal the external processes (partition broken)")
    if [p for p in obs["processes"] if p["attribution"] == "noise"] != list(obs["noise"]):
        problems.append("noise must equal the noise processes (partition broken)")
    if obs["empty"] and any(p["used_memory_mib"] is None for p in obs["processes"]):
        problems.append("a process with unknown memory can never leave the lane empty")
    expected = (not obs["tenants"] and not any(p["attribution"] == "lease" for p in obs["processes"])
                and obs["unexplained_mib"] < obs["thresholds"]["lane_noise_mib"] and noise_total <= obs["thresholds"]["noise_cap_mib"])
    if obs["empty"] != expected:
        problems.append(f"empty={obs['empty']} contradicts tenants/unexplained memory (expected {expected})")
    return problems


def host_ceiling_bound(*, send_t: float, rtt_s: float, in_s: float, approved_max_end_t: float) -> dict[str, Any]:
    """N1 oracle (round 3). Times are on the AUTHORITY's monotonic clock. The host anchored max-end no later than
    send_t + rtt_s (it received the message before replying), so its ceiling is at most send_t + rtt_s + in_s.
    If that can exceed the approval, the authority must send a shorten-only 'ceiling' message."""
    bound = send_t + rtt_s + in_s
    return {"host_max_end_upper_bound": bound, "late_by_s": max(0.0, bound - approved_max_end_t), "must_shorten": bound > approved_max_end_t}


def ceiling_confirmed(*, reply_received_t: float, max_end_remaining_s: float | None, approved_max_end_t: float) -> bool:
    """N1 oracle (round 4). The host built its reply before the authority received it, so the host ceiling is at most
    reply_received_t + max_end_remaining_s (authority monotonic clock; no clock comparison). A grant is allowed only
    when that bound is at or before the approval. No reply (lost message) = not confirmed."""
    if max_end_remaining_s is None:
        return False
    return reply_received_t + max_end_remaining_s <= approved_max_end_t


FENCED_STATES = frozenset({"reserved", "staging", "starting", "running"})


def fence_evidence_ok(reply: Mapping[str, Any], expect: Mapping[str, Any]) -> bool:
    """Round 8 (Sol 6 r7): the reported state alone is not evidence. A reply counts only if it carries the executor's
    PERSISTED fence for this lease: exactly one fence on the expected lane (expect['lane_id'] on expect['host_id']),
    whose identity equals the echoed identity (lease, generation, token hash, run, unit), whose state is a fenced
    state equal to observed_state, and which was not invalidated by a reboot. Where the lane requires an inhibitor
    (expect['inhibitor_required'], default True: the authority sets awake.hold_inhibitor on every lane of a host whose
    inventory power mode is 'sleeps'), the fence must say inhibitor_held and the reply's inhibitor must be held under the
    lane/generation unit name flightctl-awake-<lane>-g<generation>.service."""
    ident = reply.get("echoed_identity") or {}
    lane_id = expect.get("lane_id")
    if lane_id is None or (ident.get("lane") or {}).get("lane_id") != lane_id:
        return False
    on_lane = [f for f in reply.get("fences") or [] if f["identity"]["lane"] == ident["lane"]]
    if len(on_lane) != 1 or on_lane[0]["identity"] != ident:
        return False
    fence = on_lane[0]
    if fence["state"] not in FENCED_STATES or fence["state"] != reply["observed_state"] or fence["rebooted_since_reserve"]:
        return False
    if expect.get("inhibitor_required", True):
        inhibitor = reply.get("inhibitor")
        unit = f"flightctl-awake-{lane_id}-g{ident['generation']}.service"
        if not (fence["inhibitor_held"] is True and inhibitor is not None and inhibitor["held"] is True and inhibitor["unit"] == unit):
            return False
    return True


def grant_after_reserve(replies: list[Mapping[str, Any]], approved_max_end_t: float, expect: Mapping[str, Any] | None = None) -> str:
    """N1 oracle (rounds 4-8). Each entry is {'received_t': float, 'reply': <executor reply in the WIRE shape of
    executor.schema.json>} or {'lost': True}; expect = {'lease_id', 'generation', 'host_id', 'lane_id', 'request_ids':
    set, 'inhibitor_required': bool (default True)}. A reply counts only if it validates against the executor reply
    schema, is ok=True, definite=True and dry_run=False, its controller_request_id is one this authority sent for this
    reserve, its nested echoed_identity and host_boot name the same lease_id, generation and host, AND (round 8) it
    carries the matching persisted fence evidence (fence_evidence_ok). Round 7: a 'reserve' reply with observed_state
    'reserved' must come first; only after it may a 'ceiling' reply in a fenced state confirm a shortened ceiling.
    Any other kind, a dry-run, or a free/unknown state never confirms."""
    if expect is None:
        return "pending ceiling-unconfirmed"
    reserved = False
    for entry in replies:
        if entry.get("lost"):
            continue
        reply = entry.get("reply")
        if not isinstance(reply, Mapping) or errors(reply, "executor", "reply"):
            continue
        ident = reply.get("echoed_identity") or {}
        if not (reply["ok"] is True and reply["definite"] is True and reply["dry_run"] is False
                and reply["controller_request_id"] in expect["request_ids"]
                and ident.get("lease_id") == expect["lease_id"]
                and ident.get("generation") == expect["generation"]
                and (ident.get("lane") or {}).get("host_id") == expect["host_id"]
                and reply.get("host_boot", {}).get("host_id") == expect["host_id"]
                and fence_evidence_ok(reply, expect)):
            continue
        if reply["kind"] == "reserve" and reply["observed_state"] == "reserved":
            reserved = True
        elif not (reply["kind"] == "ceiling" and reserved and reply["observed_state"] in FENCED_STATES):
            continue
        if ceiling_confirmed(reply_received_t=entry["received_t"], max_end_remaining_s=reply["max_end_remaining_s"], approved_max_end_t=approved_max_end_t):
            return "grant"
    return "pending ceiling-unconfirmed"


def executor_reply(*, lease_id: str, generation: int, host_id: str, controller_request_id: str, remaining: float | None,
                   ok: bool = True, definite: bool = True, kind: str = "reserve", observed_state: str | None = None,
                   dry_run: bool = False, fenced: bool = True, inhibitor_held: bool = True) -> dict[str, Any]:
    """Build an executor reply in the exact WIRE shape of executor.schema.json (nested echoed_identity, host_boot).
    By default an ok reply in a fence state carries its persisted fence and a held inhibitor (round 8); fenced=False
    builds Sol 6 r7's reply (observed_state reserved, fences [], inhibitor null)."""
    import copy

    reserve = copy.deepcopy(examples("executor")["valid"][0])
    ident = reserve["identity"]
    ident.update(lease_id=lease_id, generation=generation)
    ident["lane"]["host_id"] = host_id
    ident["unit"] = f"flightctl-{ident['lane']['lane_id']}-g{generation}.service"
    state = observed_state or ("reserved" if ok else "free")
    fence_states = {"reserved", "staging", "starting", "running", "stopping", "quarantined", "closed"}
    with_fence = fenced and ok and state in fence_states
    fences = [{"identity": copy.deepcopy(ident), "state": state, "local_deadlines": [], "inhibitor_held": inhibitor_held,
               "rebooted_since_reserve": False}] if with_fence else []
    inhibitor = ({"held": inhibitor_held, "unit": f"flightctl-awake-{ident['lane']['lane_id']}-g{generation}.service", "what": "idle"}
                 if with_fence else None)
    reply = {"schema_version": 2, "kind": kind, "controller_request_id": controller_request_id, "echoed_identity": ident,
             "ok": ok, "definite": definite, "observed_state": state,
             "host_boot": {"host_id": host_id, "boot_id": "e8a72066-3f84-4fbb-bfd5-9e055bee90c0", "observed_at": "2026-10-02T10:00:01Z"},
             "host_utc": "2026-10-02T10:00:01Z", "fences": fences, "unit": None, "occupancy": None, "inhibitor": inhibitor, "stage": None,
             "log_lines": [], "next_cursor": None, "error": None if ok else {"code": "unknown", "message": "reserve not applied", "layer": "executor", "cause": None},
             "dry_run": dry_run, "max_end_remaining_s": remaining, "output": None}
    return reply


def parse_utc(value: str):
    """Parse a contract utc_time ('...Z', optional fraction) to an aware datetime. Never compare timestamps as text."""
    from datetime import datetime, timezone

    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"not a UTC timestamp: {value!r}")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    return parsed.astimezone(timezone.utc)


def auth_decision(peer_ids: list[Mapping[str, str]], accounts: list[Mapping[str, Any]], token: Mapping[str, Any] | None,
                  now: str) -> tuple[str, str | None]:
    """B6 oracle (round 5). The request carries no principal: it is derived from the peer's external ids, or selected
    by a token accepted from an allowed peer. Expiry compares PARSED aware datetimes (now >= expires_at denies);
    revoked tokens deny; a rejected token never falls back."""
    by_id = {a["principal_id"]: a for a in accounts}
    if token is not None:
        owner = by_id.get(token["principal_id"])
        try:
            expired = parse_utc(now) >= parse_utc(token["expires_at"])
        except (ValueError, TypeError, KeyError):
            return "deny", None  # unparsable or missing expiry fails closed (round 7 self-probe)
        if owner is None or not owner["enabled"] or token.get("revoked_at") or expired:
            return "deny", None
        allowed = owner["allowed_peers"] + owner["external_ids"]
        if not any(p in allowed for p in peer_ids):
            return "deny", None
        return "allow", owner["principal_id"]
    matches = [a for a in accounts if any(p in a["external_ids"] for p in peer_ids)]
    if len(matches) != 1 or not matches[0]["enabled"]:
        return "deny", None
    return "allow", matches[0]["principal_id"]




def work_admission(active_parents: Mapping[tuple[str, str], str], principal_id: str, host_id: str, kind: str,
                   lease_id: str, principal_kind: str) -> tuple[str, str | None]:
    """B5 oracle (round 4). kind: 'parent' (a lease) or 'child' (session/job naming lease_id).
    active_parents maps (principal_id, host_id) -> parent lease_id."""
    if principal_kind != "human":
        return "allow", None
    current = active_parents.get((principal_id, host_id))
    if kind == "parent":
        return ("deny", "account_busy") if current is not None else ("allow", None)
    return ("allow", None) if current == lease_id else ("deny", "account_busy")


def admit_parent(db_path: str, principal_id: str, host_id: str, lease_id: str) -> bool:
    """B5 reference for the authority side: one active parent per (principal, host), enforced by a UNIQUE index inside
    the admission transaction (BEGIN IMMEDIATE). Returns True if this lease became the parent."""
    import sqlite3

    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS parent(principal_id TEXT, host_id TEXT, lease_id TEXT, active INTEGER)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_parent ON parent(principal_id, host_id) WHERE active = 1")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("INSERT INTO parent VALUES (?, ?, ?, 1)", (principal_id, host_id, lease_id))
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            return False
        conn.execute("COMMIT")
        return True
    finally:
        conn.close()


FRIEND_ACCOUNT_RE = r"^fc-[a-z0-9][a-z0-9_-]{0,28}$"
# round 8 (Sol 6 r7): claim-clear is a SEPARATE root-only program (mode 0700 root:root) reachable only through the
# operator's own sudoers rule, which must re-authenticate (no NOPASSWD). The executor's helper has no claim-clear.
CLEAR_PATH = "/usr/local/libexec/flightctl-claim-clear"


def _claim_paths(state_dir: str, account: str) -> tuple[str, str, str]:
    import os
    import re

    if not re.fullmatch(FRIEND_ACCOUNT_RE, account) or account == "fc-svc":
        raise ValueError(f"account {account!r} is not an allowlisted friend account")
    return (os.path.join(state_dir, f"{account}.parent"), os.path.join(state_dir, f"{account}.quarantined"),
            os.path.join(state_dir, f".{account}.lock"))


@contextlib.contextmanager
def _account_lock(lock_path: str, enabled: bool = True):
    """Round 8: one exclusive flock per account serialises admission+start, release/clear and the reconcile marker
    write, so a quarantine can never land between a child's admission and its start. `enabled=False` exists ONLY so a
    race-injection test hook (which runs while the lock is held) can model a path that bypasses it."""
    import fcntl
    import os

    if not enabled:
        yield
        return
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _quarantine(marker: str, status: str) -> None:
    try:
        with open(marker, "x", encoding="utf-8") as handle:
            handle.write(status)
    except FileExistsError:
        pass


def _unlink_if_ours(state_dir: str, account: str, path: str, our_ino: int) -> bool:
    """Tombstone technique (rounds 6 and 8): rename the claim path to a unique tombstone and delete it only if the
    tombstone is the inode we expect; otherwise put it back and refuse. If the put-back finds a NEW claim at the path,
    nothing is deleted: the tombstone stays for the operator and the account is quarantined (fail closed)."""
    import os
    import uuid

    _, marker, _ = _claim_paths(state_dir, account)
    tomb = os.path.join(state_dir, f".{account}.tomb.{uuid.uuid4().hex}")
    try:
        os.rename(path, tomb)
    except FileNotFoundError:
        return False
    if os.lstat(tomb).st_ino != our_ino:
        try:
            os.link(tomb, path)
            os.unlink(tomb)
        except FileExistsError:
            _quarantine(marker, "conflict")
        return False
    os.unlink(tomb)
    return True


def helper_claim(state_dir: str, account: str, lease_id: str, boot_id: str = "boot-a", *, start=None,
                 _after_link=None, _before_rollback=None, _lock: bool = True) -> bool:
    """B5 reference for the helper's admission (rounds 5-8). session-open and friend unit-start carry --parent-lease.
    ONE RULE (round 8): while the account is quarantined NO claim of any kind is admitted (no new parent, no child of
    the old parent); otherwise the request is admitted only if it claims the parent (no claim yet) or names the claimed
    parent (a child). The claim is a complete temp file published with link(); `start` (the actual session-open or
    systemd-run) runs while the account lock is still held, so a quarantine cannot land between admission and start.
    Defence in depth (round 8, Sol 6 r7): if a marker appears after the link, the rollback deletes the claim path only
    if it still names our own inode (tombstone check); if the path was cleared and replaced meanwhile, we refuse
    without touching the new claim."""
    import json
    import os
    import tempfile

    path, marker, lock = _claim_paths(state_dir, account)
    with _account_lock(lock, _lock):
        if os.path.lexists(marker):
            return False
        fd, tmp = tempfile.mkstemp(prefix=f".{account}.", dir=state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"lease_id": lease_id, "boot_id": boot_id}, handle)
            our_ino = os.lstat(tmp).st_ino
            try:
                os.link(tmp, path)
                linked = True
            except FileExistsError:
                linked = False
            if linked:
                if _after_link is not None:
                    _after_link()
                if os.path.lexists(marker):  # quarantined after our link: roll back only our own inode
                    if _before_rollback is not None:
                        _before_rollback()
                    _unlink_if_ours(state_dir, account, path, our_ino)
                    return False
                try:
                    if os.lstat(path).st_ino != our_ino:
                        return False  # cleared and replaced since our link: not ours any more
                except FileNotFoundError:
                    return False
            else:
                try:
                    fdc = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                except OSError:
                    return False
                with os.fdopen(fdc, encoding="utf-8") as handle:
                    if json.load(handle)["lease_id"] != lease_id or os.path.lexists(marker):
                        return False
            if start is not None:
                start()
            return True
        finally:
            os.unlink(tmp)


CLOSE_PROOF_KEYS = ("user_slice_empty", "occupancy_empty", "key_removed")


def _remove_claim(state_dir: str, account: str, lease_id: str, race_hook) -> bool:
    import json
    import os

    path, _, _ = _claim_paths(state_dir, account)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return True  # already gone (e.g. a retried clear after a crash)
    except OSError:
        return False  # e.g. a symlink planted at the claim path: refuse, never follow
    try:
        with os.fdopen(os.dup(fd), encoding="utf-8") as handle:
            if json.load(handle)["lease_id"] != lease_id:
                return False
        if race_hook is not None:
            race_hook()
        tomb_ok = _unlink_if_ours(state_dir, account, path, os.fstat(fd).st_ino)
        if not tomb_ok and os.path.lexists(path):
            return False
        return True
    finally:
        os.close(fd)


def helper_release(state_dir: str, account: str, lease_id: str, close_proof: Mapping[str, bool], _race_hook=None,
                   *, _lock: bool = True) -> bool:
    """B5 claim-release (rounds 6-8), run through the executor's helper: requires the close proof and the same lease,
    and is race-safe (inode-checked tombstone). It never lifts a quarantine: a quarantined account needs claim-clear."""
    import os

    path, marker, lock = _claim_paths(state_dir, account)
    if not all(close_proof.get(k) is True for k in CLOSE_PROOF_KEYS):
        return False
    with _account_lock(lock, _lock):
        if os.path.lexists(marker):
            return False
        return _remove_claim(state_dir, account, lease_id, _race_hook)


def clear_authorized(invocation: Mapping[str, Any], operator_account: str, operator_uid: int, program: str = CLEAR_PATH) -> list[str]:
    """Round 8 (Sol 6 r7): how claim-clear verifies operator approval. It is a separate program at CLEAR_PATH, mode
    0700 root:root, so only root can execute it; the executor's helper never execs it and has no claim-clear
    subcommand. It runs only when sudo started it for the operator: real and effective UID 0, and SUDO_USER/SUDO_UID
    (set by sudo itself, after env_reset) equal to helper.json operator_account and that account's UID. The operator's
    sudoers rule must re-authenticate (no NOPASSWD); `operator_sudo_audit` checks it on the installed host. Input =
    {'program': argv0 realpath, 'ruid', 'euid', 'sudo_user', 'sudo_uid'}."""
    problems = []
    if invocation.get("program") != program:
        problems.append(f"{program} must run as its own root-only program, never through the executor's helper")
    if invocation.get("ruid") != 0 or invocation.get("euid") != 0:
        problems.append("claim-clear must run as root (real and effective UID 0)")
    if invocation.get("sudo_user") != operator_account or invocation.get("sudo_uid") != operator_uid:
        problems.append("claim-clear must be started by sudo for the configured operator account")
    return problems


def helper_clear(state_dir: str, account: str, lease_id: str, close_proof: Mapping[str, bool],
                 invocation: Mapping[str, Any], operator_account: str, operator_uid: int, *, _lock: bool = True) -> bool:
    """B5 claim-clear (round 8): the only way a quarantine is lifted. Operator authorization (clear_authorized), the
    close proof and the claimed lease are all required; the claim is removed first (tombstone-checked) and the marker
    last, so a crash in between leaves the account blocked."""
    import os

    path, marker, lock = _claim_paths(state_dir, account)
    if clear_authorized(invocation, operator_account, operator_uid):
        return False
    if not all(close_proof.get(k) is True for k in CLOSE_PROOF_KEYS):
        return False
    with _account_lock(lock, _lock):
        if not os.path.lexists(marker):
            return False  # nothing to clear: an unquarantined claim is released with claim-release
        if not _remove_claim(state_dir, account, lease_id, None):
            return False
        os.unlink(marker)
        return True


def helper_reconcile(state_dir: str, active_parents: Mapping[str, str], current_boot_id: str, *, _lock: bool = True) -> dict[str, str]:
    """B5 reconcile (rounds 6-8): NEVER removes a claim. A claim the authority lists is 'kept'; one it omits is
    'orphaned-quarantined' and one naming another parent is 'conflict'. Both write the account's quarantine marker
    under the account lock (admission effect: helper_claim refuses everything) and raise an operator alert; only
    claim-clear lifts it."""
    import json
    import os

    outcome = {}
    for name in sorted(os.listdir(state_dir)):
        if not name.endswith(".parent") or name.startswith("."):
            continue
        account = name[: -len(".parent")]
        path, marker, lock = _claim_paths(state_dir, account)
        with _account_lock(lock, _lock):
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            except OSError:
                continue
            with os.fdopen(fd, encoding="utf-8") as handle:
                claim = json.load(handle)
            want = active_parents.get(account)
            if want == claim["lease_id"] and not os.path.lexists(marker):
                outcome[account] = "kept"
                continue
            status = "kept-quarantined" if want == claim["lease_id"] else ("orphaned-quarantined" if want is None else "conflict")
            _quarantine(marker, status)
            outcome[account] = status
    return outcome


class SplitStore:
    """Round 5 reference for D-token-4 crash consistency (main authority DB + never-backed-up replay store).

    ORDER for a grant: (1) commit the replay row (request_id, token, deadline) in the replay store; (2) commit the
    lease + token-free idempotency record (with request_fingerprint and replay_deadline) in the main DB. The main DB
    is the source of truth.
    RECOVERY at startup: delete replay rows whose request_id has no committed idempotency record (a crash between 1
    and 2 left an orphan that was never granted).
    SCOPE (round 9, Sol 6 r8): idempotency and replay rows are keyed by (authenticated principal_id, request_id), as
    rpc-envelope states; another principal reusing the same request_id (even with the same payload) is a different
    request and gets a fresh decision, never this principal's lease or token.
    REPLAY: round 8 (Sol 6 r7) checks the request fingerprint FIRST: the same request_id with a different fingerprint
    is 409 conflict at any time and never sees the stored response. A matching retry returns the grant with its token
    if the replay row exists and is within its deadline; at/after the deadline it gets the null-token replay; inside
    the window with the row missing (restored main DB, lost replay store) it is 409 replay_unavailable: no token is
    invented and NO second grant is made; the orphaned lease is closed through the holder-lost path."""

    def __init__(self, main_path: str, replay_path: str):
        import sqlite3

        self.main = sqlite3.connect(main_path, isolation_level=None)
        self.replay = sqlite3.connect(replay_path, isolation_level=None)
        for conn in (self.main, self.replay):
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA secure_delete=ON")
        self.main.execute("CREATE TABLE IF NOT EXISTS idem(principal_id TEXT NOT NULL, request_id TEXT NOT NULL, request_fingerprint TEXT NOT NULL, lease_id TEXT, lane TEXT, replay_deadline REAL, PRIMARY KEY (principal_id, request_id))")
        self.main.execute("CREATE TABLE IF NOT EXISTS lease(lease_id TEXT PRIMARY KEY, lane TEXT, token_sha256 TEXT, state TEXT)")
        self.main.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active ON lease(lane) WHERE state IN ('active', 'holder-lost')")
        self.replay.execute("CREATE TABLE IF NOT EXISTS replay(principal_id TEXT NOT NULL, request_id TEXT NOT NULL, token TEXT, deadline REAL, PRIMARY KEY (principal_id, request_id))")

    def grant(self, request_id: str, lane: str, lease_id: str, token: str, deadline: float, *, principal: str, fingerprint: str, now: float,
              crash_between: bool = False) -> dict:
        import hashlib

        known = self.replay_or_record(request_id, principal=principal, fingerprint=fingerprint, now=now)  # round 7: the real retry time
        if known is not None:
            return known
        self.replay.execute("INSERT INTO replay VALUES (?, ?, ?, ?)", (principal, request_id, token, deadline))  # step 1
        if crash_between:
            raise RuntimeError("crash between the two commits")
        self.main.execute("BEGIN IMMEDIATE")
        try:
            self.main.execute("INSERT INTO lease VALUES (?, ?, ?, 'active')", (lease_id, lane, hashlib.sha256(token.encode()).hexdigest()))
            self.main.execute("INSERT INTO idem VALUES (?, ?, ?, ?, ?, ?)", (principal, request_id, fingerprint, lease_id, lane, deadline))
        except Exception:
            self.main.execute("ROLLBACK")
            self.replay.execute("DELETE FROM replay WHERE principal_id = ? AND request_id = ?", (principal, request_id))
            return {"status": 409, "code": "busy"}
        self.main.execute("COMMIT")
        return {"status": 200, "lease_id": lease_id, "token": token}

    def replay_or_record(self, request_id: str, *, principal: str, fingerprint: str, now: float) -> dict | None:
        """Round 6 (Sol 6 r5): the token-free replay deadline lives in the MAIN record, so a missing replay row is
        interpreted by the main DB alone. Round 8 (Sol 6 r7): a fingerprint mismatch is 409 conflict before anything
        else, with no lease id and no token."""
        rec = self.main.execute("SELECT request_fingerprint, lease_id, replay_deadline FROM idem WHERE principal_id = ? AND request_id = ?", (principal, request_id)).fetchone()
        if rec is None:
            return None
        stored_fp, lease_id, deadline = rec
        if stored_fp != fingerprint:
            return {"status": 409, "code": "conflict"}
        if now >= deadline:
            return {"status": 200, "lease_id": lease_id, "token": None}
        row = self.replay.execute("SELECT token FROM replay WHERE principal_id = ? AND request_id = ?", (principal, request_id)).fetchone()
        if row is not None:
            return {"status": 200, "lease_id": lease_id, "token": row[0]}
        self.main.execute("UPDATE lease SET state = 'holder-lost' WHERE lease_id = ? AND state = 'active'", (lease_id,))
        return {"status": 409, "code": "replay_unavailable", "lease_id": lease_id}

    def scrub(self, now: float) -> int:
        """Routine end-of-window scrub of raw tokens (secure_delete + WAL truncate)."""
        cur = self.replay.execute("DELETE FROM replay WHERE deadline <= ?", (now,))
        self.replay.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return cur.rowcount

    def recover(self) -> int:
        ids = {tuple(r) for r in self.main.execute("SELECT principal_id, request_id FROM idem")}
        orphans = [tuple(r) for r in self.replay.execute("SELECT principal_id, request_id FROM replay") if tuple(r) not in ids]
        for pid, rid in orphans:
            self.replay.execute("DELETE FROM replay WHERE principal_id = ? AND request_id = ?", (pid, rid))
        return len(orphans)

    def active_leases(self, lane: str) -> int:
        return self.main.execute("SELECT count(*) FROM lease WHERE lane = ? AND state IN ('active', 'holder-lost')", (lane,)).fetchone()[0]

    def close_after_proof(self, lease_id: str) -> None:
        """The holder-lost lease frees the lane only through the normal stop + emptiness proof."""
        self.main.execute("UPDATE lease SET state = 'closed' WHERE lease_id = ?", (lease_id,))

    def close(self) -> None:
        self.main.close()
        self.replay.close()




def contained_open(root_fd: int, relpath: str, expect_sha256: str | None = None) -> bytes:
    """Reference for the output containment rule (executor 'output' kind): reject absolute paths, '.', '..' and empty
    components, then walk component by component from a directory fd with O_NOFOLLOW, refuse symlinks and non-regular
    files, verify the expected hash on the open fd. Raises PermissionError (escape) or ValueError (changed)."""
    import hashlib
    import os
    import stat

    if not relpath or relpath.startswith("/") or "\x00" in relpath:
        raise PermissionError("relpath must be relative")
    parts = relpath.split("/")
    if any(p in {"", ".", ".."} for p in parts):
        raise PermissionError("relpath must be relative and contained")
    fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = nxt
        leaf = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=fd)
    except OSError as exc:
        raise PermissionError(f"refused: {exc.strerror}") from exc
    finally:
        os.close(fd)
    try:
        if not stat.S_ISREG(os.fstat(leaf).st_mode):
            raise PermissionError("not a regular file")
        with os.fdopen(leaf, "rb", closefd=False) as handle:
            data = handle.read()
    finally:
        os.close(leaf)
    if expect_sha256 is not None and hashlib.sha256(data).hexdigest() != expect_sha256:
        raise ValueError("output changed between list and get")
    return data


def output_owner_expected(dir_owner_uid: int, recorded_uid: int, overflow_uid: int) -> int | None:
    """Amendment 2 (Sol 6.1 cold review P1-2): namespace-aware ownership for DynamicUser outputs. systemd documents
    (systemd.exec(5), v261) that with DynamicUser and an id-mapped StateDirectory the HOST sees the files owned by the
    overflow uid ('nobody', /proc/sys/kernel/overflowuid, measured 65534 on the controller 2 Oct) while the service sees
    its own uid; without id-mapping the host sees the recorded dynamic uid. The expected owner is therefore the
    host-visible owner of the job's own StateDirectory root, accepted only if it is the recorded uid (no id-mapping) or the
    overflow uid (id-mapped); any other owner means the directory is not the job's and NOTHING is collected (None)."""
    if dir_owner_uid == recorded_uid:
        return recorded_uid
    if dir_owner_uid == overflow_uid:
        return overflow_uid
    return None


def collect_job_outputs(staging_fd: int, store_fd: int, recorded_uid: int, overflow_uid: int) -> dict[str, Any]:
    """Amendment 2: output-collect = stat the job's StateDirectory root (the staging fd), derive the expected owner with
    output_owner_expected, then stage_outputs against THAT owner (regular files only, st_nlink == 1, owner == expected,
    O_NOFOLLOW throughout). A foreign-owned staging root collects nothing."""
    import os

    expected = output_owner_expected(os.fstat(staging_fd).st_uid, recorded_uid, overflow_uid)
    if expected is None:
        return {"accepted": {}, "rejected": {".": "staging directory is not owned by the job (neither recorded nor overflow uid)"}, "owner": None}
    return dict(stage_outputs(staging_fd, store_fd, expected), owner=expected)


def stage_outputs(staging_fd: int, store_fd: int, job_uid: int) -> dict[str, Any]:
    """Round 4 reference for trusted output staging (run after the job unit stopped and its cgroup is empty).
    Amendment 2: job_uid is the EXPECTED host-visible owner from output_owner_expected (collect_job_outputs), not
    necessarily the recorded runtime uid. Accepts only regular files with st_nlink == 1 and st_uid == job_uid (checked with fstat on the O_NOFOLLOW fd) and
    COPIES their bytes into the executor-owned store; everything else is rejected with a reason."""
    import hashlib
    import os
    import stat

    accepted: dict[str, str] = {}
    rejected: dict[str, str] = {}

    def walk(src_fd: int, dst_fd: int, prefix: str) -> None:
        for name in sorted(os.listdir(src_fd)):
            rel = f"{prefix}{name}"
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=src_fd)
            except OSError as exc:
                rejected[rel] = f"open refused: {exc.strerror}"
                continue
            try:
                st = os.fstat(fd)
                if stat.S_ISDIR(st.st_mode) and st.st_uid == job_uid:
                    os.mkdir(name, 0o750, dir_fd=dst_fd)
                    sub = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dst_fd)
                    try:
                        walk(fd, sub, rel + "/")
                    finally:
                        os.close(sub)
                    continue
                if not stat.S_ISREG(st.st_mode):
                    rejected[rel] = "not a regular file"
                elif st.st_nlink != 1:
                    rejected[rel] = f"hard link (st_nlink={st.st_nlink})"
                elif st.st_uid != job_uid:
                    rejected[rel] = "not owned by the job account"
                else:
                    with os.fdopen(fd, "rb", closefd=False) as src:
                        data = src.read()
                    out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640, dir_fd=dst_fd)
                    with os.fdopen(out, "wb") as dst:
                        dst.write(data)
                    accepted[rel] = hashlib.sha256(data).hexdigest()
            finally:
                os.close(fd)

    walk(staging_fd, store_fd, "")
    return {"accepted": accepted, "rejected": rejected}




HELPER_PATH = "/usr/local/libexec/flightctl-helper"
HELPER_CONFIG = "/etc/flightctl/helper.json"
# secure_path may name only these (the C7h install audit also stats each: root-owned, not group/other-writable)
SECURE_PATH_ALLOWED = frozenset({"/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"})


def sudo_l_grants(text: str, user: str) -> list[dict[str, Any]]:
    """Parse the EFFECTIVE grants from `sudo -l -U <user>` output (format observed on sudo 1.9.17p2, 2 Oct 2026):
    header 'User <u> may run the following commands on <h>:', grants indented 4 spaces starting with '(',
    continuation lines indented 8 spaces joined with one space, optional 'TAG: ' prefixes, comma-separated commands.
    Group (%grp) and alias grants appear here already expanded, which is why the audit uses this output."""
    import re

    lines = text.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if re.match(rf"^User {re.escape(user)} may run the following commands on \S+:$", l))
    except StopIteration:
        return []
    entries: list[str] = []
    for line in lines[start + 1:]:
        if not line.strip():
            break
        if line.startswith("        ") and entries:
            entries[-1] += " " + line.strip()
        elif line.startswith("    ("):
            entries.append(line.strip())
        else:
            break
    grants = []
    for entry in entries:
        m = re.match(r"^\(([^)]*)\)\s*(.*)$", entry)
        if not m:
            grants.append({"runas": None, "tags": [], "commands": [entry], "raw": entry})
            continue
        rest = m.group(2)
        tags = []
        while True:
            t = re.match(r"^([A-Z_]+):\s*(.*)$", rest)
            if not t:
                break
            tags.append(t.group(1))
            rest = t.group(2)
        grants.append({"runas": m.group(1), "tags": tags, "commands": [c.strip() for c in rest.split(", ") if c.strip()], "raw": entry})
    return grants


def sudoers_audit(sudo_l_text: str, caller: str) -> list[str]:
    """C7h EFFECTIVE-privilege audit (round 4): run on `sudo -l -U <caller>` output from the installed host.
    The caller must have exactly one effective grant: (root) NOPASSWD: <helper>, no other command, no SETENV,
    and its matching Defaults must not weaken the environment."""
    problems = []
    grants = sudo_l_grants(sudo_l_text, caller)
    if len(grants) != 1:
        problems.append(f"{caller} has {len(grants)} effective sudo grants; exactly one (the helper) is allowed")
    for g in grants:
        if g["runas"] != "root":
            problems.append(f"grant runs as ({g['runas']}), not (root): {g['raw']}")
        if "NOPASSWD" not in g["tags"] or "SETENV" in g["tags"]:
            problems.append(f"grant tags {g['tags']} must be NOPASSWD without SETENV: {g['raw']}")
        if g["commands"] != [HELPER_PATH]:
            problems.append(f"grant allows {g['commands']}, not exactly [{HELPER_PATH}]")
    defaults = sudo_l_text.split("may run the following commands")[0]
    import re

    if "!env_reset" in defaults:
        problems.append("Defaults weaken the environment (!env_reset)")
    # round 5 (Sol 6 r4): the Defaults C7h names must be PRESENT, not merely not negated
    entries = {e.strip().split("=", 1)[0] for e in re.split(r",\s*|\n\s*", defaults) if e.strip()}
    for required in ("env_reset", "!setenv", "secure_path"):
        if required not in entries:
            problems.append(f"required Defaults entry {required} is missing for {caller}")
    # round 6 (Sol 6 r5): secure_path must be a non-empty list of standard root-owned system directories
    m = re.search(r"secure_path=([^,\n]*)", defaults)
    if m is not None:
        raw = m.group(1).strip()
        if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
            raw = raw[1:-1]
        parts = re.split(r"\\?:", raw)  # sudo -l escapes ':' as '\:'; both spellings separate entries
        dirs = [d for d in parts if d]
        if not dirs:
            problems.append("secure_path is empty")
        elif len(dirs) != len(parts):
            # round 7 (Sol 6 r6): an empty component (leading, trailing or doubled separator) means the current directory
            problems.append("secure_path has an empty component (current directory in PATH)")
        for d in dirs:
            if d not in SECURE_PATH_ALLOWED:
                problems.append(f"secure_path entry {d!r} is not a standard root-owned system directory")
    if re.search(r"(?<!!)\bsetenv\b", defaults):
        problems.append("Defaults weaken the environment (setenv)")
    if re.search(r"env_keep\s*\+?=\s*\"?[^\"\n]*\b(LD_|PYTHON)", defaults):
        problems.append("Defaults weaken the environment (env_keep of loader/interpreter variables)")
    return problems


def sudoers_file_audit(text: str, caller: str, caller_groups: list[str]) -> list[str]:
    """Static check of an installed sudoers.d file (complements, never replaces, the effective audit): any rule whose
    user list can include the caller (its name, a %group it belongs to, ALL, or a User_Alias containing either) may
    grant only the helper."""
    import re

    aliases: dict[str, set[str]] = {}
    for line in text.splitlines():
        m = re.match(r"^\s*User_Alias\s+(\w+)\s*=\s*(.+)$", line)
        if m:
            aliases[m.group(1)] = {x.strip() for x in m.group(2).split(",")}
    me = {caller, "ALL"} | {f"%{g}" for g in caller_groups}
    problems = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Defaults") and (s.startswith("Defaults ") or s.startswith("Defaults\t") or s.startswith(f"Defaults:{caller}")):
            if "!env_reset" in s or re.search(r"(?<!!)\bsetenv\b", s):
                problems.append(f"Defaults weaken the environment for {caller}: {s}")
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("Defaults") or s.startswith("User_Alias"):
            continue
        users, _, rest = s.partition(" ")
        members = set()
        for u in users.split(","):
            members |= aliases.get(u, {u})
        if not members & me:
            continue
        cmds = rest.split(":", 1)[-1] if ":" in rest else rest.split(")", 1)[-1]
        commands = [c.strip() for c in cmds.split(",")]
        if commands != [HELPER_PATH] or "SETENV" in rest:
            problems.append(f"rule can apply to {caller} and grants more than the helper: {s}")
    return problems




SUDO_BUILTINS = frozenset({"sudoedit", "list"})


def sudo_path_matches(spec: str, program: str) -> bool:
    """Does a sudoers command PATH spec reach `program`? Rules from sudoers(5) (1.9.17p2, read 2 Oct 2026): 'ALL';
    a '^...$' POSIX ERE (1.9.10+); a directory ending in '/' reaches files directly inside it (not sub-directories);
    otherwise shell-style wildcards (*, ?, [...]) that never match '/'. An unparsable regex is treated as reaching
    (fail closed)."""
    import fnmatch
    import posixpath
    import re

    if spec == "ALL":
        return True
    if spec.startswith("^") and spec.endswith("$"):
        try:
            return re.fullmatch(spec[4:] if spec.startswith("^(?i)") else spec, program, re.I if spec.startswith("^(?i)") else 0) is not None
        except re.error:
            return True
    if spec.endswith("/"):
        spec, program = spec[:-1], posixpath.dirname(program)
    want, have = spec.split("/"), program.split("/")
    return len(want) == len(have) and all(fnmatch.fnmatchcase(h, w) for h, w in zip(have, want))


def _command_reaches(command: str, program: str, cmnd_aliases: Mapping[str, list[str]] | None = None, _depth: int = 0) -> bool:
    """One Cmnd from a grant: 'path [args]' (arguments never limit reach here: the clear program is dangerous with any
    argument, so an argument-bearing grant counts), '!path' (a negation grants nothing, and is NOT allowed to cancel a
    route: fail closed), a built-in, or an alias NAME. A known Cmnd_Alias is expanded; an unknown alias name is treated
    as reaching (fail closed)."""
    import re

    c = command.strip()
    if not c or c.startswith("!"):
        return False
    path = c.split()[0]
    if path in SUDO_BUILTINS:
        return False
    if path == "ALL" or path.startswith("/") or path.startswith("^"):
        return sudo_path_matches(path, program)
    if re.fullmatch(r"[A-Z][A-Z0-9_]*", path):
        if cmnd_aliases is not None and path in cmnd_aliases and _depth < 16:
            return any(_command_reaches(x, program, cmnd_aliases, _depth + 1) for x in cmnd_aliases[path])
        return True
    return True  # anything unrecognised: fail closed


def _grant_reaches(grant: Mapping[str, Any], program: str, cmnd_aliases: Mapping[str, list[str]] | None = None) -> bool:
    if grant["runas"] is None:
        return True  # unparsable grant line: fail closed
    return any(_command_reaches(c, program, cmnd_aliases) for c in grant["commands"])


def parse_sudoers_rules(text: str) -> tuple[dict[str, set[str]], dict[str, list[str]], list[dict[str, Any]]]:
    """Static sudoers parse for the C7h install audit: User_Alias and Cmnd_Alias definitions (including 'A = x : B = y'
    on one line) and user specs 'users hosts = (runas) TAGS: cmnd, ...'. Continuation lines ending in '\\' are joined."""
    import re

    joined = re.sub(r"\\\n\s*", " ", text)
    users: dict[str, set[str]] = {}
    cmnds: dict[str, list[str]] = {}
    rules = []
    for line in joined.splitlines():
        s = line.split("#", 1)[0].strip() if not line.strip().startswith("#") else ""
        if not s or s.startswith("Defaults"):
            continue
        m = re.match(r"^(User_Alias|Cmnd_Alias|Cmd_Alias|Host_Alias|Runas_Alias)\s+(.*)$", s)
        if m:
            for part in m.group(2).split(" : "):
                name, _, value = part.partition("=")
                items = [x.strip() for x in value.split(",") if x.strip()]
                if m.group(1) == "User_Alias":
                    users[name.strip()] = set(items)
                elif m.group(1) in ("Cmnd_Alias", "Cmd_Alias"):
                    cmnds[name.strip()] = items
            continue
        who, _, rest = s.partition(" ")
        if "=" not in rest:
            continue
        spec = rest.split("=", 1)[1].strip()
        runas = None
        rm = re.match(r"^\(([^)]*)\)\s*(.*)$", spec)
        if rm:
            runas, spec = rm.group(1), rm.group(2)
        tags = []
        while True:
            t = re.match(r"^([A-Z_]+):\s*(.*)$", spec)
            if not t:
                break
            tags.append(t.group(1))
            spec = t.group(2)
        rules.append({"users": [u.strip() for u in who.split(",")], "runas": runas if runas is not None else "root", "tags": tags,
                      "commands": [c.strip() for c in spec.split(",") if c.strip()], "raw": s})
    return users, cmnds, rules


def _static_routes(sudoers_text: str, account: str, groups: list[str] | tuple[str, ...], program: str) -> list[dict[str, Any]]:
    user_aliases, cmnd_aliases, rules = parse_sudoers_rules(sudoers_text)
    me = {account, "ALL"} | {f"%{g}" for g in groups}

    def members(u: str, depth: int = 0) -> set[str]:
        if u in user_aliases and depth < 16:
            out: set[str] = set()
            for x in user_aliases[u]:
                out |= members(x, depth + 1)
            return out
        return {u}

    found = []
    for rule in rules:
        names: set[str] = set()
        for u in rule["users"]:
            names |= members(u)
        if names & me and _grant_reaches(rule, program, cmnd_aliases):
            found.append(rule)
    return found


def _defaults_lines(sudo_l_text: str) -> tuple[list[str], list[str]]:
    """Split `sudo -l -U <u>` Defaults output into the 'Matching Defaults entries' parameters and the
    'Runas and Command-specific defaults' lines (format measured on sudo 1.9.17p2: each line '    Defaults!<cmnds> <params>'
    or '    Defaults><runas> <params>')."""
    import re

    lines = sudo_l_text.splitlines()
    matching: list[str] = []
    specific: list[str] = []
    mode = None
    for line in lines:
        if line.startswith("Matching Defaults entries for "):
            mode = "m"
            continue
        if line.startswith("Runas and Command-specific defaults for "):
            mode = "s"
            continue
        if not line.strip() or not line.startswith("    "):
            mode = None if not line.startswith("    ") else mode
            continue
        if mode == "m":
            matching += [e.strip() for e in re.split(r",\s*", line.strip()) if e.strip()]
        elif mode == "s":
            specific.append(line.strip())
    return matching, specific


def _param_value(params: list[str], name: str):
    value = None
    for p in params:
        if p == f"!{name}":
            value = False
        elif p == name:
            value = True
        elif p.startswith(f"{name}="):
            value = p.split("=", 1)[1].strip().strip('"')
    return value


def clear_fresh_auth_audit(sudo_l_text: str, cmnd_aliases: Mapping[str, list[str]] | None = None, program: str = CLEAR_PATH) -> list[str]:
    """Round 9 (Sol 6 r8): sudo caches credentials (timestamp_timeout, default 5 minutes), so a password rule alone does
    not re-authenticate. The EFFECTIVE timestamp_timeout for CLEAR_PATH must be 0 ('always prompt', sudoers(5)) and
    authentication must not be disabled. Precedence follows sudoers(5): matching (global/host/user) Defaults first,
    then runas-specific (Defaults>root/ALL), then command-specific Defaults!<cmnd> 'applied later, once the command's
    path is known'; later entries override earlier ones. A Defaults! line naming an unresolvable alias that touches
    these options fails the audit (fail closed)."""
    import re

    matching, specific = _defaults_lines(sudo_l_text)
    problems = []
    timeout = _param_value(matching, "timestamp_timeout")
    auth = _param_value(matching, "authenticate")
    staged = {">": [], "!": []}
    for line in specific:
        m = re.match(r"^Defaults([!>])(\S+)\s+(.*)$", line)
        if not m:
            problems.append(f"unparsable command-specific Defaults line: {line}")
            continue
        kind, targets, params = m.group(1), m.group(2), [p.strip() for p in re.split(r",\s*", m.group(3)) if p.strip()]
        staged[kind].append((targets.split(","), params, line))
    for kind in (">", "!"):
        for targets, params, line in staged[kind]:
            touches = any(p.lstrip("!").split("=", 1)[0] in ("timestamp_timeout", "authenticate") for p in params)
            if kind == ">":
                applies = any(t in ("root", "ALL") for t in targets)
            else:
                unresolved = [t for t in targets if re.fullmatch(r"[A-Z][A-Z0-9_]*", t) and t != "ALL" and not (cmnd_aliases and t in cmnd_aliases)]
                if unresolved and touches:
                    problems.append(f"cannot resolve {unresolved} in {line}; state the claim-clear path literally")
                applies = any(_command_reaches(t, program, cmnd_aliases) for t in targets if t not in unresolved)
            if applies:
                t = _param_value(params, "timestamp_timeout")
                timeout = t if t is not None else timeout
                a = _param_value(params, "authenticate")
                auth = a if a is not None else auth
    if auth is False:
        problems.append(f"authentication is disabled (!authenticate) for {program}")
    try:
        ok = timeout is not None and float(timeout) == 0.0
    except (TypeError, ValueError):
        ok = False
    if not ok:
        problems.append(f"effective timestamp_timeout for {program} is {timeout if timeout is not None else 'the default (5)'}; it must be 0 so every invocation prompts")
    return problems


def operator_sudo_audit(sudo_l_text: str, operator: str, sudoers_text: str | None = None, groups: list[str] | tuple[str, ...] = (),
                        program: str = CLEAR_PATH) -> list[str]:
    """Rounds 8-9 (Sol 6 r7, r8): `sudo -l -U <operator>` on the installed host (plus, when given, the installed sudoers
    text for Cmnd_Alias expansion). The operator must reach CLEAR_PATH as root; EVERY grant that reaches it (exact,
    ALL, wildcard, directory, regex, argument-bearing or alias) must re-authenticate: no NOPASSWD, no SETENV; and the
    effective timestamp_timeout for the program must be 0 (clear_fresh_auth_audit)."""
    cmnd_aliases = parse_sudoers_rules(sudoers_text)[1] if sudoers_text else None
    grants = [g for g in sudo_l_grants(sudo_l_text, operator) if _grant_reaches(g, program, cmnd_aliases)]
    if sudoers_text:
        grants += _static_routes(sudoers_text, operator, groups, program)
    problems = []
    if not grants:
        problems.append(f"{operator} has no sudo route to {program}")
    for g in grants:
        if g["runas"] not in ("root", "ALL", "ALL : ALL", "root : root"):
            problems.append(f"grant does not run as root: {g['raw']}")
        if "NOPASSWD" in g["tags"] or "SETENV" in g["tags"]:
            problems.append(f"route to claim-clear must re-authenticate (no NOPASSWD/SETENV): {g['raw']}")
    problems += clear_fresh_auth_audit(sudo_l_text, cmnd_aliases, program)
    return problems


def no_clear_route_audit(sudo_l_text: str, account: str, sudoers_text: str | None = None, groups: list[str] | tuple[str, ...] = (),
                         program: str = CLEAR_PATH) -> list[str]:
    """Rounds 8-9: the executor and every friend account must have no sudo route to CLEAR_PATH, counting wildcard,
    directory, regex, argument-bearing, ALL and alias grants (unknown aliases fail closed)."""
    cmnd_aliases = parse_sudoers_rules(sudoers_text)[1] if sudoers_text else None
    found = [g["raw"] for g in sudo_l_grants(sudo_l_text, account) if _grant_reaches(g, program, cmnd_aliases)]
    if sudoers_text:
        found += [r["raw"] for r in _static_routes(sudoers_text, account, groups, program)]
    return [f"{account} can reach {program}: {raw}" for raw in found]


def helper_config_semantics(cfg: Mapping[str, Any]) -> list[str]:
    """Round 8: cross-field rules for helper.json that JSON Schema cannot state."""
    problems = []
    op = cfg.get("operator_account")
    if op == cfg.get("caller_account"):
        problems.append("operator_account must not be the executor (caller_account)")
    if op in cfg.get("friend_accounts", []):
        problems.append("operator_account must not be a friend account")
    probe = cfg.get("probe_account")
    if probe is not None and (probe in cfg.get("friend_accounts", []) or probe in (op, cfg.get("caller_account"))):
        problems.append("probe_account must be a dedicated test account: not a friend, not the operator, not the executor (Amendment 1 rev 4)")
    return problems


# Round 8 (Sol 6 r7): a mention of the withdrawn shared account is allowed ONLY inside one of these withdrawal phrases,
# matched over the occurrence itself (whitespace-normalised), never by a same-line keyword elsewhere on the line.
WITHDRAWAL_PHRASES = (
    r"the shared `?fc-svc`? account is (?:withdrawn|gone)",
    r"\bno (?:shared )?service account is (?:involved|needed any more)",
)


def schema_prose(schema: Any) -> str:
    """The prose of a schema file (every 'description' and every x-* text except x-examples), which is what a reader
    takes as instructions; structural keywords such as a `not: {pattern: ^fc-svc$}` rejection are not prose."""
    out: list[str] = []

    def walk(node: Any, key: str | None = None) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "x-examples":
                    continue
                if isinstance(v, str) and (k == "description" or k.startswith("x-") or (key or "").startswith("x-")):
                    out.append(v)
                else:
                    walk(v, k)
        elif isinstance(node, list):
            for v in node:
                walk(v, key)

    walk(schema)
    return "\n".join(out)


def service_account_offenders(text: str) -> list[str]:
    """Return every 'service account' / fc-svc / service_account occurrence that is not itself inside a withdrawal
    phrase. Unlike a same-line keyword exemption, an affirmative instruction stays an offender even if the word
    'withdrawn' appears elsewhere on the line or in the sentence."""
    import re

    flat = re.sub(r"\s+", " ", text)
    covered: list[tuple[int, int]] = []
    for phrase in WITHDRAWAL_PHRASES:
        covered += [m.span() for m in re.finditer(phrase, flat, re.I)]
    offenders = []
    for m in re.finditer(r"service account|fc-svc|service_account", flat, re.I):
        if not any(a <= m.start() and m.end() <= b for a, b in covered):
            offenders.append(flat[max(0, m.start() - 60): m.end() + 40])
    return offenders


def helper_install_audit(stat_chain: list[Mapping[str, Any]]) -> list[str]:
    """C7h install audit: every path from / to the helper and to its config (and the keys dir) must be root-owned and
    not group/other-writable, so the caller cannot replace the helper or its trusted config. Input = os.stat of each
    component as {path, uid, mode}; the gauge collects it on the real host."""
    problems = []
    for entry in stat_chain:
        if entry["uid"] != 0:
            problems.append(f"{entry['path']} is not owned by root")
        if entry["mode"] & 0o022:
            problems.append(f"{entry['path']} is group- or other-writable")
    return problems


def executor_semantics(message: Mapping[str, Any]) -> list[str]:
    """Cross-field executor-v2 rules JSON Schema cannot express."""
    problems = []
    ident = message.get("identity") or message.get("echoed_identity") or {}
    unit = ident.get("unit")
    lane = (ident.get("lane") or {}).get("lane_id")
    gen = ident.get("generation")
    if unit is not None and lane is not None and gen is not None and unit != f"flightctl-{lane}-g{gen}.service":
        problems.append("unit name must be flightctl-<lane_id>-g<generation>.service (assigned before reserve)")
    if "deadlines" in message:
        kinds = [d["kind"] for d in message["deadlines"]]
        if len(kinds) != len(set(kinds)):
            problems.append("duplicate deadline kind")
    return problems


# ---------------------------------------------------------------- Amendment 1 rev 4: C9 runbook session probe
PROBE_PATH = "/usr/local/libexec/flightctl-session-probe"
PROBE_MAX_TTL_S = 900
PROBE_OK = "/usr/bin/printf flightctl-probe-ok"


def _utc_compact(t: float) -> str:
    import datetime

    # rev 5 (Sol 6 amd1 r4): sshd(8) reads expiry-time in the SYSTEM time zone unless it ends in 'Z'; always write UTC + 'Z'
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y%m%d%H%M%SZ")


def sshd_expiry_effective(value: str, server_tz: str) -> float:
    """How sshd(8) interprets an authorized_keys expiry-time value (local man page, read 2 Oct 2026): format
    YYYYMMDDHHMM[SS][Z]; 'Dates and times will be interpreted in the system time zone unless suffixed by a Z character,
    in which case they will be interpreted in the UTC time zone'. Returns the effective expiry as a POSIX timestamp."""
    import datetime
    import zoneinfo

    utc = value.endswith("Z")
    digits = value[:-1] if utc else value
    fmt = "%Y%m%d%H%M%S" if len(digits) == 14 else "%Y%m%d%H%M"
    naive = datetime.datetime.strptime(digits, fmt)
    tz = datetime.timezone.utc if utc else zoneinfo.ZoneInfo(server_tz)
    return naive.replace(tzinfo=tz).timestamp()


def session_probe(keys_dir: str, cfg: Mapping[str, Any], invocation: Mapping[str, Any], operator_uid: int, *, account: str,
                  pubkey: str, ttl_s: int, now: float, audit: list[dict[str, Any]], mono_now: float, boot_id: str) -> bool:
    """Amendment 1 rev 4 (Sol 6 amd1 r3): the bounded key-writing path for the C9 runbook's login proof while the flag is
    OFF. A separate root-only program (PROBE_PATH, mode 0700 root:root), reached only through the operator's own
    re-authenticating sudo rule (same pattern and audits as claim-clear). Writes ONE key for helper.json probe_account
    only (a dedicated test account: no friend, not the operator, not the executor), with ttl_s <= 900, as
    'restrict,command="<probe-ok>",expiry-time="<UTC>" <key>' so sshd itself refuses it after expiry even if no sweep
    runs. Refused while friend_sessions is on (session-open is the path then), for any other account, for a non-operator
    caller, and while an unexpired probe key exists. Every call is an audit event."""
    import json
    import os
    import re

    def event(kind: str, **extra: Any) -> None:
        audit.append(dict({"event": kind, "account": account, "at": now}, **extra))

    reasons = clear_authorized(invocation, cfg["operator_account"], operator_uid, program=PROBE_PATH)
    probe_account = cfg.get("probe_account")
    if probe_account is None or account != probe_account:
        reasons.append("only the configured probe_account may be probed")
    if cfg.get("friend_sessions") is True:
        reasons.append("friend_sessions is on: use session-open")
    if not (0 < ttl_s <= PROBE_MAX_TTL_S):
        reasons.append(f"ttl_s must be in (0, {PROBE_MAX_TTL_S}]")
    if not re.fullmatch(r"(ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa) [A-Za-z0-9+/=]{16,8192}", pubkey or ""):
        reasons.append("one public key, no options")
    path = os.path.join(keys_dir, account) if probe_account and account == probe_account else None
    if path and os.path.lexists(path + ".probe.json"):
        with open(path + ".probe.json", encoding="utf-8") as handle:
            if json.load(handle)["expires_at"] > now:
                reasons.append("an unexpired probe key already exists")
    if reasons:
        event("probe-refused", reasons=reasons, caller=invocation.get("sudo_user"))
        return False
    expires = now + ttl_s
    line = f'restrict,command="{PROBE_OK}",expiry-time="{_utc_compact(expires)}" {pubkey}\n'
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(line)
    os.replace(path + ".tmp", path)
    with open(path + ".probe.json", "w", encoding="utf-8") as handle:
        # rev 8 (clock rollback): also the monotonic deadline and boot id; the sweep honours whichever expires first
        json.dump({"expires_at": expires, "expires_mono": mono_now + ttl_s, "boot_id": boot_id}, handle)  # rev 9: no wall fallback
    event("probe-granted", expires_at=expires, caller=invocation.get("sudo_user"))
    return True


PROBE_SWEEP_MODES = ("expired", "all")


def probe_sweep(keys_dir: str, cfg: Mapping[str, Any], invocation: Mapping[str, Any], *, now: float, audit: list[dict[str, Any]],
                mode: str, mono_now: float, boot_id: str, account: str | None = None) -> bool:
    """Amendment 1 rev 5 (Sol 6 amd1 r4): `flightctl-session-probe --sweep --expired|--all [--account <a>]`, the ONLY way a
    probe key is removed. ROOT-ONLY and REMOVE-ONLY: it needs real and effective UID 0 and NO sudo context check, so the
    root systemd units can run it (the per-minute `flightctl-session-probe-sweep.timer` with --expired, and the runbook's
    rollback unit and the operator's step-7 revoke with --all). It can never add or extend a key. It acts only on
    helper.json probe_account's key file: an --account naming anything else is refused, and a file at that path whose
    content is not a probe key line (restrict,command="<probe-ok>",expiry-time="...Z") is refused and left in place.
    Idempotent: nothing to remove returns False with no error. EVERY outcome is an audit event (rev 6): probe-swept-<mode>
    (removed), sweep-none-present, sweep-skipped-unexpired, sweep-refused-non-probe, sweep-refused."""
    import json
    import os

    if invocation.get("program") != PROBE_PATH or invocation.get("ruid") != 0 or invocation.get("euid") != 0:
        audit.append({"event": "sweep-refused", "reason": "root-only: real and effective UID 0 via the probe program", "at": now})
        return False
    probe_account = cfg.get("probe_account")
    if mode not in PROBE_SWEEP_MODES or probe_account is None or (account is not None and account != probe_account):
        audit.append({"event": "sweep-refused", "reason": "only the probe_account key, mode expired|all", "account": account, "at": now})
        return False
    path = os.path.join(keys_dir, probe_account)
    meta = path + ".probe.json"
    caller = invocation.get("sudo_user")
    if not os.path.lexists(path) and not os.path.lexists(meta):
        # rev 6 (Sol 6 amd1 r5): every outcome is an audit event, including the idempotent no-op
        audit.append({"event": "sweep-none-present", "mode": mode, "account": probe_account, "at": now, "caller": caller})
        return False
    if os.path.lexists(path):
        with open(path, encoding="utf-8") as handle:
            content = handle.read()
        if not content.startswith(f'restrict,command="{PROBE_OK}",expiry-time="') or content.count("\n") != 1:
            audit.append({"event": "sweep-refused-non-probe", "reason": "file is not a probe key line; left in place", "mode": mode,
                          "account": probe_account, "at": now, "caller": caller})
            return False
    if mode == "expired":
        expires = None
        expired_by_other_clock = False
        if os.path.lexists(meta):
            with open(meta, encoding="utf-8") as handle:
                record = json.load(handle)
            expires = record["expires_at"]
            # rev 8: a wall-clock rollback must not extend the probe: expired if EITHER the wall clock or the monotonic
            # clock says so, and always after a reboot (a different boot id: the monotonic clock restarted)
            # rev 9 (Sol 6 amd1 r8): the monotonic value and boot id are REQUIRED arguments (CLOCK_MONOTONIC and
            # /proc/sys/kernel/random/boot_id in the real program); a record without them is treated as expired
            expired_by_other_clock = (record.get("boot_id") != boot_id or "expires_mono" not in record
                                      or mono_now >= record["expires_mono"])
        if expires is not None and now < expires and not expired_by_other_clock:
            audit.append({"event": "sweep-skipped-unexpired", "mode": mode, "account": probe_account, "expires_at": expires,
                          "at": now, "caller": caller})
            return False
    for f in (path, meta):
        if os.path.lexists(f):
            os.unlink(f)
    audit.append({"event": f"probe-swept-{mode}", "mode": mode, "account": probe_account, "at": now, "caller": caller})
    return True



# ---------------------------------------------------------------- Amendment 3 rev 2 (Sol 6.1 amd3): C9w session boundary
SESSION_KEY_TYPES = ("ssh-ed25519", "sk-ssh-ed25519@openssh.com", "ecdsa-sha2-nistp256", "sk-ecdsa-sha2-nistp256@openssh.com")


P256_P = 2**256 - 2**224 + 2**192 + 2**96 - 1
P256_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B


def _ssh_fields(blob: bytes) -> list[bytes] | None:
    """Split an SSH wire blob into its length-prefixed strings; None unless it is consumed exactly."""
    import struct
    fields, i = [], 0
    while i < len(blob):
        if len(blob) - i < 4:
            return None
        (n,) = struct.unpack(">I", blob[i:i + 4])
        if n > len(blob) - i - 4:
            return None
        fields.append(blob[i + 4:i + 4 + n])
        i += 4 + n
    return fields


def _key_structure_problem(key_type: str, fields: list[bytes]) -> str | None:
    """Amendment 3 rev 3 (Sol 6.1 amd3r2): the COMPLETE public-key structure of each supported type (RFC 8709, RFC 5656,
    OpenSSH PROTOCOL.u2f), so the stored form is canonical only for a well-formed key."""
    if not fields or fields[0] != key_type.encode():
        return "the type inside the key blob differs from the declared type"
    rest = fields[1:]
    sk = key_type.startswith("sk-")
    if sk:
        if not rest or not rest[-1].startswith(b"ssh:") or len(rest[-1]) > 128:
            return "a security-key blob ends with an application string starting 'ssh:'"
        rest = rest[:-1]
    if key_type in ("ssh-ed25519", "sk-ssh-ed25519@openssh.com"):
        return None if len(rest) == 1 and len(rest[0]) == 32 else "an Ed25519 key is exactly one 32-byte public key"
    if len(rest) != 2 or rest[0] != b"nistp256":
        return "an ECDSA P-256 key names curve nistp256 and carries one point"
    point = rest[1]
    if len(point) != 65 or point[0] != 4:
        return "the P-256 point must be uncompressed (0x04 || X || Y, 65 bytes)"
    x, y = int.from_bytes(point[1:33], "big"), int.from_bytes(point[33:], "big")
    if not (x < P256_P and y < P256_P) or (y * y - (x * x * x - 3 * x + P256_B)) % P256_P != 0:
        return "the P-256 point is not on the curve"
    return None


def parse_session_key(text: str) -> tuple[dict[str, str] | None, list[str]]:
    """P1-4 key enrolment: accept ONE bare OpenSSH public key line ('<type> <base64> [comment]'). Refused: options before
    the type (the helper writes its own restrict/expiry-time/command options), line breaks or control characters, an
    unknown type, invalid base64, or (rev 3) a blob that is not the complete, exactly consumed structure of its type:
    Ed25519 one 32-byte key; ECDSA curve nistp256 and an uncompressed point on the curve; sk- types also an 'ssh:'
    application. Returns the canonical form '<type> <base64>' (comment dropped) and the ssh-keygen SHA256 fingerprint."""
    import base64
    import binascii
    import hashlib
    if not isinstance(text, str) or any(ord(c) < 0x20 or ord(c) > 0x7E for c in text) or len(text) > 2048:
        return None, ["the key must be one printable ASCII line with no control characters or line breaks"]
    parts = text.split(" ")
    if len(parts) < 2 or parts[0] not in SESSION_KEY_TYPES:
        return None, [f"the line must start with a key type from {SESSION_KEY_TYPES} (no options)"]
    if len(parts) > 2 and (parts[2] == "" or len(" ".join(parts[2:])) > 100):
        return None, ["the comment must be one short field"]
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (binascii.Error, ValueError):
        return None, ["the key body is not valid base64"]
    fields = _ssh_fields(blob)
    if fields is None:
        return None, ["the key blob is truncated or has trailing bytes"]
    problem = _key_structure_problem(parts[0], fields)
    if problem:
        return None, [problem]
    canonical_b64 = base64.b64encode(blob).decode("ascii")
    fpr = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    return {"key_type": parts[0], "public_key": f"{parts[0]} {canonical_b64}", "key_fingerprint": fpr}, []


def session_key_enrol(registry: dict[str, dict[str, Any]], principal_id: str, text: str, *, friend_sessions_on: bool, now: str,
                      label: str | None = None) -> dict[str, Any]:
    """session-key-enrol: creates friend state, so it is refused while features.friend_sessions is off (checked FIRST,
    before the key is even parsed). The principal comes from the authenticated peer, never from the request.
    Rev 3 (Sol 6.1 amd3r2): a REVOKED fingerprint is never re-enrolled, by anyone, until the operator purges its record
    (session-key-revoke with purge): a revocation usually means the key is lost or compromised, and a friend-side
    re-enrolment would silently undo it."""
    if not friend_sessions_on:
        return {"ok": False, "error": {"code": "unavailable", "message": "friend_sessions is off"}}
    key, problems = parse_session_key(text)
    if key is None:
        return {"ok": False, "error": {"code": "invalid", "message": problems[0]}}
    held = registry.get(key["key_fingerprint"])
    if held is not None and held["revoked_at"] is not None:
        return {"ok": False, "error": {"code": "conflict", "message": "this key was revoked; only an operator purge allows it again"}}
    if held is not None and held["principal_id"] != principal_id:
        return {"ok": False, "error": {"code": "conflict", "message": "key enrolled by another principal"}}
    record = {"schema_version": 2, "principal_id": principal_id, "label": label, "enrolled_at": now, "revoked_at": None, **key}
    registry[key["key_fingerprint"]] = record
    return {"ok": True, "error": None, "key": dict(record)}


def session_key_list(registry: Mapping[str, Mapping[str, Any]], principal_id: str, *, operator: bool = False) -> list[dict[str, Any]]:
    """session-key-list: read-only, works while off; a friend sees only their own keys."""
    return [dict(r) for _, r in sorted(registry.items()) if operator or r["principal_id"] == principal_id]


def session_key_revoke(registry: dict[str, dict[str, Any]], principal_id: str, fingerprint: str, *, now: str,
                       operator: bool = False, purge: bool = False) -> dict[str, Any]:
    """session-key-revoke: cleanup, works while off. Another principal's key is 'not_found' (no existence oracle). The
    revoked record stays as a tombstone that blocks re-enrolment; rev 3: purge=True (operator only, an already revoked
    key) deletes the tombstone, the one route by which that key may be enrolled again."""
    record = registry.get(fingerprint)
    if record is None or (record["principal_id"] != principal_id and not operator):
        return {"ok": False, "error": {"code": "not_found", "message": "no such key"}}
    if purge:
        if not operator:
            return {"ok": False, "error": {"code": "denied", "message": "only the operator purges a revoked key"}}
        if record["revoked_at"] is None:
            return {"ok": False, "error": {"code": "conflict", "message": "revoke before purging"}}
        del registry[fingerprint]
        return {"ok": True, "error": None}
    if record["revoked_at"] is None:
        record["revoked_at"] = now
    return {"ok": True, "error": None}


def resolve_session_key(registry: Mapping[str, Mapping[str, Any]], principal_id: str, fingerprint: str) -> str | None:
    """session-open's fingerprint -> key resolution: only the caller's own, unrevoked key resolves."""
    record = registry.get(fingerprint)
    if record is None or record["principal_id"] != principal_id or record["revoked_at"] is not None:
        return None
    return record["public_key"]


def session_attribution(cgroup_path: str, open_sessions: Sequence[Mapping[str, Any]], lane_id: str) -> str | None:
    """P1-4 attribution: the parent lease of a process ONLY through the executor's session registry: its cgroup lies
    inside the user slice an OPEN entry records for THIS lane. No uid input exists: a friend-uid process anywhere else
    is not attributed (the caller then reports it 'external')."""
    import re
    if not cgroup_path.startswith("/") or "/../" in cgroup_path + "/" or "/./" in cgroup_path + "/":
        return None
    found = None
    for entry in open_sessions:
        if entry.get("state") != "open" or entry.get("lane_id") != lane_id or not re.fullmatch(r"user-[1-9][0-9]{0,9}\.slice", entry.get("slice", "")):
            continue
        if (cgroup_path + "/").startswith(f"/user.slice/{entry['slice']}/"):
            if found is not None and found != entry["parent_lease_id"]:
                return None  # two open entries claim one slice: ambiguous, never attribute
            found = entry["parent_lease_id"]
    return found


def executor_session_closes(registry: Sequence[Mapping[str, Any]], *, mono_now: float, lease_deadline_mono: Mapping[str, float],
                            reconcile_active: Sequence[str] | None = None) -> list[tuple[str, str]]:
    """P1-4 executor lifecycle: which open registry entries the executor closes by itself, and why. local-expiry: the
    entry's own monotonic deadline passed; controller-loss: its parent lease's executor-side deadline lapsed (no beat);
    reconcile: the authority's active list omits it. A missing parent deadline counts as lapsed (fail closed)."""
    closes = []
    for entry in registry:
        if entry["state"] != "open":
            continue
        if reconcile_active is not None and entry["session_id"] not in reconcile_active:
            closes.append((entry["session_id"], "reconcile"))
        elif mono_now >= entry["expires_mono"]:
            closes.append((entry["session_id"], "local-expiry"))
        elif mono_now >= lease_deadline_mono.get(entry["parent_lease_id"], float("-inf")):
            closes.append((entry["session_id"], "controller-loss"))
    return closes


# P1-5 seam loading: Amendment 3 rev 3 moved the loader, its three-state enablement model and the import trust boundary
# into the stdlib-only module c9_loader.py (it is exercised under the production interpreter flags, python3 -I -S -B).
from .c9_loader import (C9_BODY_STATES, C9_LOADER_STATES, C9_PACKAGES, C9_WINDOW_MAX_TTL_S, C9ImportGuard, c9_absent_audit,  # noqa: E402,F401
                        c9_body_allowed, c9_loader_state, c9_seam_load, c9_window_close, c9_window_open, c9_window_read,
                        c9_window_sweep, c9_window_valid, preloaded_outside_modules, release_stale_removals, stdlib_trusted,
                        third_party_locations, trusted_import_context)
