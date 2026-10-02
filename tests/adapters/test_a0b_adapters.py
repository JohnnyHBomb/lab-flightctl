import hashlib
import importlib
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from tests.contracts_v2.validation import (
    FEATURE_PORTS, LANE_CRITICAL_PORTS, MUTATING_PORTS, adapters_semantics,
    assert_invalid, assert_valid, required_ports,
)

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config/adapters-v2.json.example"
BANNER = ("LIVE profile; lane-desk OFF; lane-gpu0 SHADOW (dry-run executor writes); "
          "lane-gpu1 LIVE; fake: health_probe,model_cache,notifier,release_backend,session_gateway,signer")


def _example(tmp_path):
    path = tmp_path / "adapters.json"
    path.write_bytes(EXAMPLE.read_bytes())
    return path, json.loads(path.read_bytes())


def _checked(path, config, accepted=True, port=None, lane=None, shaped=True):
    if not shaped:
        assert_invalid(config, "adapters")
    assert bool(adapters_semantics(config)) is (not accepted and shaped)
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    module = importlib.import_module("flightctl.adapters")
    problems = module.check_config(config)
    assert bool(problems) is (not accepted)
    if accepted:
        return module.load_selection(path)
    with pytest.raises(module.AdapterRefused) as exc:
        module.load_selection(path)
    assert exc.value.problems == problems
    assert any(p.port == port and p.lane == lane for p in problems)


def test_registry_selects_per_port_and_lane(tmp_path):
    path, config = _example(tmp_path)
    selection = _checked(path, config)
    module = importlib.import_module("flightctl.adapters")
    registry = module.Registry()
    schema = json.loads((ROOT / "contracts/v2/adapters.schema.json").read_bytes())
    assert module.PORTS == tuple(schema["properties"]["ports"]["required"])
    for port in module.PORTS:
        for impl in ("fake", "dryrun", "real", "record"):
            registry.register(port, impl, lambda p=port, i=impl: (p, i))
        for lane in (None, *config["lanes"]):
            effective = config["lanes"][lane].get("ports", {}).get(port, config["ports"][port]) if lane else config["ports"][port]
            assert selection.impl(port, lane) == effective
            assert registry.resolve(selection, port, lane) == (port, effective)
    missing = module.Registry()
    missing.register("workload_runner", "fake", lambda: "fake")
    with pytest.raises(module.AdapterRefused) as exc:
        missing.resolve(selection, "workload_runner", "lane-gpu0")
    assert [(p.port, p.lane) for p in exc.value.problems] == [("workload_runner", "lane-gpu0")]
    missing.register("workload_runner", "dryrun", lambda: "dryrun")
    assert missing.resolve(selection, "workload_runner", "lane-gpu0") == "dryrun"
    for port, impl, factory in (("unknown", "real", list), ("clock", "unknown", list), ("clock", "real", None)):
        with pytest.raises(ValueError):
            registry.register(port, impl, factory)
    for port, lane in (("unknown", None), ("clock", "unknown")):
        with pytest.raises(KeyError):
            selection.impl(port, lane)
    config["ports"]["command_runner"] = "fake"
    _checked(path, config, False, "command_runner")


def test_feature_required_ports_never_fake_in_live(tmp_path):
    path, config = _example(tmp_path)
    _checked(path, config)
    lane_keys = set(json.loads((ROOT / "contracts/v2/adapters.schema.json").read_bytes())["properties"]["lanes"]["additionalProperties"]["properties"]["ports"]["properties"])
    for feature in FEATURE_PORTS:
        enabled = deepcopy(config)
        enabled["features"][feature] = True
        if feature == "sessions":
            enabled["features"]["friend_sessions"] = True
        required = required_ports(enabled["features"])
        enabled["ports"].update(dict.fromkeys(required, "real"))
        enabled["allow_fake"] = [p for p in enabled["allow_fake"] if p not in required]
        _checked(path, enabled)
        for port in required:
            for impl in ("fake", "dryrun", "record"):
                bad = deepcopy(enabled)
                bad["ports"][port] = impl
                _checked(path, bad, False, port)
            bad = deepcopy(enabled)
            bad["allow_fake"].append(port)
            _checked(path, bad, False, port)
        for port in (required | set(LANE_CRITICAL_PORTS)) & lane_keys:
            bad = deepcopy(enabled)
            bad["lanes"]["lane-gpu1"]["ports"] = {port: "fake"}
            _checked(path, bad, False, port, "lane-gpu1")
    for port in LANE_CRITICAL_PORTS:
        bad = deepcopy(config)
        bad["allow_fake"].append(port)
        _checked(path, bad, False, port)
    for impl, shaped in (("dryrun", True), ("bogus", False)):
        bad = deepcopy(config)
        bad["ports"]["health_probe"] = impl
        _checked(path, bad, False, "health_probe", shaped=shaped)


def test_shadow_lane_has_no_real_mutating_port(tmp_path):
    path, config = _example(tmp_path)
    _checked(path, config)
    for port in MUTATING_PORTS:
        for impl in ("real", "record"):
            bad = deepcopy(config)
            bad["lanes"]["lane-gpu0"]["ports"][port] = impl
            _checked(path, bad, False, port, "lane-gpu0")
    exception = deepcopy(config)
    exception["lanes"]["lane-gpu0"]["shadow_real"] = ["inhibitor"]
    exception["lanes"]["lane-gpu0"]["ports"]["inhibitor"] = "real"
    assert _checked(path, exception).impl("inhibitor", "lane-gpu0") == "real"
    exception["lanes"]["lane-gpu0"]["legacy_lane"] = None
    _checked(path, exception, False, "inhibitor", "lane-gpu0")


def test_banner_and_event_hash(tmp_path):
    path, config = _example(tmp_path)
    selection = _checked(path, config)
    module = importlib.import_module("flightctl.adapters")
    raw = path.read_bytes()
    assert selection.profile == "live" and selection.site_id == config["site_id"]
    assert selection.sha256 == hashlib.sha256(raw).hexdigest()
    assert selection.banner == BANNER
    edited = raw[:-1] + b" "
    assert len(edited) == len(raw) and sum(a != b for a, b in zip(raw, edited)) == 1
    path.write_bytes(edited)
    assert adapters_semantics(json.loads(path.read_bytes())) == []
    changed = module.load_selection(path)
    assert changed.sha256 != selection.sha256 and changed.banner == BANNER
    event = module.adapter_config_event(selection, seq=1, event_id="evt-1",
                                        occurred_at="2026-10-02T10:00:00Z", controller_id="controller-a")
    assert_valid(event, "event")
    assert event == dict(schema_version=2, seq=1, event_id="evt-1", occurred_at="2026-10-02T10:00:00Z",
                         controller_id="controller-a", kind="adapter-config", state="applied", actor="system",
                         site_id=config["site_id"], request_id=None, lane_id=None, host_id=None, generation=None,
                         error=None, refs={}, reason="adapter selection loaded", token_redacted=True,
                         detail={"adapter_sha256": selection.sha256, "banner": BANNER})
    event["detail"]["adapter_sha256"] = "invalid"
    assert_invalid(event, "event")
    path.write_bytes(b"\xef\xbb\xbf" + raw)
    for odd in (path, None):
        with pytest.raises(module.AdapterRefused):
            module.load_selection(odd)
    config["ports"]["clock"] = "fake"
    _checked(path, config, False, "clock")


@pytest.mark.realtime
def test_cli_startup_refusal_exit3(tmp_path):
    path, config = _example(tmp_path)

    def run(*args):
        return subprocess.run([sys.executable, "-m", "flightctl.adapters", *map(str, args)],
                              cwd=ROOT, capture_output=True, text=True, timeout=10)

    config["lanes"]["lane-gpu1"]["ports"] = {"workload_runner": "fake"}
    assert adapters_semantics(config)
    path.write_text(json.dumps(config), encoding="utf-8")
    refused = run(path)
    assert refused.returncode == 3 and refused.stdout == ""
    assert "port=workload_runner lane=lane-gpu1:" in refused.stderr
    assert all(line.startswith("adapter-refused: port=") for line in refused.stderr.splitlines())
    for invalid in (tmp_path / "missing.json", tmp_path, path):
        path.write_bytes(b"{")
        result = run(invalid)
        assert result.returncode == 3 and result.stdout == "" and "adapter-refused:" in result.stderr
    path, config = _example(tmp_path)
    assert adapters_semantics(config) == []
    result = run(path)
    assert result.returncode == 0 and result.stderr == "" and len(result.stdout.splitlines()) == 1
    assert list(json.loads(result.stdout)["lanes"]) == sorted(config["lanes"])
    assert json.loads(result.stdout) == {"profile": config["profile"], "banner": BANNER,
        "adapter_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "ports": config["ports"],
        "lanes": {lane: {"mode": entry["mode"], "ports": config["ports"] | entry.get("ports", {})}
                  for lane, entry in config["lanes"].items()}}
    for args in ((), (path, path)):
        result = run(*args)
        assert result.returncode == 2 and result.stdout == "" and "usage:" in result.stderr
