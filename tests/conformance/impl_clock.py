"""Clock twins for conformance (A1): the sim rig's SimClock as the fake, flightctl.clock.RealClock as the real twin.

Amendment 14: RealClock reads the clock and boot id of the host the test process runs on, so its real twin proves
nothing about a remote target. The real factory refuses any target other than host-local (G3 sweep 7 Nov, F1: an ssh-form
run recorded the runner's boot id as the target's); clock G3 evidence comes from a host-local run on each host."""

from flightctl.clock import RealClock
from tests.sim.rig import SimClock

from . import registry


class RemoteClockTarget(ValueError):
    """The real clock twin cannot observe a remote host."""


def real_clock(target):
    if target != "host-local":
        raise RemoteClockTarget(f"the real clock twin reads the local host only; run clock conformance on the target host "
                                f"with FLIGHTCTL_CONFORMANCE_TARGET=host-local, not {target!r}")
    return RealClock()


registry.register("clock", "fake", lambda target: SimClock(boot_id="sim-conformance"))
registry.register("clock", "real", real_clock)
