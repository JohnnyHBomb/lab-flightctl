from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from flightctl.authority import Authority
from flightctl.store import SQLiteStore
from tests.authority.helpers import OTHER_PRINCIPAL, PRINCIPAL, Executor, make_authority, request
from tests.fakes.clock import FakeClock
from tests.fakes.gpu import FakeGPUProbe
from tests.fakes.ssh import FakeTransport


ROOT = Path(__file__).resolve().parents[2]


def _lanes(*lane_ids: str, quota_key: str | None = None) -> list[dict[str, object]]:
    return [
        {
            "lane_id": lane_id,
            "host_id": f"host-{index}",
            "reachability": "confirmed",
            "enabled": True,
            **({"quota_key": quota_key} if quota_key is not None else {}),
        }
        for index, lane_id in enumerate(lane_ids, 1)
    ]


def test_revision3_current_booking_exclusivity_and_end_bound(tmp_path):
    authority, transport, _clock = make_authority(tmp_path, other_mapping=True)
    booked = authority.handle(
        request("current-booking", "book", {"start": "2026-09-28T10:00:00Z", "end": "2026-09-28T10:15:00Z", "purpose": "benchmark"}),
        peer="peer-a",
    )
    assert booked["status"] == 200
    booking = booked["data"]["booking"]

    blocked = authority.handle(
        request("booking-exclusivity", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 36000}, principal=OTHER_PRINCIPAL),
        peer="peer-b",
    )
    assert blocked["status"] == 409
    assert not transport.calls

    bounded = authority.handle(
        request(
            "booking-bound-acquire",
            "acquire",
            {"purpose": "benchmark", "class": "booked", "est_s": 1, "max_s": 36000, "booking_id": booking["booking_id"]},
        ),
        peer="peer-a",
    )
    assert bounded["status"] == 200
    assert bounded["data"]["lease"]["max_end"] == booking["end"]
    assert bounded["data"]["lease"]["approved_max_end"] == booking["end"]


def test_revision3_confirmed_empty_release_retires_quota_but_uncertain_stop_does_not(tmp_path):
    released_path = tmp_path / "released"
    released_path.mkdir()
    released, released_transport, released_clock = make_authority(
        released_path,
        lanes=_lanes("lane-a", "lane-b", quota_key="device-a"),
        policy={"quotas": {"device-a": 1}},
    )
    first = released.handle(request("quota-release-first", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60}, lane="lane-a"), peer="peer-a")
    assert first["status"] == 200
    released_result = released.handle(request("quota-release", "release", {"token": first["data"]["token"]}, lane="lane-a"), peer="peer-a")
    assert released_result["status"] == 200
    lease, reservation_status = released.store.get_lease(token=first["data"]["token"])
    assert lease["reservation"]["state"] == "released"
    assert reservation_status == "released"
    second = released.handle(request("quota-release-second", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60}, lane="lane-b", principal=OTHER_PRINCIPAL), peer="peer-b")
    assert second["status"] == 200
    assert [call["message"]["kind"] for call in released_transport.calls] == ["reserve", "stop", "reserve"]
    released_clock.jump_utc(31)
    assert released.handle(request("released-skew", "queue", {"action": "list"}, lane=None), peer="peer-a")["status"] == 503
    assert released.store.get_lane("lane-a")["state"] == "free"

    uncertain_path = tmp_path / "uncertain"
    uncertain_path.mkdir()
    uncertain, uncertain_transport, _clock = make_authority(
        uncertain_path,
        lanes=_lanes("lane-a", "lane-b", quota_key="device-a"),
        policy={"quotas": {"device-a": 1}},
        transport=Executor(stop="lost"),
    )
    uncertain_first = uncertain.handle(request("quota-uncertain-first", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60}, lane="lane-a"), peer="peer-a")
    assert uncertain_first["status"] == 200
    uncertain_result = uncertain.handle(request("quota-uncertain-stop", "release", {"token": uncertain_first["data"]["token"]}, lane="lane-a"), peer="peer-a")
    assert uncertain_result["status"] == 503
    assert uncertain.store.get_lane("lane-a")["state"] == "quarantined"
    denied = uncertain.handle(request("quota-uncertain-second", "acquire", {"purpose": "benchmark", "class": "batch", "est_s": 1, "max_s": 60}, lane="lane-b", principal=OTHER_PRINCIPAL), peer="peer-b")
    assert denied["status"] == 409
    assert [call["message"]["kind"] for call in uncertain_transport.calls] == ["reserve", "stop"]


def test_revision3_executor_vector_and_gpu_observation_cross_production_boundary(tmp_path):
    vector = json.loads((ROOT / "tests" / "contracts" / "vectors" / "executor.json").read_text(encoding="utf-8"))
    reserve_request = vector["requests"][0]
    reserve_reply = vector["replies"][0]
    identity = reserve_request["identity"]
    lane = {"lane_id": identity["lane"]["lane_id"], "lane": identity["lane"], "endpoint": "host-1", "state": "starting", "generation": identity["generation"]}
    lease = {
        "lane": identity["lane"],
        "generation": identity["generation"],
        "token": identity["token"],
        "instance": identity["instance"],
        "unit": identity["unit"],
        "invocation": identity["invocation"],
        "deadline": identity["deadline"],
        "class": reserve_request["execution_policy"]["class"],
        "max_end": reserve_request["execution_policy"]["max_end"],
    }
    transport = FakeTransport([{"outcome": "success", "response": reserve_reply}])
    authority, _unused, _clock = make_authority(tmp_path, transport=transport)
    ok, reply, reason = authority._executor_call(lane, lease, "reserve")
    assert ok is True
    assert reply == reserve_reply
    assert reason == "ok"
    assert transport.calls[0]["message"]["kind"] == "reserve"

    bad_stop_reply = copy.deepcopy(vector["replies"][2])
    bad_stop_reply["ok"] = True
    bad_stop_reply["uncertain"] = False
    bad_stop_reply["error"] = None
    bad_stop_reply["cgroup_occupants"] = ["occupant-a"]
    bad_stop_reply["gpu_tenants"] = []
    stop_transport = FakeTransport([{"outcome": "success", "response": bad_stop_reply}])
    stop_authority, _unused, _clock = make_authority(tmp_path / "stop", transport=stop_transport)
    stop_lease = dict(lease, unit="unit-a", invocation="invoke-a")
    stop_lease["deadline"] = vector["requests"][3]["identity"]["deadline"]
    stop_lease["max_end"] = vector["requests"][3]["execution_policy"]["max_end"]
    stop_ok, _reply, _reason = stop_authority._executor_call(lane, stop_lease, "stop")
    assert stop_ok is False

    probe = FakeGPUProbe({"host-1": {"family": "nvidia", "raw": "Model X,8192,555.1,NVIDIA"}})
    observation = probe.inspect("host-1")
    assert observation["count"] == 1
    inventory = {
        "site_id": "site-a",
        "controller": {"controller_id": "controller-a"},
        "hosts": [{"host_id": "host-1", "ssh_endpoint": "host-1", "reachability": "confirmed", "gpu_count": observation["count"], "devices": observation["devices"]}],
        "lanes": [{"lane_id": "lane-gpu0", "host_id": "host-1", "reachability": "confirmed", "enabled": True}],
    }
    bootstrapped = Authority(
        SQLiteStore(tmp_path / "inventory.sqlite"),
        FakeTransport(),
        FakeClock(datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)),
        inventory=inventory,
        identity_mapping=[{"external_id": "peer-a", "principal": PRINCIPAL, "roles": ["agent"]}],
    )
    assert bootstrapped.store.get_lane("lane-gpu0")["endpoint"] == "host-1"
    assert probe.calls == [{"host": "host-1"}]
