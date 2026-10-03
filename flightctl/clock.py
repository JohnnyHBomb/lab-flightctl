"""The real Clock twin: the host's UTC time, monotonic clock and kernel boot id.

The boot id is read once, at construction, from the kernel; there is no fallback.
A process that cannot read it must not start (ClockUnavailable).
"""

import re as _re
import time as _time
from datetime import datetime as _datetime, timezone as _timezone

BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"

_BOOT_ID = _re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class ClockUnavailable(RuntimeError):
    """The host's boot id cannot be read; the process must not start."""


class RealClock:
    def __init__(self, *, boot_id_path: str = BOOT_ID_PATH) -> None:
        try:
            with open(boot_id_path, encoding="ascii") as handle:
                value = handle.read().strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise ClockUnavailable(f"cannot read the host boot id from {boot_id_path}: {exc}") from exc
        if not _BOOT_ID.fullmatch(value):
            raise ClockUnavailable(f"{boot_id_path} does not hold a kernel boot id (lowercase UUID), found {value[:80]!r}")
        self._boot_id = value

    def utc(self) -> _datetime:
        return _datetime.now(_timezone.utc)

    def monotonic(self) -> float:
        return _time.monotonic()

    def boot_id(self) -> str:
        return self._boot_id
