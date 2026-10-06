from tests.sim.rig import SimSite


def test_repro_cross_host_reserve(tmp_path):
    site = SimSite(
        tmp_path,
        {
            "host-ctl": {"boot_id": "boot-ctl", "monotonic_start": 1000.0},
            "host-1": {"boot_id": "boot-1", "monotonic_start": 9000.0, "skew_s": 5.0},
        },
        controller_host="host-ctl",
        lanes={"lane-gpu1": "host-1"},
    )
    response = site.rpc("acquire", {"purpose": "simulated work", "est_s": 600, "max_s": 3600}, lane="lane-gpu1")
    assert response["status"] == 200, response
    state = site.executor_lane("lane-gpu1")
    assert state["state"] == "starting", state
    assert state["generation"] == response["data"]["generation"], (state, response)
