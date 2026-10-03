from datetime import timedelta

import pytest

from tests.sim.rig import START_UTC, SimSite


def test_sim_hosts_have_distinct_clocks(tmp_path):
    site = SimSite(
        tmp_path,
        {
            "host-ctl": {"boot_id": "boot-ctl", "monotonic_start": 1000.0},
            "host-1": {"boot_id": "boot-1", "monotonic_start": 9000.0, "skew_s": 5.0},
        },
        controller_host="host-ctl",
        lanes={"lane-gpu0": "host-ctl", "lane-gpu1": "host-1"},
    )
    controller = site.hosts["host-ctl"].clock
    remote = site.hosts["host-1"].clock
    assert controller is not remote
    assert controller.boot_id() == "boot-ctl"
    assert remote.boot_id() == "boot-1"
    assert controller.monotonic() == 1000.0
    assert remote.monotonic() == 9000.0
    assert controller.utc() == START_UTC
    assert remote.utc() == START_UTC + timedelta(seconds=5)
    assert site.authority.clock is controller
    for host in site.hosts.values():
        assert host.executor.clock is host.clock

    site.advance(125)
    assert controller.monotonic() == 1125.0
    assert remote.monotonic() == 9125.0
    assert controller.utc() == START_UTC + timedelta(seconds=125)
    assert remote.utc() == START_UTC + timedelta(seconds=130)

    remote_before = (remote.utc(), remote.monotonic(), remote.boot_id())
    controller.advance(7.5)
    assert controller.monotonic() == 1132.5
    assert controller.utc() == START_UTC + timedelta(seconds=132.5)
    assert (remote.utc(), remote.monotonic(), remote.boot_id()) == remote_before
    acquired = site.rpc("acquire", {"purpose": "simulated work", "est_s": 600, "max_s": 3600}, lane="lane-gpu0")
    assert acquired["status"] == 200, acquired
    host = site.hosts["host-ctl"]
    executor_before, store_before = host.executor, host.state_store
    utc_before = controller.utc()
    host.reboot("boot-ctl-next")
    assert host.executor is not executor_before
    assert host.state_store is store_before
    assert host.executor.clock is controller
    lane = site.executor_lane("lane-gpu0")
    assert lane["generation"] == acquired["data"]["generation"], (lane, acquired)
    assert lane["reconcile_required"] is True, lane
    assert controller.boot_id() == "boot-ctl-next"
    assert controller.monotonic() == 0.0
    assert controller.utc() == utc_before
    assert (remote.utc(), remote.monotonic(), remote.boot_id()) == remote_before
    with pytest.raises(ValueError):
        controller.advance(-1)
    with pytest.raises(ValueError):
        controller.reboot("boot-ctl-next")
    assert controller.monotonic() == 0.0
    assert controller.utc() == utc_before
