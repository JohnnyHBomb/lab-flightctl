"""A systemd fake with identity and cgroup occupancy checks."""

from __future__ import annotations

from collections import deque
from typing import Mapping


class FakeSystemd:
    def __init__(self, scripted: list[Mapping[str, object]] | None = None, occupants: list[str] | None = None, gpu_occupants: list[str] | None = None) -> None:
        self._script = deque(dict(item) for item in (scripted or []))
        self.occupants = list(occupants or [])
        self.gpu_occupants = list(gpu_occupants or [])
        self.calls: list[dict[str, object]] = []
        self.started: set[tuple[str, str]] = set()
        self._invocations: dict[str, str] = {}

    def queue(self, outcome: str, **payload: object) -> None:
        self._script.append({"outcome": outcome, **payload})

    def _next(self, method: str, unit: str, invocation: str) -> dict[str, object]:
        self.calls.append({"method": method, "unit": unit, "invocation": invocation})
        scripted = self._script.popleft() if self._script else {"outcome": "success"}
        outcome = scripted.get("outcome", "success")
        if unit in self._invocations and self._invocations[unit] != invocation:
            return self._result(unit, invocation, False, "invocation_mismatch")
        if outcome in {"invocation_mismatch", "failed_stop", "timeout", "unknown", "lost"}:
            return self._result(unit, invocation, False, str(outcome))
        return self._result(unit, invocation, True, "success")

    def _result(self, unit: str, invocation: str, ok: bool, status: str) -> dict[str, object]:
        return {"ok": ok, "status": status, "unit": unit, "invocation": invocation, "occupants": list(self.occupants), "cgroup_occupants": list(self.occupants), "gpu_occupants": list(self.gpu_occupants)}

    def start(self, unit: str, invocation: str) -> Mapping[str, object]:
        result = self._next("start", unit, invocation)
        if result.get("ok"):
            self.started.add((unit, invocation))
            self._invocations[unit] = invocation
        return result

    def stop(self, unit: str, invocation: str) -> Mapping[str, object]:
        result = self._next("stop", unit, invocation)
        if result.get("ok"):
            self.started.discard((unit, invocation))
            self._invocations.pop(unit, None)
            self.occupants.clear()
            self.gpu_occupants.clear()
        return result

    def inspect(self, unit: str, invocation: str) -> Mapping[str, object]:
        return self._next("inspect", unit, invocation)
