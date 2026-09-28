"""Package-local test adapters for unit-isolated cleanup evidence."""

from __future__ import annotations

from collections import defaultdict
from typing import Mapping

from tests.fakes import FakeSystemd


_KNOWN_OUTCOMES = frozenset({"success", "invocation_mismatch", "failed_stop", "timeout", "unknown", "lost"})


class UnitSystemdAdapter:
    """Give each unit its own P0 fake instance and preserve unknown outcomes."""

    def __init__(self) -> None:
        self._units: dict[str, FakeSystemd] = defaultdict(FakeSystemd)
        self.calls: list[dict[str, object]] = []

    def queue(self, unit: str, outcome: str, **payload: object) -> None:
        if outcome not in _KNOWN_OUTCOMES:
            raise ValueError(f"unknown scripted systemd outcome: {outcome}")
        self._units[unit].queue(outcome, **payload)

    def set_occupancy(self, unit: str, *, cgroup: list[str] | None = None, gpu: list[str] | None = None) -> None:
        fake = self._units[unit]
        fake.occupants[:] = list(cgroup or [])
        fake.gpu_occupants[:] = list(gpu or [])

    @property
    def started(self) -> set[tuple[str, str]]:
        result: set[tuple[str, str]] = set()
        for fake in self._units.values():
            result.update(fake.started)
        return result

    def start(self, unit: str, invocation: str) -> Mapping[str, object]:
        self.calls.append({"method": "start", "unit": unit, "invocation": invocation})
        return self._units[unit].start(unit, invocation)

    def stop(self, unit: str, invocation: str) -> Mapping[str, object]:
        self.calls.append({"method": "stop", "unit": unit, "invocation": invocation})
        return self._units[unit].stop(unit, invocation)

    def inspect(self, unit: str, invocation: str) -> Mapping[str, object]:
        self.calls.append({"method": "inspect", "unit": unit, "invocation": invocation})
        result = self._units[unit].inspect(unit, invocation)
        if result.get("status") == "unknown":
            return {**result, "ok": False, "status": "unknown"}
        return result
