"""Run safety assertions against clean and temporarily mutated product code."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from .support import make_release


ROOT = Path(__file__).resolve().parents[2]
MUTATIONS = {
    "confirmation-bypass": (
        "deploy/flightctl_release.py",
        'confirmed = metadata.get("confirmed")',
        'confirmed = metadata.get("confirmed", True)',
        1,
    ),
    "denylist-bypass": (
        "deploy/check_portability.py",
        "            return False",
        "            return True",
        1,
    ),
    "reopen-after-failed-drain": (
        "deploy/flightctl_release.py",
        'if state.get("admission") != "open":',
        "if False:",
        1,
    ),
    "mixed-rollback-versions": (
        "deploy/flightctl_release.py",
        '            or running_state_compatibility != manifest.get("state_compatibility")',
        "",
        1,
    ),
    "omit-chat-drain": (
        "deploy/flightctl_release.py",
        "            self.backend.drain_chat(release_id)",
        "            pass  # mutated chat drain",
        2,
    ),
}


def _load_product(monkeypatch, relative, old, new, count, mutated):
    path = ROOT / relative
    source = path.read_text(encoding="utf-8")
    assert source.count(old) == count, "mutation no longer matches the implementation"
    if mutated:
        source = source.replace(old, new)
    module = types.ModuleType("_p6_mutation_product")
    # Retain the product schema root. Only the module's code is mutated;
    # validation still reads the real frozen contracts, with no bypass.
    module.__file__ = str(path)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


def _check_safety(module, name, root, monkeypatch):
    root.mkdir()
    if name == "denylist-bypass":
        document = root / "tracked.txt"
        document.write_text("forbidden-fixture-entry", encoding="utf-8")
        denylist = root / "denylist.txt"
        denylist.write_text(document.read_text(encoding="utf-8"), encoding="utf-8")
        # Only git enumeration is a peripheral fake; exercise the real scanner.
        monkeypatch.setattr(module, "_tracked_files", lambda _: [document])
        assert module.scan_denylist(root, denylist) is False, "unsafe mutation survived"
        return

    manager = module.ReleaseManager(module.ReleasePaths(root / "state"))
    bundle = make_release(root / "releases", "r1")
    if name == "confirmation-bypass":
        manifest_path = bundle / "release.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["inventory"].pop("confirmed")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        operation = lambda: manager.stage(str(bundle))
    else:
        manager.stage(str(bundle))
        if name == "omit-chat-drain":
            manager.backend._state["fail_step"] = "drain_chat"
            operation = lambda: manager.activate("r1")
        else:
            manager.activate("r1")
            if name == "reopen-after-failed-drain":
                manager.backend._state["fail_step"] = "drain_workloads"
                with pytest.raises(module.ReleaseFailure, match="drain_workloads"):
                    manager.drain("r1")
                manager.backend._state["smoke"] = {gate: "ok" for gate in module.REQUIRED_SMOKE_GATES}
                operation = lambda: manager.smoke("r1")
            else:
                newer = make_release(root / "releases", "r2", state_version="state-v2")
                manager.stage(str(newer))
                manager.activate("r2")
                operation = lambda: manager.rollback("r1")
    try:
        operation()
    except module.ReleaseFailure:
        pass
    else:
        raise AssertionError("unsafe mutation survived")
    state = manager.backend.get_state()
    assert state["admission"] == "closed"
    if name == "mixed-rollback-versions":
        assert state["current_release"] == "r2"
        assert state["current_state_compatibility"]["version"] == "state-v2"


@pytest.mark.parametrize("name", MUTATIONS)
def test_named_implementation_mutation_is_detected(tmp_path, monkeypatch, name):
    """The same assertion passes clean code and fails its named mutant."""
    for mutated in (False, True):
        with monkeypatch.context() as patch:
            module = _load_product(patch, *MUTATIONS[name], mutated)
            root = tmp_path / ("mutant" if mutated else "clean")
            if mutated:
                with pytest.raises(AssertionError, match="unsafe mutation survived"):
                    _check_safety(module, name, root, patch)
            else:
                _check_safety(module, name, root, patch)
