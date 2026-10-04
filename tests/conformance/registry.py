"""Conformance implementation registry (skeleton).

Each slice that delivers an implementation of a port registers a factory here, e.g.

    register("workload_runner", "fake", lambda target: FakeRunner())
    register("workload_runner", "real", lambda target: SystemdUserRunner(LocalCommandRunner()))

'fake' factories always run. 'real' and 'dryrun' factories run only when FLIGHTCTL_CONFORMANCE_TARGET
names a host (pytest marker 'onlab'); hosted CI and lab-ci containers skip them with a visible reason,
and the gauge runs them on the named host (GATES.txt G3). A port with no registered implementation is
skipped with the slice that owes it, never silently passed.

Amendment 2 (Sol 6.1 cold review): implementations also expose the explicit identities the ports now take:
test_lane (workload_runner, session_gateway), test_parent_lease (session_gateway) and test_noise_allowlist
(occupancy_probe: the lane's approved desktop identities, [] when the target lane has none).
"""

from __future__ import annotations

import os
from typing import Any, Callable

Factory = Callable[[str | None], Any]
_REGISTRY: dict[str, dict[str, Factory]] = {}

OWED_BY = {
    "clock": "A1", "command_runner": "A2", "executor_transport": "A4", "workload_runner": "A6",
    "inventory_probe": "A3i", "occupancy_probe": "A3", "inhibitor": "A11", "waker": "A12", "peer_identity": "A7",
    "health_probe": "C8", "signer": "C3", "model_cache": "C6a", "session_gateway": "C9w", "notifier": "C10",
    "legacy_observer": "A9", "release_backend": "B5",
}


def register(port: str, kind: str, factory: Factory) -> None:
    if kind not in {"fake", "dryrun", "real"}:
        raise ValueError(kind)
    _REGISTRY.setdefault(port, {})[kind] = factory


def implementations(port: str) -> list[tuple[str, Factory]]:
    return sorted(_REGISTRY.get(port, {}).items())


def target() -> str | None:
    return os.environ.get("FLIGHTCTL_CONFORMANCE_TARGET") or None
