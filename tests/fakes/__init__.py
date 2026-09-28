"""Scripted, side-effect-free test doubles for contract consumers."""

from .clock import FakeClock
from .gpu import FakeGPUProbe
from .ssh import FakeSSH
from .systemd import FakeSystemd

__all__ = ["FakeClock", "FakeGPUProbe", "FakeSSH", "FakeSystemd"]
