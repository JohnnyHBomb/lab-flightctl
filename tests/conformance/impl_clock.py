"""Clock twins for conformance (A1): the sim rig's SimClock as the fake, flightctl.clock.RealClock as the real twin."""

from flightctl.clock import RealClock
from tests.sim.rig import SimClock

from . import registry

registry.register("clock", "fake", lambda target: SimClock(boot_id="sim-conformance"))
registry.register("clock", "real", lambda target: RealClock())
