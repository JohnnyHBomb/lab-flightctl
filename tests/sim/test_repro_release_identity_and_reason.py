import json

import pytest

from tests.sim.rig import SimSite


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="A5b1: the reserve fences unit/invocation None and the authority assigns them after the reserve, so release is refused (stop identity mismatch) and the executor's reason never reaches the RPC error",
)
def test_repro_release_identity_and_reason(tmp_path):
    site = SimSite(
        tmp_path,
        {"host-ctl": {"boot_id": "boot-ctl", "monotonic_start": 1000.0}},
        controller_host="host-ctl",
        lanes={"lane-gpu0": "host-ctl"},
    )
    args = {"purpose": "simulated work", "est_s": 600, "max_s": 3600}
    acquired = site.rpc("acquire", args, lane="lane-gpu0")
    assert acquired["status"] == 200, acquired
    released = site.rpc("release", {"token": acquired["data"]["token"]}, lane="lane-gpu0")
    assert released["status"] == 200, released
    state = site.executor_lane("lane-gpu0")
    assert state["state"] == "free", state

    acquired = site.rpc("acquire", args, lane="lane-gpu0")
    assert acquired["status"] == 200, acquired
    site.hosts["host-ctl"].systemd.queue("failed_stop")
    released = site.rpc("release", {"token": acquired["data"]["token"]}, lane="lane-gpu0")
    assert released["status"] != 200, released
    assert "systemd stop reply was not a verified success" in json.dumps(released["error"]), released
