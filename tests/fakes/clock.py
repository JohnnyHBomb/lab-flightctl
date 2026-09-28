"""An independently controllable UTC/monotonic clock with reboot simulation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
class FakeClock:
    def __init__(self, utc_at: datetime | None = None, monotonic_s: float = 0.0, boot_id: str = "boot-a") -> None:
        self._utc = (utc_at or datetime(2026, 9, 27, 20, 0, tzinfo=timezone.utc)).astimezone(timezone.utc)
        self._monotonic = float(monotonic_s)
        self._boot = boot_id
        self.calls: list[dict[str, object]] = []

    def utc(self) -> datetime:
        self.calls.append({"method": "utc"})
        return self._utc

    def monotonic(self) -> float:
        self.calls.append({"method": "monotonic"})
        return self._monotonic

    def boot_id(self) -> str:
        self.calls.append({"method": "boot_id"})
        return self._boot

    def advance(self, *, utc_s: float = 0.0, monotonic_s: float = 0.0) -> None:
        """Advance each time base independently; omitted bases do not move."""

        self._utc += timedelta(seconds=utc_s)
        self._monotonic += monotonic_s
        self.calls.append({"method": "advance", "utc_s": utc_s, "monotonic_s": monotonic_s})

    def jump_utc(self, seconds: float) -> None:
        self.advance(utc_s=seconds)

    def jump_monotonic(self, seconds: float) -> None:
        self.advance(monotonic_s=seconds)

    def reboot(self, *, boot_id: str | None = None, utc_s: float = 0.0, monotonic_s: float = 0.0) -> None:
        self._boot = boot_id or f"{self._boot}-next"
        self._utc += timedelta(seconds=utc_s)
        self._monotonic = float(monotonic_s)
        self.calls.append({"method": "reboot", "boot_id": self._boot, "utc_s": utc_s, "monotonic_s": monotonic_s})
