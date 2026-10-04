"""Replay fake registration for the occupancy conformance port (A3i)."""

from tests.fakes.gpu_replay import ReplayGPUProbe

from . import registry


registry.register("occupancy_probe", "fake", lambda target: ReplayGPUProbe())
