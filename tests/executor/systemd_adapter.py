"""Package-local Systemd adapter for executor tests.

The P0 fake is intentionally frozen and has process-wide occupancy on each
instance plus a permissive unknown-outcome default.  This adapter gives every
unit/invocation pair its own frozen instance and translates any script token
outside the explicit vocabulary into a failed, fail-closed result.  It is a
test seam; production code never imports it.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Mapping

from tests.fakes.systemd import FakeSystemd


_KNOWN_OUTCOMES = {"success", "failed_stop", "timeout", "unknown", "lost", "invocation_mismatch"}


class IsolatedSystemd:
    """Route each exact unit/invocation to a separate frozen fake instance."""

    def __init__(self) -> None:
        self.instances: dict[tuple[str, str], FakeSystemd] = {}
        self.scripts: dict[tuple[str, str, str], deque[dict[str, object]]] = defaultdict(deque)
        self.calls: list[dict[str, object]] = []
        self.on_stop = None

    def instance(
        self,
        unit: str,
        invocation: str,
        *,
        occupants: list[str] | None = None,
        gpu_occupants: list[str] | None = None,
    ) -> FakeSystemd:
        key = (unit, invocation)
        if key not in self.instances:
            self.instances[key] = FakeSystemd(occupants=occupants, gpu_occupants=gpu_occupants)
        return self.instances[key]

    def queue(self, unit: str, invocation: str, method: str, outcome: str, **payload: object) -> None:
        self.instance(unit, invocation)
        self.scripts[(unit, invocation, method)].append({"outcome": outcome, **payload})

    def _call(self, method: str, unit: str, invocation: str) -> Mapping[str, object]:
        self.calls.append({"method": method, "unit": unit, "invocation": invocation})
        fake = self.instance(unit, invocation)
        scripted = self.scripts[(unit, invocation, method)].popleft() if self.scripts[(unit, invocation, method)] else None
        if scripted is not None:
            outcome = str(scripted.get("outcome", "unknown"))
            if outcome not in _KNOWN_OUTCOMES:
                # The frozen fake would treat this as success; the adapter
                # refuses to turn an unrecognised script into cleanup.
                return self._failure(unit, invocation, "unknown", fake, scripted)
            if outcome != "success":
                fake.queue(outcome, **{key: value for key, value in scripted.items() if key != "outcome"})
        result = dict(getattr(fake, method)(unit, invocation))
        if scripted is not None:
            for key in ("state", "active", "complete"):
                if key in scripted:
                    result[key] = scripted[key]
            for key in ("cgroup_occupants", "gpu_occupants", "gpu_tenants"):
                if key in scripted:
                    result[key] = scripted[key]
        return result

    @staticmethod
    def _failure(unit: str, invocation: str, status: str, fake: FakeSystemd, payload: Mapping[str, object]) -> dict[str, object]:
        return {"ok": False, "status": status, "unit": unit, "invocation": invocation, "occupants": list(fake.occupants), "cgroup_occupants": list(fake.occupants), "gpu_occupants": list(fake.gpu_occupants), **{key: value for key, value in payload.items() if key != "outcome"}}

    def start(self, unit: str, invocation: str) -> Mapping[str, object]:
        return self._call("start", unit, invocation)

    def stop(self, unit: str, invocation: str) -> Mapping[str, object]:
        if self.on_stop is not None:
            self.on_stop(unit, invocation)
        return self._call("stop", unit, invocation)

    def inspect(self, unit: str, invocation: str) -> Mapping[str, object]:
        return self._call("inspect", unit, invocation)
