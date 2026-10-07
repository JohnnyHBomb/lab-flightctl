import json

from tests.authority.test_a5b1_executor_client import ACQUIRE, Site, rpc


def test_repro_release_identity_and_reason(tmp_path):
    site = Site(tmp_path)
    authority = site.authority()
    acquired = rpc(authority, "acquire-1", "acquire", ACQUIRE)
    assert acquired["status"] == 200, acquired
    released = rpc(authority, "release-1", "release", {"token": acquired["data"]["token"]})
    assert released["status"] == 200, released
    (reserve, _), (stop, _) = site.calls
    assert stop["identity"] == reserve["identity"], (reserve, stop)
    state = authority.store.get_lane("lane-gpu1")
    assert state["state"] == "free", state

    acquired = rpc(authority, "acquire-2", "acquire", ACQUIRE)
    assert acquired["status"] == 200, acquired
    site.probe.tenant = True
    released = rpc(authority, "release-2", "release", {"token": acquired["data"]["token"]})
    assert released["status"] != 200, released
    assert "gpu_tenant" in json.dumps(released["error"]), released
