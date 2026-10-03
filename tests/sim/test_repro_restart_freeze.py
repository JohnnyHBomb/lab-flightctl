import pytest

from tests.sim.rig import PEER, SimSite


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="A8: after the controller host reboots the authority refuses every request with 503 clock skew or reboot requires reconciliation, and reconcile() never lifts the freeze",
)
def test_repro_restart_freeze(tmp_path):
    site = SimSite(
        tmp_path,
        {"host-ctl": {"boot_id": "boot-ctl", "monotonic_start": 1000.0}},
        controller_host="host-ctl",
        lanes={"lane-gpu0": "host-ctl"},
    )
    site.tick()
    site.hosts["host-ctl"].reboot("boot-ctl-next")
    site.restart_authority()
    site.authority.reconcile(peer=PEER)
    response = site.rpc("acquire", {"purpose": "simulated work", "est_s": 600, "max_s": 3600}, lane="lane-gpu0")
    assert response["status"] == 200, response
