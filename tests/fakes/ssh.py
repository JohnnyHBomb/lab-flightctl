"""A scripted transport fake: no shell, socket, SSH, or background process is used."""

from __future__ import annotations

from collections import deque
from typing import Mapping


class FakeSSH:
    def __init__(self, scripted: list[Mapping[str, object]] | None = None) -> None:
        self._script = deque(dict(item) for item in (scripted or []))
        self.calls: list[dict[str, object]] = []

    def queue(self, outcome: str, **payload: object) -> None:
        self._script.append({"outcome": outcome, **payload})

    def request(self, endpoint: str, message: Mapping[str, object], timeout_s: float) -> Mapping[str, object]:
        self.calls.append({"endpoint": endpoint, "message": dict(message), "timeout_s": timeout_s})
        scripted = self._script.popleft() if self._script else {"outcome": "timeout", "error": "no scripted reply"}
        outcome = scripted.get("outcome", "success")
        if outcome == "success":
            return {"status": "ok", "response": scripted.get("response", {})}
        if outcome == "denied":
            return {"status": "denied", "error": scripted.get("error", "access denied")}
        if outcome == "lost":
            return {"status": "lost", "error": scripted.get("error", "reply lost")}
        if outcome == "timeout":
            return {"status": "timeout", "error": scripted.get("error", "timed out")}
        if outcome == "delayed":
            return {"status": "delayed", "delay_s": scripted.get("delay_s", timeout_s), "error": scripted.get("error", "reply exceeded deadline")}
        if outcome == "unknown":
            return {"status": "unknown", "error": scripted.get("error", "unknown transport outcome")}
        return {"status": "failure", "error": scripted.get("error", f"unknown scripted outcome: {outcome}")}


FakeTransport = FakeSSH
