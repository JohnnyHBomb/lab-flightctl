"""Dependency-injection interfaces used by the controller and its fakes."""

from datetime import datetime
from typing import Mapping, Protocol


class Clock(Protocol):
    def utc(self) -> datetime: ...

    def monotonic(self) -> float: ...

    def boot_id(self) -> str: ...


class Transport(Protocol):
    def request(self, endpoint: str, message: Mapping[str, object], timeout_s: float) -> Mapping[str, object]: ...


class GPUProbe(Protocol):
    def inspect(self, host: str) -> Mapping[str, object]: ...


class Systemd(Protocol):
    def start(self, unit: str, invocation: str) -> Mapping[str, object]: ...

    def stop(self, unit: str, invocation: str) -> Mapping[str, object]: ...

    def inspect(self, unit: str, invocation: str) -> Mapping[str, object]: ...
