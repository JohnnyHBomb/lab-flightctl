from tests.authority.test_a5b2_beat_renew import ACQUIRE, Site, rpc


def test_repro_beat_and_renew(tmp_path):
    site = Site(tmp_path)
    acquired = rpc(site.authority, "acquire-1", "acquire", {**ACQUIRE, "class": "batch"})
    assert acquired["status"] == 200, acquired
    for minute in range(1, 16):
        site.advance(60)
        assert [beat["ok"] for beat in site.authority.beat_leases()] == [True], minute
        assert site.host.enforce_deadlines() == [], minute
    renewed = rpc(site.authority, "renew-1", "renew", {"token": acquired["data"]["token"], "extend_s": 60})
    assert renewed["status"] == 200, renewed
