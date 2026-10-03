import pytest

from tests.sim.rig import SimSite


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="A5b2: nothing ever sends a beat, so the executor quarantines a protected lease as heartbeat-stale, and renew right after acquire is 409 approved maximum reached",
)
def test_repro_beat_and_renew(tmp_path):
    site = SimSite(
        tmp_path,
        {"host-ctl": {"boot_id": "boot-ctl", "monotonic_start": 1000.0}},
        controller_host="host-ctl",
        lanes={"lane-gpu0": "host-ctl"},
    )
    acquired = site.rpc("acquire", {"class": "batch", "purpose": "simulated work", "est_s": 600, "max_s": 3600}, lane="lane-gpu0")
    assert acquired["status"] == 200, acquired
    site.advance(900)
    state = site.executor_lane("lane-gpu0")
    assert state["state"] in {"starting", "running"}, state
    renewed = site.rpc("renew", {"token": acquired["data"]["token"], "extend_s": 60}, lane="lane-gpu0")
    assert renewed["status"] == 200, renewed
