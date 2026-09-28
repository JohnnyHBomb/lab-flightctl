"""Package-local fakes and neutral release fixtures for P6 scaffolding."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

from tests.contracts.validation import validate_instance
from tests.fakes.clock import FakeClock
from tests.fakes.gpu import FakeGPUProbe
from tests.fakes.ssh import FakeTransport
from tests.fakes.systemd import FakeSystemd


ROOT = Path(__file__).resolve().parents[2]
RELEASE_TOOL = ROOT / "deploy" / "flightctl-release"


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"


def digest(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def confirmed_inventory(*, revision: int = 1, enabled: bool = True, reachable: str = "confirmed") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "site_id": "site-a",
        "revision": revision,
        "stage": "confirmed",
        "controller": {
            "controller_id": "controller-a",
            "endpoint": "https://controller.site-a.invalid/v1/rpc",
            "account": "controller",
            "paths": [],
            "auth_state": "configured",
        },
        "timezone": "Etc/UTC",
        "identity_mapping": [],
        "chat_lane_order": ["lane-gpu0"] if enabled else [],
        "hosts": [
            {
                "host_id": "host-1",
                "ssh_endpoint": "host-1.example",
                "ssh_user": "runner",
                "reachability": reachable,
                "observed_at": "2026-09-28T10:00:00Z" if reachable == "confirmed" else None,
                "observation_error": None if reachable == "confirmed" else "probe unknown",
                "gpu_count": 1,
                "gpu_count_reason": None,
                "devices": [
                    {
                        "device_id": "gpu0",
                        "vendor": "nvidia",
                        "model": "Generic Accelerator",
                        "vram_bytes": 17179869184,
                        "driver": "550.1",
                        "unknown_reasons": {},
                    }
                ],
            }
        ],
        "lanes": [
            {
                "lane_id": "lane-gpu0",
                "host_id": "host-1",
                "device_ids": ["gpu0"],
                "class": "gpu",
                "enabled": enabled,
                "policy": {
                    "site_id": "site-a",
                    "capability_class": "gpu",
                    "capabilities": ["compute"],
                    "isolation": ["cgroup"],
                    "sanitisation": ["gpu-reset"],
                    "assurance": {
                        "required": [],
                        "offered": ["observed"],
                        "verified": [],
                        "evidence_refs": ["inventory-revision-1"],
                        "unknown": False,
                    },
                    "observed_at": "2026-09-28T10:00:00Z",
                    "provenance": ["operator-confirmed"],
                },
            }
        ],
    }


def policy(*, revision: int = 1) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "policy_id": "default",
        "revision": revision,
        "updated_at": "2026-09-28T10:00:00Z",
        "purpose_rules": ["purpose-required"],
        "content_rules": ["acceptable-use"],
        "admission": {
            "max_clock_skew_s": 30,
            "heartbeat_s": 60,
            "queue_refresh_s": 60,
            "queue_expiry_s": 600,
            "booking_horizon_s": 1209600,
            "minimum_booking_s": 900,
            "agent_max_s": 14400,
            "operator_max_s": 43200,
        },
        "security_hooks": {
            "supported_schemes": ["ssh-sk"],
            "unsupported_supplied_action": "deny",
        },
    }


def make_release(
    root: Path,
    release_id: str,
    *,
    inventory: Mapping[str, Any] | None = None,
    policy_document: Mapping[str, Any] | None = None,
    artifact_text: str | None = None,
    state_version: str = "state-v1",
    gate_order: list[str] | None = None,
) -> Path:
    bundle = root / release_id
    bundle.mkdir(parents=True, exist_ok=True)
    inventory_path = bundle / "inventory.json"
    policy_path = bundle / "policy.json"
    artifact_path = bundle / "packages" / "future_namespace" / "__init__.py"
    inventory_bytes = json_bytes(inventory or confirmed_inventory())
    policy_bytes = json_bytes(policy_document or policy())
    artifact_bytes = (artifact_text or f"release {release_id}\n").encode("utf-8")
    inventory_path.write_bytes(inventory_bytes)
    policy_path.write_bytes(policy_bytes)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(artifact_bytes)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "release_id": release_id,
        "release_version": release_id,
        "protocol": {"version": "rpc-v1", "sha256": "a" * 64},
        "state_compatibility": {"version": state_version, "sha256": "b" * 64},
        "inventory": {
            "path": "inventory.json",
            "revision": inventory_path.stat().st_size and (inventory or confirmed_inventory()).get("revision", 1),
            "sha256": digest(inventory_bytes),
            "confirmed": True,
        },
        "policy": {
            "path": "policy.json",
            "revision": policy_path.stat().st_size and (policy_document or policy()).get("revision", 1),
            "sha256": digest(policy_bytes),
            "confirmed": True,
        },
        "artifacts": [
            {
                "path": "packages/future_namespace/__init__.py",
                "version": release_id,
                "sha256": digest(artifact_bytes),
            }
        ],
        "rollout": {"version": 1, "gate_order": gate_order or ["protocol", "hash", "authentication", "local-deadline", "cleanup"]},
    }
    (bundle / "release.json").write_bytes(json_bytes(manifest))
    return bundle


def run_tool(state_dir: Path, command: str, release: str, *, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    process_env = os.environ.copy()
    process_env["FLIGHTCTL_RELEASE_STATE_DIR"] = str(state_dir)
    if env:
        process_env.update(env)
    return subprocess.run(
        [sys.executable, str(RELEASE_TOOL), command, release],
        cwd=ROOT,
        env=process_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=10,
    )


def state(state_dir: Path) -> dict[str, Any]:
    return json.loads((state_dir / "state.json").read_text(encoding="utf-8"))


class P0BoundaryFakes:
    """All P0 runtime seams are explicit and local to the integration harness."""

    def __init__(self) -> None:
        self.clock = FakeClock()
        self.transport = FakeTransport()
        self.systemd = FakeSystemd()
        self.gpu_probe = FakeGPUProbe()


class JsonBridgeFake:
    """Internal JSON bridge fake that validates and preserves the P0 envelope."""

    def __init__(self, boundaries: P0BoundaryFakes | None = None) -> None:
        injected = boundaries or P0BoundaryFakes()
        self.clock = injected.clock
        self.transport = injected.transport
        self.systemd = injected.systemd
        self.gpu_probe = injected.gpu_probe
        self.requests: list[dict[str, Any]] = []
        self.observations: list[dict[str, Any]] = []

    def request(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        request = dict(envelope)
        validate_instance(request, "rpc-envelope-v1.schema.json")
        self.requests.append(request)
        self.observations.append(
            {
                "clock": {
                    "utc": self.clock.utc(),
                    "monotonic": self.clock.monotonic(),
                    "boot_id": self.clock.boot_id(),
                },
                "transport": self.transport.request(
                    "configured-endpoint",
                    {"op": request["op"], "request_id": request["request_id"]},
                    timeout_s=1.0,
                ),
                "systemd": self.systemd.inspect("bridge-unit", request["request_id"]),
                "gpu": self.gpu_probe.inspect("bridge-host"),
            }
        )
        response = {
            "schema": 1,
            "request_id": request["request_id"],
            "status": 200,
            "data": {
                "kind": "status",
                "lane": None,
                "state": "unknown",
                "generation": None,
                "occupancy": {"certainty": "unknown", "reason": "no scripted occupancy"},
                "reachability": {"certainty": "unknown", "reason": "no scripted reachability"},
            },
            "error": None,
        }
        validate_instance(response, "rpc-envelope-v1.schema.json")
        return response
