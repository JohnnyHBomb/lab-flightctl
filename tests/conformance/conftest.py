"""Conformance harness (skeleton), round 2 (Sol 6 B7).

* Port attribution is PER TEST: port_params(port) puts a `port(<name>)` marker on every parameter, and the
  evidence reporter reads that marker. A module may test several ports.
* Case applicability is declared, not decided inside the test body:
    @pytest.mark.fake_only   fault injection / sim-only behaviour; recorded as "n/a" for dryrun and real
    @pytest.mark.not_dryrun  the case needs a real effect; recorded as "n/a" for dryrun
  Test bodies must not call pytest.skip(); a gauge run (strict) fails any test that does.
* Strict mode (the gauge, GATES G3): FLIGHTCTL_CONFORMANCE_STRICT=1 and FLIGHTCTL_CONFORMANCE_PORTS=<a,b,...>.
  For every listed port: no registered implementation, a missing real twin, or ANY skip that is not a declared
  "n/a" is a FAILURE. Evidence files record passed / failed / n/a per case and strict=true; the gauge accepts
  only strict evidence with zero failures.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from . import registry

_RESULTS: dict[tuple[str, str], dict[str, str]] = {}


def strict_ports() -> set[str]:
    if os.environ.get("FLIGHTCTL_CONFORMANCE_STRICT") != "1":
        return set()
    return {p for p in os.environ.get("FLIGHTCTL_CONFORMANCE_PORTS", "").split(",") if p}


def pytest_configure(config: pytest.Config) -> None:
    for line in (
        "onlab: needs a real host named by FLIGHTCTL_CONFORMANCE_TARGET",
        "realtime: uses real time and real processes (no fake clock)",
        "port(name): the port this parametrised case exercises (evidence attribution)",
        "fake_only: applies to the fake only (fault injection); n/a for dryrun and real",
        "not_dryrun: needs a real effect; n/a for the dryrun twin",
        "strict_missing(port): strict run without a real twin; fails at setup",
    ):
        config.addinivalue_line("markers", line)


def port_params(port: str) -> list:
    """Parametrise a case over the registered implementations of `port`."""
    found = dict(registry.implementations(port))
    strict = port in strict_ports()
    if not found and not strict:
        return [pytest.param(None, None, marks=[pytest.mark.port(port), pytest.mark.skip(reason=f"no implementation of {port} registered yet (owed by {registry.OWED_BY.get(port, '?')})")], id="unregistered")]
    params = []
    for kind, factory in sorted(found.items()):
        marks = [pytest.mark.port(port)]
        if kind in {"real", "dryrun"}:
            marks.append(pytest.mark.onlab)
            if registry.target() is None:
                marks.append(pytest.mark.skip(reason=f"{port}/{kind}: set FLIGHTCTL_CONFORMANCE_TARGET to run on a real host"))
        params.append(pytest.param(kind, factory, marks=marks, id=f"{port}-{kind}"))
    if strict and "real" not in found:
        # round 3 (Sol 6 B7): a HARD failure at setup, never an expected failure (xfail would let the run pass)
        params.append(pytest.param("real", None, marks=[pytest.mark.port(port), pytest.mark.strict_missing(port)], id=f"{port}-missing-real"))
    return params


def pytest_runtest_setup(item: pytest.Item) -> None:
    missing = item.get_closest_marker("strict_missing")
    if missing is not None:
        pytest.fail(f"STRICT conformance: no real twin of {missing.args[0]} is registered", pytrace=False)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        callspec = getattr(item, "callspec", None)
        kind = callspec.params.get("kind") if callspec else None
        if kind is None:
            continue
        if item.get_closest_marker("fake_only") and kind != "fake":
            item.add_marker(pytest.mark.skip(reason="n/a: fake-only case"))
            item.user_properties.append(("applicability", "n/a"))
        elif item.get_closest_marker("not_dryrun") and kind == "dryrun":
            item.add_marker(pytest.mark.skip(reason="n/a: needs a real effect"))
            item.user_properties.append(("applicability", "n/a"))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    outcome = yield
    report = outcome.get_result()
    marker = item.get_closest_marker("port")
    callspec = getattr(item, "callspec", None)
    if marker is None or callspec is None:
        return
    if report.when != "call" and not (report.when == "setup" and (report.skipped or report.failed)):
        return
    port, kind = marker.args[0], callspec.params.get("kind")
    if kind is None:
        return
    declared_na = ("applicability", "n/a") in item.user_properties
    if report.skipped and declared_na:
        result = "n/a"
    elif report.skipped:
        result = "skipped"
        if port in strict_ports() and kind != "fake":
            report.outcome = "failed"
            report.longrepr = f"STRICT conformance: undeclared skip of {item.nodeid} for {port}/{kind}"
            result = "failed"
    else:
        result = report.outcome
    _RESULTS.setdefault((port, kind), {})[item.originalname] = result


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    evidence = os.environ.get("FLIGHTCTL_CONFORMANCE_EVIDENCE")
    host = registry.target()
    if not evidence or not host or not _RESULTS:
        return
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=False).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        commit = "unknown"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(evidence)
    out.mkdir(parents=True, exist_ok=True)
    strict = bool(strict_ports())
    for (port, kind), cases in sorted(_RESULTS.items()):
        record = {"port": port, "implementation": kind, "host": host, "commit": commit, "at": stamp, "strict": strict, "cases": cases,
                  "proven": strict and kind != "fake" and all(v in {"passed", "n/a"} for v in cases.values())}
        (out / f"conformance-{port}-{kind}-{host}-{stamp}.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
