"""Regression evidence for release and assembled acceptance boundaries."""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

from deploy.flightctl_release import FileReleaseBackend, ReleaseFailure, ReleaseManager, ReleasePaths

from . import test_p6_scaffold as scaffold
from .support import make_release


def test_assembled_rejects_verdict_only_adapter(monkeypatch) -> None:
    """Calls and boolean self-reports are not nine behavioral assertions."""
    parent = types.ModuleType("flightctl")
    parent.__path__ = []
    monkeypatch.setitem(sys.modules, "flightctl", parent)
    for name in ("authority", "auth", "store", "executor", "client", "discovery"):
        module = types.ModuleType(f"flightctl.{name}")
        module.stub = lambda: None
        module.__spec__ = importlib.util.spec_from_loader(module.__name__, loader=None)
        monkeypatch.setitem(sys.modules, module.__name__, module)

    class VerdictOnly:
        def __init__(self, **seams):
            self.seams = seams

        def run_scenario(self, name):
            for seam in self.seams.values():
                seam.calls.append({"pretend": name})
            return True

        run_mutation = run_scenario

    adapter = types.ModuleType("flightctl.integration")
    adapter.build_p6_acceptance = VerdictOnly
    monkeypatch.setitem(sys.modules, adapter.__name__, adapter)
    monkeypatch.setenv("FLIGHTCTL_INTEGRATION_PHASE", "assembled")
    with monkeypatch.context() as paths:
        paths.setattr(Path, "is_file", lambda self: True)
        paths.setattr(Path, "stat", lambda self: types.SimpleNamespace(st_size=1))
        paths.setattr(os, "access", lambda *args: True)
        with pytest.raises(pytest.fail.Exception, match="assembled acceptance incomplete"):
            scaffold.test_assembled_scenarios()


def test_unknown_integration_phase_refuses(monkeypatch) -> None:
    monkeypatch.setenv("FLIGHTCTL_INTEGRATION_PHASE", "assembeld")
    with pytest.raises(pytest.fail.Exception, match="unknown integration phase"):
        scaffold.test_assembled_scenarios()


@pytest.mark.parametrize("operation", ["activate", "drain", "smoke", "rollback"])
@pytest.mark.parametrize("observation", [None, {}, {"empty": False}, {"empty": True},
                                        {"empty": 1, "occupants": []},
                                        {"empty": True, "occupants": ["protected"]}])
def test_release_requires_explicit_empty_observation(tmp_path, monkeypatch, operation, observation) -> None:
    paths = ReleasePaths(tmp_path / "state")
    backend = FileReleaseBackend(paths.state_dir)
    manager = ReleaseManager(paths, backend)
    manager.stage(str(make_release(tmp_path / "releases", "r1")))
    if operation != "activate":
        manager.activate("r1")
    if operation == "smoke":
        backend._state["smoke"] = {gate: "ok" for gate in
                                   ("protocol", "hash", "authentication", "local-deadline", "cleanup")}
    if operation == "rollback":
        manager.stage(str(make_release(tmp_path / "releases", "r2")))
        manager.activate("r2")
    before = len(backend.get_state()["trace"])
    monkeypatch.setattr(backend, "inspect_empty", lambda release: observation)
    with pytest.raises(ReleaseFailure, match="occupancy"):
        getattr(manager, operation)("r1")
    result = backend.get_state()
    assert result["admission"] == "closed"
    assert "reopen" not in result["trace"][before:]
    if operation == "rollback":
        assert result["current_release"] == "r2"
