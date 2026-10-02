"""Amendment 4 (race R-A0b follow-ups): the adapter-refused requirement, the example file name, the shadow_real oracle
hole, sim-profile lane overrides, the ci.yml append allowance and the harvested-merge line cap."""

import copy
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from flightctl import adapters as A

from .validation import adapters_semantics, errors, validator

_VALIDATOR = validator("adapters")

ROOT = Path(__file__).parents[2]
ADAPTERS_MD = (ROOT / "contracts/v2/ADAPTERS.md").read_text(encoding="utf-8")
SCHEMA_TEXT = (ROOT / "contracts/v2/adapters.schema.json").read_text(encoding="utf-8")
EXAMPLE = json.loads((ROOT / "config/adapters-v2.json.example").read_text(encoding="utf-8"))
PORTS = sorted(EXAMPLE["ports"])
IMPLS = ("fake", "dryrun", "real", "record")
LANE_SCOPED_ALL = set(A._CRITICAL) | set(A._MUTATING)


def contract_accepts(config) -> bool:
    return not any(True for _ in _VALIDATOR.iter_errors(config)) and not adapters_semantics(config)


def sim_site() -> dict:
    config = copy.deepcopy(EXAMPLE)
    config["profile"] = "sim"
    config["ports"] = {p: "fake" for p in config["ports"]}
    config["allow_fake"] = []
    config["lanes"] = {"lane-sim": {"mode": "sim", "ports": {}}}
    return config


def test_adapter_refused_is_a_stderr_line_and_exit_3_not_an_event(tmp_path: Path) -> None:
    events = json.loads((ROOT / "contracts/v2/event.schema.json").read_text(encoding="utf-8"))["$defs"]["kind"]["enum"]
    assert "adapter-refused" not in events and "`adapter-refused` event" not in ADAPTERS_MD
    rule = ADAPTERS_MD.split("\n8. ", 1)[1].split("\n\n", 1)[0]
    assert "exit 3" in rule and "`adapter-refused: port=<port|-> lane=<lane|->: <message>`" in rule
    bad = copy.deepcopy(EXAMPLE)
    bad["allow_fake"].append("inhibitor")
    path = tmp_path / "adapters.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    run = subprocess.run([sys.executable, "-m", "flightctl.adapters", str(path)], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert run.returncode == 3 and run.stdout == ""
    assert all(re.fullmatch(r"adapter-refused: port=[\w-]+ lane=[\w-]+: .+", l) for l in run.stderr.splitlines()), run.stderr


def test_the_named_example_file_exists() -> None:
    named = set(re.findall(r"config/[\w.-]+\.example", ADAPTERS_MD + SCHEMA_TEXT))
    assert named == {"config/adapters-v2.json.example"}
    assert all((ROOT / n).is_file() for n in named)


@pytest.mark.parametrize("port", [p for p in PORTS if p != "inhibitor"])
def test_shadow_real_names_only_the_inhibitor_in_oracle_schema_and_module(port) -> None:
    config = copy.deepcopy(EXAMPLE)
    lane = config["lanes"]["lane-gpu0"]
    lane["shadow_real"], lane["legacy_lane"] = [port], lane.get("legacy_lane") or "gpu0"
    lane["ports"][port] = "real"
    assert any("shadow_real may name only the inhibitor" in p for p in adapters_semantics(config))
    assert errors(config, "adapters") and A.check_config(config)


@pytest.mark.parametrize("impl", ["dryrun", "real", "record"])
@pytest.mark.parametrize("port", PORTS)
def test_sim_profile_refuses_non_fake_lane_overrides(port, impl) -> None:
    config = sim_site()
    assert contract_accepts(config) and not A.check_config(config)
    config["lanes"]["lane-sim"]["ports"][port] = impl
    assert f"lane lane-sim is sim but port {port} is {impl} (a sim lane runs on fakes)" in adapters_semantics(config) or (
        port not in LANE_SCOPED_ALL and errors(config, "adapters"))
    assert errors(config, "adapters")
    assert any(p.port == port and p.lane == "lane-sim" for p in A.check_config(config))


def _corpus():
    """Deterministic mutations of the example (live) and a sim site: every lane x port x impl override, every
    profile, every lane mode, every shadow_real port."""
    bases = [EXAMPLE, sim_site()]
    for base in bases:
        for profile in ("sim", "shadow", "live"):
            for lane in base["lanes"]:
                for port in PORTS:
                    for impl in IMPLS:
                        c = copy.deepcopy(base)
                        c["profile"] = profile
                        c["lanes"][lane].setdefault("ports", {})[port] = impl
                        yield c
                for mode in ("off", "sim", "shadow", "live"):
                    c = copy.deepcopy(base)
                    c["profile"] = profile
                    c["lanes"][lane]["mode"] = mode
                    yield c
                for port in PORTS:
                    c = copy.deepcopy(base)
                    c["profile"] = profile
                    c["lanes"][lane]["shadow_real"] = [port]
                    c["lanes"][lane]["legacy_lane"] = "legacy"
                    yield c


def test_differential_contract_versus_merged_module() -> None:
    """Amendment 4: the contract (schema + oracle) and the merged flightctl/adapters.py agree on accept/refuse over the
    whole corpus, including the two R-A0b holes."""
    corpus = list(_corpus())
    mismatches = [c for c in corpus if contract_accepts(c) is bool(A.check_config(c))]
    assert len(corpus) > 1000 and not mismatches, (len(mismatches), json.dumps(mismatches[:1])[:600])
    assert sum(contract_accepts(c) for c in corpus) > 50  # the corpus is not all refusals


def test_plan_text_carries_the_ci_append_and_the_harvest_cap() -> None:
    gates = (ROOT / "docs/v2/GATES.txt").read_text(encoding="utf-8")
    g1 = gates.split("  G1 RACE GATE", 1)[1].split("\n  G1b ", 1)[0]
    assert "HARD gate for every" in g1 and "HARVESTED merge" in g1 and "line cost of each harvested item" in g1
    assert "CI APPEND" in g1 and "--ci-append tests/<dir>" in g1 and "exactly one way" in g1
    # rev 3: the fixed CIYML check enforces the rule; the rev-2 withdrawal stays as dated history
    assert "CIYML check (--ci-append tests/<dir>, one per new directory) enforces it" in g1
    assert "revision 2 (Sol 6.1 amd4) found" in g1 and "revision 3 (Sol 6.1 amd4r2) confirmed" in g1
    assert "is to enforce" not in g1 and "until then the reviewer checks" not in g1
    slices = (ROOT / "docs/v2/SLICES.md").read_text(encoding="utf-8")
    rules = slices.split("## Rules every brief carries", 1)[1].split("\n## ", 1)[0]
    assert "appends exactly the token `tests/<dir>`" in rules and "only a harvested merge may" in rules


# ---------------------------------------------------------------- revision 2 (Sol 6.1 amd4)
LANE_PORTS = set(json.loads(SCHEMA_TEXT)["properties"]["lanes"]["additionalProperties"]["properties"]["ports"]["properties"])
READ_ONLY = [p for p in PORTS if p not in A._MUTATING and p in LANE_PORTS]  # the others cannot be lane overrides at all (schema)
LANE_SCOPED = sorted(set(A._CRITICAL) | set(A._MUTATING))
SHARED = [p for p in PORTS if p not in LANE_SCOPED]


def test_example_read_only_ports_are_not_all_feature_required() -> None:
    from .validation import required_ports
    free = [p for p in READ_ONLY if p not in required_ports(EXAMPLE["features"])]
    assert "health_probe" in free and EXAMPLE["lanes"]["lane-gpu1"]["mode"] == "live"


@pytest.mark.parametrize("port", READ_ONLY)
def test_rule6_read_only_lane_override_cannot_be_dryrun_on_a_live_lane(port) -> None:
    """Sol 6.1 amd4 (inherited from c676b2c): ADAPTERS rule 6 holds for lane overrides too."""
    config = copy.deepcopy(EXAMPLE)
    config["lanes"]["lane-gpu1"]["ports"] = {port: "dryrun"}
    assert f"lane lane-gpu1: port {port} is read-only and has no dryrun twin; use real or fake" in adapters_semantics(config)
    assert any(p.port == port and p.lane == "lane-gpu1" and "no dryrun twin" in p.message for p in A.check_config(config))


@pytest.mark.parametrize("port", READ_ONLY)
def test_rule6_read_only_lane_override_cannot_be_dryrun_on_a_sim_lane(port) -> None:
    config = sim_site()
    config["lanes"]["lane-sim"]["ports"] = {port: "dryrun"}
    assert f"lane lane-sim: port {port} is read-only and has no dryrun twin; use real or fake" in adapters_semantics(config)
    assert any(p.port == port and p.lane == "lane-sim" and "no dryrun twin" in p.message for p in A.check_config(config))


def shadow_site() -> dict:
    config = copy.deepcopy(EXAMPLE)
    config["profile"] = "shadow"
    for p in A._MUTATING:
        config["ports"][p] = "dryrun" if p in A._DRYRUN else "fake"
    config["lanes"].pop("lane-gpu1")
    return config


@pytest.mark.parametrize("site", ["live", "shadow"])
def test_sim_lane_in_a_live_or_shadow_site_runs_on_fakes(site) -> None:
    """Sol 6.1 amd4 recommendation, taken: a sim lane's EFFECTIVE lane-scoped ports are fake (overrides and inherited
    values); the shared site-level ports may be inherited but never overridden to anything but fake."""
    config = EXAMPLE if site == "live" else shadow_site()
    assert contract_accepts(config) and not A.check_config(config)
    inherits = copy.deepcopy(config)
    inherits["lanes"]["lane-sim"] = {"mode": "sim"}
    assert not contract_accepts(inherits) and A.check_config(inherits)  # inherits the site's real/dryrun lane-scoped ports
    good = copy.deepcopy(config)
    good["lanes"]["lane-sim"] = {"mode": "sim", "ports": {p: "fake" for p in LANE_SCOPED}}
    assert contract_accepts(good) and not A.check_config(good), (adapters_semantics(good), A.check_config(good))
    for port in LANE_SCOPED:
        for impl in ("real", "dryrun", "record"):
            bad = copy.deepcopy(good)
            bad["lanes"]["lane-sim"]["ports"][port] = impl
            assert not contract_accepts(bad) and any(p.port == port and p.lane == "lane-sim" for p in A.check_config(bad)), (port, impl)
    for port in SHARED:
        explicit = copy.deepcopy(good)
        explicit["lanes"]["lane-sim"]["ports"][port] = "real"
        assert errors(explicit, "adapters") and any(p.port == port for p in A.check_config(explicit)), port


def test_orphaned_adapter_refused_topic_and_error_code_are_removed() -> None:
    topics = json.loads((ROOT / "contracts/v2/notification.schema.json").read_text(encoding="utf-8"))
    codes = json.loads((ROOT / "contracts/v2/common.schema.json").read_text(encoding="utf-8"))["$defs"]["error_code"]["enum"]
    assert "adapter-refused" not in json.dumps(topics) and "adapter_refused" not in codes
    rule = ADAPTERS_MD.split("\n7. ", 1)[1].split("\n8. ", 1)[0]
    for name in ("`clock`", "`inventory_probe`", "`peer_identity`", "`signer`", "`health_probe`", "`legacy_observer`"):
        assert name in rule, name
    assert {n.strip("`") for n in re.findall(r"`[a-z_]+`", rule.split("SHARED exceptions", 1)[1])} >= set(SHARED)


def test_semgrep_scans_flightctl_deploy_and_tests() -> None:
    """Rev 2 (lead, measured): the pr job scans flightctl/ too, and the repo .semgrepignore replaces semgrep's built-in
    list (which skipped tests/) while keeping only tool and cache directories out."""
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "run: semgrep scan --error --config p/python flightctl deploy tests" in workflow
    ignored = [l.strip() for l in (ROOT / ".semgrepignore").read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]
    assert ignored == [".git/", ".venv/", "__pycache__/", ".pytest_cache/"]
    for path in ("tests/contracts_v2/test_v2_amendment3_boundary.py", "tests/roster/shims.py"):
        for line in (ROOT / path).read_text(encoding="utf-8").splitlines():
            if "nosemgrep" in line:
                assert re.search(r"# nosemgrep: python\.lang\.security\.audit\.[\w.-]+ -- \S", line), (path, line)  # rule and reason named
