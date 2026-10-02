"""Site-selected adapters: validate before constructing any port implementation."""
import hashlib as _hashlib
import json as _json
import re as _re
import sys as _sys
from dataclasses import dataclass as _dataclass
from functools import lru_cache as _lru_cache
from pathlib import Path as _Path


@_lru_cache
def _schema(path):
    return _json.loads(path.read_bytes())


_PATH = _Path(__file__).resolve().parents[1] / "contracts/v2/adapters.schema.json"
_SCHEMA = _schema(_PATH)
PORTS = tuple(_SCHEMA["properties"]["ports"]["required"])
_IMPLS = ("fake", "dryrun", "real", "record")
_MUTATING = ("workload_runner", "inhibitor", "waker", "model_cache", "session_gateway", "notifier", "release_backend")
_CRITICAL = ("command_runner", "executor_transport", "workload_runner", "occupancy_probe", "inhibitor", "waker")
_DRYRUN = ("workload_runner", "inhibitor", "waker")
_READS = ("command_runner", "executor_transport", "occupancy_probe")


@_dataclass(frozen=True)
class Problem:
    port: str | None
    lane: str | None
    message: str


class AdapterRefused(Exception):
    def __init__(self, problems: list[Problem]):
        self.problems = list(problems) or [Problem(None, None, "adapter selection refused")]
        super().__init__("; ".join(p.message for p in self.problems))


def _shape(value, rule, source=_PATH, trail=()):
    """Check the structural keywords used by this frozen schema, without dependencies."""
    port = trail[1] if trail[:1] == ("ports",) and len(trail) > 1 else None
    if trail[:1] == ("lanes",) and len(trail) > 3 and trail[2] == "ports":
        port = trail[3]
    lane = trail[1] if trail[:1] == ("lanes",) and len(trail) > 1 else None
    if trail[:1] == ("allow_fake",) or (trail[:1] == ("lanes",) and len(trail) > 2 and trail[2] == "shadow_real"):
        port = value if isinstance(value, str) else None
    field = "/".join(map(str, trail)) + ("" if rule is False else f": {value!r:.60}")
    problem = Problem(port, lane, "invalid configuration field " + field)
    if rule is False:
        return [problem]
    if "$ref" in rule:
        file, pointer = rule["$ref"].split("#/")
        source = (source.parent / file).resolve() if file else source
        target = _schema(source)
        for key in pointer.split("/"):
            target = target[key]
        return _shape(value, target, source, trail)
    if "anyOf" in rule:
        return [] if any(not _shape(value, r, source, trail) for r in rule["anyOf"]) else [problem]
    kind = rule.get("type")
    types = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}
    if kind in types and not isinstance(value, types[kind]):
        return [problem]
    if kind == "integer" and (type(value) not in (int, float) or value % 1 != 0):
        return [problem]
    if ("enum" in rule and value not in rule["enum"]) or ("const" in rule and value != rule["const"]):
        return [problem]
    minimum, maximum = {"integer": ("minimum", "maximum"), "array": ("minItems", "maxItems"), "string": ("minLength", "maxLength")}.get(kind, ("", ""))
    size = len(value) if kind in ("string", "array") else value if kind == "integer" else 0
    if size < rule.get(minimum, size) or size > rule.get(maximum, size):
        return [problem]
    if kind == "string" and "pattern" in rule and not _re.search(rule["pattern"], value):
        return [problem]
    if kind == "array":
        if rule.get("uniqueItems") and any(item in value[:i] for i, item in enumerate(value)):
            return [problem]
        return [p for i, item in enumerate(value) for p in _shape(item, rule.get("items", {}), source, trail + (i,))]
    if kind == "object":
        missing = [p for key in rule.get("required", []) if key not in value
                   for p in _shape(None, False, source, trail + (key,))]
        return missing + [p for key, item in value.items() for p in
                          (_shape(key, rule.get("propertyNames", {}), source, trail + (key,)) +
                           _shape(item, rule.get("properties", {}).get(key, rule.get("additionalProperties", {})), source, trail + (key,)))]
    return []


def check_config(config) -> list[Problem]:
    try:
        problems = _shape(config, _SCHEMA)
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        return [Problem(None, None, f"cannot validate adapter selection: {exc}")]
    if problems:
        return problems
    profile, ports, features = config["profile"], config["ports"], config["features"]
    required = {p for f, on in features.items() if on for p in _SCHEMA["properties"]["features"]["x-required-ports"][f]}
    for port, impl in sorted(ports.items()):
        for invalid, message in (
            (profile == "live" and impl == "fake" and port not in config["allow_fake"],
             f"live profile refuses fake port {port} (not in allow_fake)"),
            (profile == "live" and port in required and impl != "real",
             f"live profile: port {port} is required by an enabled feature and is {impl}"),
            (profile == "sim" and impl != "fake", f"sim profile refuses {impl} port {port}"),
            (profile == "shadow" and port in _MUTATING and impl in ("real", "record"),
             f"shadow profile: mutating port {port} is {impl} (shadow writes nothing)"),
            (impl == "dryrun" and port not in _MUTATING, f"port {port} is read-only and has no dryrun twin"),
        ):
            if invalid:
                problems.append(Problem(port, None, message))
    for port in config["allow_fake"]:
        if port in _CRITICAL or port in required:
            problems.append(Problem(port, None, f"allow_fake may not list {port}: lane-critical or feature-required"))
    for lane, entry in sorted(config["lanes"].items()):
        mode, overrides = entry["mode"], entry.get("ports", {})
        effective, exception = ports | overrides, entry.get("shadow_real", [])
        if exception and mode != "shadow":
            problems.append(Problem("inhibitor", lane, f"lane {lane} is {mode}; shadow_real needs a shadow lane"))
        if mode == "live":
            if profile != "live":
                problems.append(Problem(None, lane, f"lane {lane} is live but the site profile is {profile}"))
            for port in sorted(set(_CRITICAL) | required):
                if not (port == "waker" and not entry.get("wake_needed", True)) and effective[port] != "real":
                    problems.append(Problem(port, lane, f"lane {lane} is live but {port} is {effective[port]}"))
        if mode == "shadow":
            if profile == "sim":
                problems.append(Problem(None, lane, f"lane {lane} is shadow but the site profile is sim"))
            if exception and not entry.get("legacy_lane"):
                problems.append(Problem("inhibitor", lane, f"lane {lane}: shadow_real needs legacy_lane"))
            for port in _DRYRUN + _READS:
                if port == "waker" and not entry.get("wake_needed", True):
                    continue
                want = "real" if port in _READS or port in exception else "dryrun"
                if effective[port] != want:
                    message = f"lane {lane} is shadow but {port} is {effective[port]} (must be {want})"
                    problems.append(Problem(port, lane, message))
            for port in _MUTATING:
                if port not in exception and effective[port] in ("real", "record"):
                    message = (f"lane {lane} is shadow but mutating port {port} is {effective[port]}"
                               " (shadow writes nothing)")
                    problems.append(Problem(port, lane, message))
        if mode == "off" and overrides:
            problems.extend(Problem(port, lane, f"lane {lane} is off but carries override {port}={impl}")
                            for port, impl in overrides.items())
    tls = config["tls"]
    alert, renew, check = (tls["rotation"][k] for k in ("alert_before_s", "renew_before_s", "check_interval_s"))
    files = {tls[k] for k in ("cert_file", "key_file", "chain_file")}
    served_roots = sorted(files & set(tls["trust_anchor"]["root_ca_files"]))
    for invalid, message in (
        (features["sessions"] and not features["friend_sessions"], "sessions requires friend_sessions"),
        (alert >= renew, f"tls alert_before_s {alert} must be below renew_before_s {renew}"),
        (check >= alert, f"tls check_interval_s {check} must be below alert_before_s {alert}"),
        (len(files) != 3, "tls cert_file, key_file and chain_file must be distinct"),
        (bool(served_roots), f"tls trust roots must not be served cert, key or chain files: {served_roots}"),
    ):
        if invalid:
            problems.append(Problem(None, None, message))
    return problems


class Selection:
    def __init__(self, config, sha256):
        self.profile, self.site_id, self.sha256 = config["profile"], config["site_id"], sha256
        self._ports, self._lanes = config["ports"], config["lanes"]

    def impl(self, port, lane=None) -> str:
        site = self._ports[port]
        return site if lane is None else self._lanes[lane].get("ports", {}).get(port, site)

    @property
    def banner(self):
        parts = [f"{self.profile.upper()} profile"]
        parts += [f"{lane} {entry['mode'].upper()}" + (" (dry-run executor writes)" if entry["mode"] == "shadow" else "")
                  for lane, entry in sorted(self._lanes.items())]
        fake = sorted(p for p, impl in self._ports.items() if impl == "fake")
        return "; ".join(parts + (["fake: " + ",".join(fake)] if fake else []))


def load_selection(path) -> Selection:
    try:
        raw = _Path(path).read_bytes()
        config = _json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        raise AdapterRefused([Problem(None, None, f"cannot read or parse adapter selection: {exc}")]) from exc
    problems = check_config(config)
    if problems:
        raise AdapterRefused(problems)
    return Selection(config, _hashlib.sha256(raw).hexdigest())


class Registry:
    def __init__(self):
        self._factories = {}

    def register(self, port, impl, factory):
        if port not in PORTS or impl not in _IMPLS or not callable(factory):
            raise ValueError(f"cannot register {port!r}={impl!r} with factory {factory!r}: "
                             "unknown port or implementation, or factory not callable")
        self._factories[port, impl] = factory

    def resolve(self, selection, port, lane=None):
        impl = selection.impl(port, lane)
        if (port, impl) not in self._factories:
            raise AdapterRefused([Problem(port, lane, f"no factory registered for {port}={impl}")])
        return self._factories[port, impl]()


def adapter_config_event(selection, *, seq, event_id, occurred_at, controller_id) -> dict:
    return dict(schema_version=2, seq=seq, event_id=event_id, occurred_at=occurred_at,
                controller_id=controller_id, kind="adapter-config", state="applied", actor="system",
                site_id=selection.site_id, request_id=None, lane_id=None, host_id=None, generation=None,
                error=None, refs={}, reason="adapter selection loaded", token_redacted=True,
                detail={"adapter_sha256": selection.sha256, "banner": selection.banner})


def main(argv=None) -> int:
    args = _sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m flightctl.adapters <path>", file=_sys.stderr)
        return 2
    try:
        selection = load_selection(args[0])
    except AdapterRefused as exc:
        for p in exc.problems:
            line = f"adapter-refused: port={p.port or '-'} lane={p.lane or '-'}: {p.message}"
            print(" ".join(line.split()), file=_sys.stderr)
        return 3
    print(_json.dumps({"profile": selection.profile, "adapter_sha256": selection.sha256, "banner": selection.banner,
                      "ports": {p: selection.impl(p) for p in PORTS},
                      "lanes": {lane: {"mode": entry["mode"], "ports": {p: selection.impl(p, lane) for p in PORTS}}
                                for lane, entry in sorted(selection._lanes.items())}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
