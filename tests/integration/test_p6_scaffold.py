"""Named P6 behavioural checks.

These checks use the executable release tool and local state roots. They are
scaffold acceptance only; assembled mode intentionally fails when the real
package set has not yet been merged.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import stat
from pathlib import Path

import pytest

from deploy.flightctl_release import _manifest_hash, restore_backup
import deploy.check_portability as denylist_tool
from deploy.check_portability import scan_denylist
from tools.check_portability import scan_text

from .support import P0BoundaryFakes, JsonBridgeFake, confirmed_inventory, digest, json_bytes, make_release, run_tool, state


ASSEMBLED_SCENARIOS = (
    "one concurrent grant",
    "reserve-before-token/start",
    "replay without duplication",
    "fenced late cleanup",
    "approval tamper denial",
    "visible successors",
    "stream-safe eviction",
    "no reconnect reload",
    "retained unknown discovery denied admission",
)

ASSEMBLED_MUTATIONS = (
    "confirmation-bypass",
    "denylist-bypass",
    "reopen-after-failed-drain",
    "mixed-rollback-versions",
    "free-before-unload",
)

def _trace(state_dir: Path) -> list[str]:
    return state(state_dir).get("trace", [])


def _tracked_fixture(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "--quiet", str(root)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    for relative, contents in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return root


def test_stage_confirmation(tmp_path: Path) -> None:
    releases = tmp_path / "releases"
    bundle = make_release(releases, "r1")
    state_dir = tmp_path / "state"
    staged = run_tool(state_dir, "stage", str(bundle))
    assert staged.returncode == 0, staged.stderr
    manifest_path = state_dir / "staged" / "r1" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["artifacts"][0]["path"].endswith("__init__.py")
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o400
    assert _trace(state_dir) == []

    changed = json.loads((bundle / "inventory.json").read_text(encoding="utf-8"))
    changed["revision"] = 2
    (bundle / "inventory.json").write_text(json.dumps(changed), encoding="utf-8")
    refused = run_tool(state_dir, "stage", str(bundle))
    assert refused.returncode != 0
    assert _trace(state_dir) == []

    def rewrite_inventory(bundle_root: Path, document: dict[str, object]) -> None:
        inventory_path = bundle_root / "inventory.json"
        inventory_path.write_bytes(json_bytes(document))
        manifest_path = bundle_root / "release.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["inventory"]["revision"] = document["revision"]
        manifest["inventory"]["sha256"] = digest(inventory_path.read_bytes())
        manifest_path.write_bytes(json_bytes(manifest))

    draft_bundle = make_release(releases, "draft")
    draft_inventory = confirmed_inventory()
    draft_inventory["stage"] = "draft"
    rewrite_inventory(draft_bundle, draft_inventory)
    draft_state = tmp_path / "draft-state"
    assert run_tool(draft_state, "stage", str(draft_bundle)).returncode != 0
    assert _trace(draft_state) == []

    unknown_bundle = make_release(releases, "unknown")
    unknown_inventory = confirmed_inventory(reachable="unknown")
    rewrite_inventory(unknown_bundle, unknown_inventory)
    unknown_state = tmp_path / "unknown-state"
    assert run_tool(unknown_state, "stage", str(unknown_bundle)).returncode != 0
    assert _trace(unknown_state) == []

    unconfirmed_bundle = make_release(releases, "unconfirmed")
    unconfirmed_manifest_path = unconfirmed_bundle / "release.json"
    unconfirmed_manifest = json.loads(unconfirmed_manifest_path.read_text(encoding="utf-8"))
    del unconfirmed_manifest["inventory"]["confirmed"]
    unconfirmed_manifest_path.write_bytes(json_bytes(unconfirmed_manifest))
    unconfirmed_state = tmp_path / "unconfirmed-state"
    assert run_tool(unconfirmed_state, "stage", str(unconfirmed_bundle)).returncode != 0
    assert _trace(unconfirmed_state) == []

    unconfirmed_policy_bundle = make_release(releases, "unconfirmed-policy")
    unconfirmed_policy_manifest_path = unconfirmed_policy_bundle / "release.json"
    unconfirmed_policy_manifest = json.loads(unconfirmed_policy_manifest_path.read_text(encoding="utf-8"))
    unconfirmed_policy_manifest["policy"]["confirmed"] = "yes"
    unconfirmed_policy_manifest_path.write_bytes(json_bytes(unconfirmed_policy_manifest))
    unconfirmed_policy_state = tmp_path / "unconfirmed-policy-state"
    assert run_tool(unconfirmed_policy_state, "stage", str(unconfirmed_policy_bundle)).returncode != 0
    assert _trace(unconfirmed_policy_state) == []

    for label, change in (
        ("missing-controller-id", lambda document: document["controller"].pop("controller_id")),
        ("missing-identity-mapping", lambda document: document.pop("identity_mapping")),
        ("unexpected-inventory-field", lambda document: document.__setitem__("unexpected", True)),
    ):
        invalid_bundle = make_release(releases, label)
        invalid_document = confirmed_inventory()
        change(invalid_document)
        rewrite_inventory(invalid_bundle, invalid_document)
        invalid_state = tmp_path / f"{label}-state"
        assert run_tool(invalid_state, "stage", str(invalid_bundle)).returncode != 0
        assert _trace(invalid_state) == []

    missing_bundle = make_release(releases, "missing")
    (missing_bundle / "packages" / "future_namespace" / "__init__.py").unlink()
    missing_state = tmp_path / "missing-state"
    assert run_tool(missing_state, "stage", str(missing_bundle)).returncode != 0
    assert _trace(missing_state) == []

    tampered_bundle = make_release(releases, "tampered")
    tampered_state = tmp_path / "tampered-state"
    assert run_tool(tampered_state, "stage", str(tampered_bundle)).returncode == 0
    staged_artifact = tampered_state / "staged" / "tampered" / "artifacts" / "packages" / "future_namespace" / "__init__.py"
    staged_artifact.chmod(0o600)
    staged_artifact.write_text("tampered bytes\n", encoding="utf-8")
    staged_artifact.chmod(0o400)
    assert run_tool(tampered_state, "activate", "tampered").returncode != 0
    assert state(tampered_state)["admission"] == "closed"


def test_release_smoke(tmp_path: Path) -> None:
    bundle = make_release(tmp_path / "releases", "r1")
    state_dir = tmp_path / "state"
    assert run_tool(state_dir, "stage", str(bundle)).returncode == 0
    assert run_tool(state_dir, "activate", "r1").returncode == 0
    state_file = state_dir / "state.json"
    current = json.loads(state_file.read_text(encoding="utf-8"))
    current["smoke"] = {gate: "ok" for gate in ["protocol", "hash", "authentication", "local-deadline", "cleanup"]}
    state_file.write_text(json.dumps(current), encoding="utf-8")
    passed = run_tool(state_dir, "smoke", "r1")
    assert passed.returncode == 0, passed.stderr
    assert passed.stdout
    assert state(state_dir)["admission"] == "open"
    assert [item for item in _trace(state_dir) if item.startswith("smoke:")] == [
        "smoke:protocol",
        "smoke:hash",
        "smoke:authentication",
        "smoke:local-deadline",
        "smoke:cleanup",
    ]

    current = state(state_dir)
    current["smoke"]["authentication"] = "unknown"
    state_file.write_text(json.dumps(current), encoding="utf-8")
    refused = run_tool(state_dir, "smoke", "r1")
    assert refused.returncode != 0
    assert state(state_dir)["admission"] == "closed"

    current = state(state_dir)
    current["smoke"]["authentication"] = "ok"
    current["smoke"]["local-deadline"] = "timed-out"
    state_file.write_text(json.dumps(current), encoding="utf-8")
    timed_out = run_tool(state_dir, "smoke", "r1")
    assert timed_out.returncode != 0
    assert state(state_dir)["admission"] == "closed"

    current = state(state_dir)
    current["smoke"]["local-deadline"] = "ok"
    current["occupants"] = ["protected"]
    state_file.write_text(json.dumps(current), encoding="utf-8")
    protected = run_tool(state_dir, "smoke", "r1")
    assert protected.returncode != 0
    assert state(state_dir)["admission"] == "closed"
    trace = _trace(state_dir)
    last_inspect = max(index for index, item in enumerate(trace) if item == "inspect")
    assert "reopen" not in trace[last_inspect + 1 :]

    incomplete_bundle = make_release(tmp_path / "releases", "incomplete", gate_order=["cleanup"])
    incomplete_state = tmp_path / "incomplete-state"
    incomplete = run_tool(incomplete_state, "stage", str(incomplete_bundle))
    assert incomplete.returncode != 0
    assert _trace(incomplete_state) == []

    reordered_bundle = make_release(
        tmp_path / "releases",
        "reordered",
        gate_order=["cleanup", "local-deadline", "authentication", "hash", "protocol"],
    )
    reordered_state = tmp_path / "reordered-state"
    assert run_tool(reordered_state, "stage", str(reordered_bundle)).returncode == 0
    assert run_tool(reordered_state, "activate", "reordered").returncode == 0
    reordered_file = reordered_state / "state.json"
    reordered_runtime = state(reordered_state)
    reordered_runtime["smoke"] = {gate: "ok" for gate in ["cleanup", "local-deadline", "authentication", "hash", "protocol"]}
    reordered_file.write_text(json.dumps(reordered_runtime), encoding="utf-8")
    assert run_tool(reordered_state, "smoke", "reordered").returncode == 0
    assert [item for item in _trace(reordered_state) if item.startswith("smoke:")] == [
        "smoke:cleanup",
        "smoke:local-deadline",
        "smoke:authentication",
        "smoke:hash",
        "smoke:protocol",
    ]

    failed_drain_bundle = make_release(tmp_path / "releases", "failed-drain")
    failed_drain_state = tmp_path / "failed-drain-state"
    assert run_tool(failed_drain_state, "stage", str(failed_drain_bundle)).returncode == 0
    assert run_tool(failed_drain_state, "activate", "failed-drain").returncode == 0
    failed_drain_runtime = state(failed_drain_state)
    failed_drain_runtime["fail_step"] = "drain_workloads"
    failed_drain_runtime["smoke"] = {gate: "ok" for gate in ["protocol", "hash", "authentication", "local-deadline", "cleanup"]}
    (failed_drain_state / "state.json").write_text(json.dumps(failed_drain_runtime), encoding="utf-8")
    assert run_tool(failed_drain_state, "drain", "failed-drain").returncode != 0
    smoke_after_failed_drain = run_tool(failed_drain_state, "smoke", "failed-drain")
    assert smoke_after_failed_drain.returncode != 0
    assert state(failed_drain_state)["admission"] == "closed"


def test_activate_order(tmp_path: Path) -> None:
    bundle = make_release(tmp_path / "releases", "r1")
    state_dir = tmp_path / "state"
    assert run_tool(state_dir, "stage", str(bundle)).returncode == 0
    activated = run_tool(state_dir, "activate", "r1")
    assert activated.returncode == 0, activated.stderr
    assert _trace(state_dir) == [
        "pause",
        "drain_workloads",
        "drain_chat",
        "inspect",
        "snapshot",
        "disable_legacy",
        "install_executors",
        "install_authority",
        "install_clients",
        "install_adapters",
        "install_watcher",
        "verify",
        "reopen",
    ]
    assert state(state_dir)["admission"] == "open"

    for failing_step in [
        "pause",
        "drain_workloads",
        "drain_chat",
        "inspect",
        "snapshot",
        "disable_legacy",
        "install_executors",
        "install_authority",
        "install_clients",
        "install_adapters",
        "install_watcher",
        "verify",
        "reopen",
    ]:
        failing_state = tmp_path / f"failure-{failing_step}"
        assert run_tool(failing_state, "stage", str(bundle)).returncode == 0
        current = state(failing_state)
        current["fail_step"] = failing_step
        (failing_state / "state.json").write_text(json.dumps(current), encoding="utf-8")
        failed = run_tool(failing_state, "activate", "r1")
        assert failed.returncode != 0
        result = state(failing_state)
        assert result["admission"] == "closed"
        trace = result["trace"]
        if failing_step in trace:
            assert "reopen" not in trace[trace.index(failing_step) + 1 :]
            assert not set(trace[trace.index(failing_step) + 1 :]).intersection(
                {
                    "snapshot",
                    "disable_legacy",
                    "install_executors",
                    "install_authority",
                    "install_clients",
                    "install_adapters",
                    "install_watcher",
                    "verify",
                    "reopen",
                }
            )

    occupied = tmp_path / "occupied"
    assert run_tool(occupied, "stage", str(bundle)).returncode == 0
    current = state(occupied)
    current["legacy_occupants"] = ["legacy-unit"]
    (occupied / "state.json").write_text(json.dumps(current), encoding="utf-8")
    refused = run_tool(occupied, "activate", "r1")
    assert refused.returncode != 0
    result = state(occupied)
    assert result["admission"] == "closed"
    assert result["quarantined"] is True
    assert "legacy-unit" in result["legacy_occupants"]

    for label, value in (("missing", "missing"), ("null", None), ("non-list", "unknown")):
        unknown_state = tmp_path / f"unknown-occupancy-{label}"
        assert run_tool(unknown_state, "stage", str(bundle)).returncode == 0
        unknown_runtime = state(unknown_state)
        if value == "missing":
            unknown_runtime.pop("occupants")
        else:
            unknown_runtime["occupants"] = value
        (unknown_state / "state.json").write_text(json.dumps(unknown_runtime), encoding="utf-8")
        refused = run_tool(unknown_state, "activate", "r1")
        assert refused.returncode != 0
        assert state(unknown_state)["admission"] == "closed"
        assert state(unknown_state)["quarantined"] is True


def test_matched_rollback_restore(tmp_path: Path) -> None:
    releases = tmp_path / "releases"
    r1 = make_release(releases, "r1")
    r2 = make_release(releases, "r2", artifact_text="newer\n")
    state_dir = tmp_path / "state"
    backup_dir = tmp_path / "operator-backups"
    env = {"FLIGHTCTL_BACKUP_DIR": str(backup_dir)}
    assert run_tool(state_dir, "stage", str(r1), env=env).returncode == 0
    assert run_tool(state_dir, "stage", str(r2), env=env).returncode == 0
    assert run_tool(state_dir, "activate", "r1", env=env).returncode == 0
    assert run_tool(state_dir, "activate", "r2", env=env).returncode == 0
    current = state(state_dir)
    current["newer_state"] = {"counter": 2}
    (state_dir / "state.json").write_text(json.dumps(current), encoding="utf-8")
    rolled = run_tool(state_dir, "rollback", "r1", env=env)
    assert rolled.returncode == 0, rolled.stderr
    assert state(state_dir)["current_release"] == "r1"
    assert state(state_dir)["admission"] == "open"
    assert state(state_dir)["newer_state"] == {"counter": 2}
    backup = next(backup_dir.glob("before-r2*"))
    restored = restore_backup(backup, tmp_path / "fresh-restore")
    assert {"state.json", "inventory.json", "policy.json", "release.json"} <= set(restored["files"])
    assert any(item.startswith("artifacts/") for item in restored["files"])
    restored_root = tmp_path / "fresh-restore"
    files = [path for path in restored_root.rglob("*") if path.is_file()]
    directories = [path for path in restored_root.rglob("*") if path.is_dir()]
    assert files and all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files)
    assert directories and all(stat.S_IMODE(path.stat().st_mode) == 0o700 for path in directories)
    rehearsal_state = tmp_path / "rehearsal-state"
    assert run_tool(rehearsal_state, "stage", str(tmp_path / "fresh-restore")).returncode == 0

    occupied_state = tmp_path / "occupied-rollback"
    assert run_tool(occupied_state, "stage", str(r1), env=env).returncode == 0
    assert run_tool(occupied_state, "stage", str(r2), env=env).returncode == 0
    assert run_tool(occupied_state, "activate", "r1", env=env).returncode == 0
    assert run_tool(occupied_state, "activate", "r2", env=env).returncode == 0
    occupied_runtime = state(occupied_state)
    occupied_runtime["occupants"] = ["protected-workload"]
    (occupied_state / "state.json").write_text(json.dumps(occupied_runtime), encoding="utf-8")
    assert run_tool(occupied_state, "rollback", "r1", env=env).returncode != 0
    assert state(occupied_state)["current_release"] == "r2"
    assert state(occupied_state)["admission"] == "closed"

    incompatible_state = tmp_path / "incompatible-rollback"
    assert run_tool(incompatible_state, "stage", str(r1), env=env).returncode == 0
    assert run_tool(incompatible_state, "stage", str(r2), env=env).returncode == 0
    assert run_tool(incompatible_state, "activate", "r1", env=env).returncode == 0
    assert run_tool(incompatible_state, "activate", "r2", env=env).returncode == 0
    snapshot_path = incompatible_state / "snapshots" / "r2.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["restore_state_compatibility"] = {"version": "incompatible", "sha256": "c" * 64}
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    assert run_tool(incompatible_state, "rollback", "r1", env=env).returncode != 0
    assert state(incompatible_state)["current_release"] == "r2"
    assert state(incompatible_state)["admission"] == "closed"

    newer_format_r1 = make_release(releases, "r1-state-v1", state_version="state-v1")
    newer_format_r2 = make_release(releases, "r2-state-v2", state_version="state-v2")
    newer_format_state = tmp_path / "newer-format-rollback"
    assert run_tool(newer_format_state, "stage", str(newer_format_r1), env=env).returncode == 0
    assert run_tool(newer_format_state, "stage", str(newer_format_r2), env=env).returncode == 0
    assert run_tool(newer_format_state, "activate", "r1-state-v1", env=env).returncode == 0
    assert run_tool(newer_format_state, "activate", "r2-state-v2", env=env).returncode == 0
    refused_newer_format = run_tool(newer_format_state, "rollback", "r1-state-v1", env=env)
    assert refused_newer_format.returncode != 0
    assert state(newer_format_state)["current_release"] == "r2-state-v2"
    assert state(newer_format_state)["current_state_compatibility"]["version"] == "state-v2"
    assert state(newer_format_state)["admission"] == "closed"

    hardware_state = tmp_path / "changed-hardware"
    assert run_tool(hardware_state, "stage", str(r1), env=env).returncode == 0
    assert run_tool(hardware_state, "stage", str(r2), env=env).returncode == 0
    hardware_runtime = state(hardware_state)
    hardware_runtime["hardware_hash"] = "hardware-a"
    (hardware_state / "state.json").write_text(json.dumps(hardware_runtime), encoding="utf-8")
    assert run_tool(hardware_state, "activate", "r1", env=env).returncode == 0
    assert run_tool(hardware_state, "activate", "r2", env=env).returncode == 0
    hardware_runtime = state(hardware_state)
    hardware_runtime["hardware_hash"] = "hardware-b"
    (hardware_state / "state.json").write_text(json.dumps(hardware_runtime), encoding="utf-8")
    assert run_tool(hardware_state, "rollback", "r1", env=env).returncode != 0
    assert state(hardware_state)["current_release"] == "r2"
    assert state(hardware_state)["admission"] == "closed"

    live_inventory = tmp_path / "live-inventory.json"
    live_inventory.write_bytes((r1 / "inventory.json").read_bytes())
    live_env = {**env, "FLIGHTCTL_INVENTORY": str(live_inventory)}
    changed_inventory_state = tmp_path / "changed-inventory"
    assert run_tool(changed_inventory_state, "stage", str(r1), env=live_env).returncode == 0
    assert run_tool(changed_inventory_state, "stage", str(r2), env=live_env).returncode == 0
    assert run_tool(changed_inventory_state, "activate", "r1", env=live_env).returncode == 0
    assert run_tool(changed_inventory_state, "activate", "r2", env=live_env).returncode == 0
    live_document = json.loads(live_inventory.read_text(encoding="utf-8"))
    live_document["revision"] = 8
    live_inventory.write_text(json.dumps(live_document), encoding="utf-8")
    assert run_tool(changed_inventory_state, "rollback", "r1", env=live_env).returncode != 0
    assert state(changed_inventory_state)["current_release"] == "r2"
    assert state(changed_inventory_state)["admission"] == "closed"

    changed = json.loads((r1 / "inventory.json").read_text(encoding="utf-8"))
    changed["revision"] = 4
    (r1 / "inventory.json").write_text(json.dumps(changed), encoding="utf-8")
    assert run_tool(state_dir, "rollback", "r1", env=env).returncode != 0
    assert state(state_dir)["current_release"] == "r1"


def test_external_denylist(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    clean_root = _tracked_fixture(
        tmp_path / "clean-repo",
        {"README.md": "portable\n", "config/inventory.json.example": "example\n"},
    )
    denylist = tmp_path / "denylist.txt"
    denylist.write_text("synthetic-forbidden-value\n", encoding="utf-8")
    assert scan_denylist(clean_root, denylist) is True

    forbidden = "synthetic-forbidden-marker-7f3e"
    forbidden_root = _tracked_fixture(
        tmp_path / "forbidden-repo",
        {"README.md": "clean\n", "config/inventory.json.example": forbidden + "\n"},
    )
    denylist.write_text(f"{forbidden}\n", encoding="utf-8")
    assert scan_denylist(forbidden_root, denylist) is False

    monkeypatch.setattr(denylist_tool, "_portable_scan", lambda root: 0)
    assert denylist_tool.main(["--denylist", str(denylist), str(clean_root)]) == 0
    clean_output = capsys.readouterr()
    assert forbidden not in clean_output.out
    assert forbidden not in clean_output.err
    assert denylist_tool.main(["--denylist", str(denylist), str(forbidden_root)]) == 1
    captured = capsys.readouterr()
    assert forbidden not in captured.out
    assert forbidden not in captured.err

    with pytest.raises(ValueError):
        scan_denylist(clean_root, tmp_path / "absent-denylist")
    (tmp_path / "unreadable-denylist").mkdir()
    with pytest.raises(ValueError):
        scan_denylist(clean_root, tmp_path / "unreadable-denylist")

    denylist.write_text("\n# comment\n", encoding="utf-8")
    with pytest.raises(ValueError):
        scan_denylist(clean_root, denylist)

    address = lambda parts: ".".join(str(part) for part in parts)
    outside_low = address((100, 63, 255, 255))
    outside_high = address((100, 128, 0, 0))
    shared_first = address((100, 64, 0, 0))
    shared_last = address((100, 127, 255, 255))
    assert scan_text(f"address={outside_low} {outside_high}", "README.md") == []
    assert scan_text(f"address={shared_first} {shared_last}", "README.md")
    assert scan_text(f"address={shared_first} {shared_last}", "config/inventory.json.example") == []


def test_ci_security_configuration() -> None:
    workflow = (Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    active_lines = [line.strip() for line in workflow.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert "cancel-in-progress: true" in workflow
    assert "timeout-minutes: 10" in workflow
    for tool in ("gitleaks", "pip-audit", "semgrep", "shellcheck"):
        assert tool in workflow
    assert "FLIGHTCTL_DENYLIST_CONTENT" in workflow
    assert "mktemp" in workflow and "chmod 600" in workflow and "printf" in workflow
    assert "test -s" in workflow
    assert "--no-deps" in workflow
    assert "python -m pip check" in active_lines
    assert "gitleaks version" in workflow
    assert "shellcheck --version" in workflow
    assert "semgrep --version" in workflow
    assert "pip-audit --version" in workflow
    assert "deploy/ci-tools.lock" in workflow
    assert any(line.startswith("- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1") for line in active_lines)
    assert any(line.startswith("- uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97") for line in active_lines)
    for command in (
        "run: shellcheck -x deploy/pre-commit-denylist deploy/provision-ci-tools",
        "run: gitleaks detect --source . --no-banner --redact --exit-code 1",
        "run: pip-audit --strict --no-deps -r deploy/requirements-ci.lock",
        "run: semgrep scan --error --config p/python flightctl deploy tests",
    ):
        assert command in active_lines
    for command in ("deploy/provision-ci-tools", "gitleaks version", "shellcheck --version", "semgrep --version", "pip-audit --version"):
        assert command in active_lines
    assert 'printf \'%s\\n\' "$FLIGHTCTL_DENYLIST_CONTENT" > "$denylist"' in active_lines
    assert not any("--denylist \"$FLIGHTCTL_DENYLIST\"" in line for line in active_lines)
    assert "CodeQL" not in workflow
    dependabot = (Path(__file__).resolve().parents[2] / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    assert "github-actions" in dependabot and "pip" in dependabot
    requirements = (Path(__file__).resolve().parents[2] / "deploy" / "requirements-ci.lock").read_text(encoding="utf-8")
    pinned_requirements = [line for line in requirements.splitlines() if line and not line.startswith("#")]
    assert pinned_requirements and all("==" in line and " " not in line for line in pinned_requirements)
    pinned_names = {line.split("==", 1)[0].lower().replace("_", "-") for line in pinned_requirements}  # PEP 503 names
    # Every CI tool is pinned, and the lock carries its transitive closure (verified in CI by `pip check`).
    assert {"semgrep", "pip-audit", "pytest", "jsonschema", "referencing"} <= pinned_names
    assert len(pinned_names) > 20 and "rpds-py" in pinned_names  # transitive (jsonschema/referencing) pinned too
    tool_lock = (Path(__file__).resolve().parents[2] / "deploy" / "ci-tools.lock").read_text(encoding="utf-8")
    assert "GITLEAKS_VERSION=" in tool_lock and "GITLEAKS_IMAGE=" in tool_lock
    assert re.search(r"@sha256:[0-9a-f]{64}", tool_lock)
    assert re.search(r"SHELLCHECK_SHA256=[0-9a-f]{64}", tool_lock)
    provisioner = Path(__file__).resolve().parents[2] / "deploy" / "provision-ci-tools"
    assert provisioner.is_file() and os.access(provisioner, os.X_OK)
    parser_workflow = (Path(__file__).resolve().parents[2] / ".github" / "workflows" / "parser-fuzz.yml").read_text(encoding="utf-8")
    parser_active_lines = [line.strip() for line in parser_workflow.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert "test_parser_properties.py" in parser_workflow
    assert "--no-deps" in parser_workflow
    assert "python -m pip check" in parser_active_lines
    assert "run: python -m pytest -q tests/integration/test_parser_properties.py" in parser_active_lines
    assert any(line.startswith("- uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1") for line in parser_active_lines)
    assert any(line.startswith("- uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97") for line in parser_active_lines)


def test_p0_injected_boundaries() -> None:
    fakes = P0BoundaryFakes()
    assert {"clock", "transport", "systemd", "gpu_probe"} <= set(vars(fakes))
    request = json.loads((Path(__file__).resolve().parents[2] / "contracts" / "rpc-envelope-v1.schema.json").read_text(encoding="utf-8"))["x-examples"]["valid"][0]
    bridge = JsonBridgeFake(fakes)
    assert bridge.clock is fakes.clock
    assert bridge.transport is fakes.transport
    assert bridge.systemd is fakes.systemd
    assert bridge.gpu_probe is fakes.gpu_probe
    response = bridge.request(request)
    assert {"generation", "lane", "occupancy", "reachability", "state"} <= set(response["data"])
    assert bridge.observations
    assert fakes.clock.calls
    assert fakes.transport.calls
    assert fakes.systemd.calls
    assert fakes.gpu_probe.calls


def test_assembled_scenarios() -> None:
    phase = os.environ.get("FLIGHTCTL_INTEGRATION_PHASE", "scaffold")
    if phase not in {"scaffold", "assembled"}:
        pytest.fail(f"unknown integration phase: {phase}")
    if phase == "scaffold":
        pytest.skip("assembled scenarios unexecuted in scaffold phase; real P1-P5/P7 packages are required")
    required = ["flightctl.authority", "flightctl.auth", "flightctl.store", "flightctl.executor", "flightctl.client", "flightctl.discovery"]
    required_paths = ["roster/run_arm.sh"]
    missing = []
    for name in required:
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except ModuleNotFoundError:
            missing.append(name)
    empty = []
    root = Path(__file__).resolve().parents[2]
    for name in required_paths:
        path = root / name
        if not path.is_file():
            missing.append(name)
        elif path.stat().st_size == 0:
            empty.append(name)
        elif not os.access(path, os.X_OK):
            empty.append(f"{name} (not executable)")
    for name in required:
        if name in missing:
            continue
        module = __import__(name, fromlist=["*"])
        if not any(not key.startswith("_") and callable(value) for key, value in vars(module).items()):
            empty.append(name)

    # P0-only scaffolding cannot certify behavior from adapter booleans or
    # seam call counts. Replace this refusal only with P6-owned assertions
    # against the assembled packages and actual implementation mutations.
    pytest.fail(
        "assembled acceptance incomplete; missing implementations: "
        + (", ".join(missing + [f"{name} (empty)" for name in empty]) or "none detected")
        + f"; scenarios not executed: {', '.join(ASSEMBLED_SCENARIOS)}"
        + f"; assembled mutations not executed: {', '.join(ASSEMBLED_MUTATIONS)}"
    )


def test_integration_mutations(tmp_path: Path) -> None:
    bundle = make_release(tmp_path / "releases", "r1")
    for mutation in (
        "drain_workloads",
        "drain_chat",
        "inspect",
        "install_executors",
        "install_authority",
        "install_clients",
        "install_adapters",
        "install_watcher",
        "verify",
    ):
        state_dir = tmp_path / mutation
        assert run_tool(state_dir, "stage", str(bundle)).returncode == 0
        current = state(state_dir)
        current["fail_step"] = mutation
        (state_dir / "state.json").write_text(json.dumps(current), encoding="utf-8")
        failed = run_tool(state_dir, "activate", "r1")
        assert failed.returncode != 0
        assert state(state_dir)["admission"] == "closed"

    bypass_bundle = make_release(tmp_path / "releases", "bypass")
    bypass_state = tmp_path / "bypass-state"
    assert run_tool(bypass_state, "stage", str(bypass_bundle)).returncode == 0
    staged_manifest_path = bypass_state / "staged" / "bypass" / "manifest.json"
    staged_manifest = json.loads(staged_manifest_path.read_text(encoding="utf-8"))
    staged_manifest["inventory"]["confirmed"] = False
    staged_manifest["manifest_sha256"] = _manifest_hash(staged_manifest)
    staged_manifest_path.chmod(0o600)
    staged_manifest_path.write_text(json.dumps(staged_manifest), encoding="utf-8")
    staged_manifest_path.chmod(0o400)
    assert run_tool(bypass_state, "activate", "bypass").returncode != 0
    assert state(bypass_state)["admission"] == "closed"

    bridge = JsonBridgeFake()
    response = bridge.request(
        json.loads((Path(__file__).resolve().parents[2] / "contracts" / "rpc-envelope-v1.schema.json").read_text(encoding="utf-8"))["x-examples"]["valid"][0]
    )
    assert response["request_id"] == "req-a"
    assert response["schema"] == 1
