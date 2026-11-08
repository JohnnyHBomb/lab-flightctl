"""Amendment 14 (drift plan P15): G3 evidence must come from the host it names. The 7 Nov vacuous clock evidence fails
the strict validator; evidence records the observed hostname and boot id; strict refuses a mismatch; the real clock twin
refuses a remote target; TARGET is normalised. Host names here are synthetic (host-a, host-b, example.net, .invalid)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conformance import conftest as harness
from tests.conformance import registry
from tests.conformance.host_identity import (
    evidence_problems,
    hostname_matches,
    local_identity,
    normalise_target,
)
from tests.conformance.impl_clock import RemoteClockTarget, real_clock

ROOT = Path(__file__).resolve().parents[2]
GATES = (ROOT / "docs/v2/GATES.txt").read_text(encoding="utf-8")
CONFORMANCE = (ROOT / "docs/v2/CONFORMANCE.tsv").read_text(encoding="utf-8")
VACUOUS = json.loads((ROOT / "tests/contracts_v2/captures/g3-vacuous-clock-20261107.json").read_text(encoding="utf-8"))
BOOT_A, BOOT_B = "aaaaaaaa-0000-4000-8000-000000000001", "bbbbbbbb-0000-4000-8000-000000000002"


def _vacuous() -> dict:
    return {k: v for k, v in VACUOUS.items() if not k.startswith("_")}


def _pytest(env_extra: dict, tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("FLIGHTCTL_CONFORMANCE")}
    env.update(env_extra, FLIGHTCTL_CONFORMANCE_EVIDENCE=str(tmp_path))
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-o", "addopts=", "-p", "no:cacheprovider", *args],
                          cwd=ROOT, env=env, capture_output=True, text=True, timeout=180, check=False)


def test_the_7_nov_vacuous_clock_evidence_fails_the_strict_validator() -> None:
    as_written = _vacuous()
    assert as_written["proven"] is True and as_written["strict"] is True  # it claimed proof
    assert "no observed host identity (hostname, boot_id) recorded" in evidence_problems(as_written)
    runner_seen = dict(as_written, observed={"hostname": "host-a", "boot_id": BOOT_A})  # what the twin really read
    assert evidence_problems(runner_seen) == ["observed host 'host-a' is not the target 'host-b'"]
    on_target = dict(as_written, observed={"hostname": "host-b", "boot_id": BOOT_B})
    assert evidence_problems(on_target) == []
    for broken in ({"hostname": "", "boot_id": BOOT_B}, {"hostname": "host-b", "boot_id": "process-1"}, None):
        assert evidence_problems(dict(as_written, observed=broken)), broken
    assert evidence_problems(dict(on_target, strict=False)) == ["not strict"]
    assert evidence_problems(dict(on_target, implementation="fake"))


def test_hostname_match_rule() -> None:
    assert hostname_matches("host-b", "host-b") and hostname_matches("user@host-b.example.net", "host-b")
    assert hostname_matches("HOST-B.example.net", "host-b.example.net")
    assert not hostname_matches("host-b", "host-a") and not hostname_matches("host-b.example.net", "host-bb")
    assert not hostname_matches("192.0.2.7", "host-b") and not hostname_matches("host-b", "")


def test_real_clock_factory_refuses_a_remote_target() -> None:
    for target in ("host-b", "user@host-b.example.net"):
        with pytest.raises(RemoteClockTarget, match="host-local"):
            real_clock(target)
    assert real_clock("host-local").boot_id() == local_identity()["boot_id"]  # on-host runs unchanged


def test_target_normalisation_with_a_synthetic_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    names = {"host-a": "host-a.example.net", "host-c": "other.example.net", "host-d": None}
    asked = []

    def resolve(name):
        asked.append(name)
        if name not in names:
            raise OSError("synthetic: unknown name")
        return names[name]

    cases = {"host-local": "host-local", "host-a": "host-a.example.net", "user@host-a": "user@host-a.example.net",
             "host-a.example.net": "host-a.example.net", "192.0.2.7": "192.0.2.7", "2001:db8::1": "2001:db8::1",
             "host-c": "host-c", "host-d": "host-d", "host-z": "host-z", "": None, None: None}
    for raw, expected in cases.items():
        assert normalise_target(raw, resolve) == expected, raw
    assert sorted(asked) == ["host-a", "host-a", "host-c", "host-d", "host-z"]  # no lookup for host-local, FQDNs or IPs
    monkeypatch.setenv("FLIGHTCTL_CONFORMANCE_TARGET", "host-local")
    assert registry.target() == "host-local"
    monkeypatch.setenv("FLIGHTCTL_CONFORMANCE_TARGET", "user@host-b.invalid")
    assert registry.target() == "user@host-b.invalid"


def test_strict_is_per_listed_port_and_identity_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLIGHTCTL_CONFORMANCE_STRICT", "1")
    monkeypatch.setenv("FLIGHTCTL_CONFORMANCE_PORTS", "clock")
    monkeypatch.setattr(harness, "target_identity", lambda host: ({"hostname": "host-b", "boot_id": BOOT_B}, ()))
    ok = {"a": "passed", "b": "n/a"}
    clock = harness.evidence_record("clock", "real", "host-b", "c0", "t0", ok)
    assert clock["strict"] and clock["proven"] and clock["observed"]["hostname"] == "host-b" and evidence_problems(clock) == []
    unlisted = harness.evidence_record("command_runner", "real", "host-b", "c0", "t0", ok)
    assert unlisted["strict"] is False and unlisted["proven"] is False and "observed" in unlisted
    assert "observed" not in harness.evidence_record("clock", "fake", "host-b", "c0", "t0", ok)
    monkeypatch.setattr(harness, "target_identity", lambda host: ({"hostname": "host-a", "boot_id": BOOT_A}, ("observed host 'host-a' is not the target 'host-b'",)))
    mismatch = harness.evidence_record("clock", "real", "host-b", "c0", "t0", ok)
    assert mismatch["proven"] is False and mismatch["identity_problems"] and evidence_problems(mismatch)


def test_a_real_host_local_strict_clock_run_writes_evidence_the_validator_accepts(tmp_path: Path) -> None:
    run = _pytest({"FLIGHTCTL_CONFORMANCE_STRICT": "1", "FLIGHTCTL_CONFORMANCE_PORTS": "clock",
                   "FLIGHTCTL_CONFORMANCE_TARGET": "host-local"}, tmp_path, "tests/conformance/test_clock_and_commands.py", "-k", "clock-")
    assert run.returncode == 0, run.stdout[-800:]
    (evidence,) = tmp_path.glob("conformance-clock-real-host-local-*.json")
    record = json.loads(evidence.read_text(encoding="utf-8"))
    assert evidence_problems(record) == [] and record["observed"] == local_identity()


def test_strict_run_refuses_a_target_whose_identity_it_cannot_observe(tmp_path: Path) -> None:
    run = _pytest({"FLIGHTCTL_CONFORMANCE_STRICT": "1", "FLIGHTCTL_CONFORMANCE_PORTS": "clock",
                   "FLIGHTCTL_CONFORMANCE_TARGET": "host-b.invalid"}, tmp_path, "tests/conformance/test_clock_and_commands.py", "-k", "clock-")
    assert run.returncode != 0 and "STRICT conformance: evidence would not be from host-b.invalid" in run.stdout, run.stdout[-800:]
    (evidence,) = tmp_path.glob("conformance-clock-real-host-b.invalid-*.json")
    record = json.loads(evidence.read_text(encoding="utf-8"))
    assert record["proven"] is False and record["observed"] is None and record["identity_problems"] and evidence_problems(record)


def test_g3_rule_and_rows_are_pinned() -> None:
    g3 = GATES.split("G3 REAL TWIN", 1)[1].split("G4 REAL TIME", 1)[0]
    for phrase in ("Amendment 14", "\"observed\" {hostname, boot_id}", "host_identity.evidence_problems() is empty",
                   "\"strict\" is true only for the listed", "The real clock twin refuses a remote target"):
        assert phrase in " ".join(g3.split()), phrase
    rows = {l.split("\t")[0]: l.split("\t") for l in CONFORMANCE.splitlines() if l.startswith("amd14-")}
    assert sorted(rows) == ["amd14-1", "amd14-2", "amd14-3"] and all(r[3] == "contract-fixed" for r in rows.values())
    names = {n.split("::")[1].split()[0] for r in rows.values() for n in r[4].split() if "test_v2_amendment14.py::" in n}
    assert names <= set(globals()) and len(names) == 3
    assert (ROOT / "docs/v2/AMENDMENT-14.md").is_file()
