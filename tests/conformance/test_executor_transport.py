"""ExecutorTransport + executor entry point conformance. The real case spawns the real executor
stdio entry point (local subprocess, or the forced-command ssh key on the target) with REAL time;
the sim case uses one SimClock per host with DIFFERENT boot ids (the configuration that hid G02)."""

import pytest

from tests.contracts_v2.validation import assert_valid

from .conftest import port_params

pytestmark = pytest.mark.parametrize("kind,factory", port_params("executor_transport"))


def _rig(kind, factory):
    from . import registry
    return factory(registry.target())  # returns a rig: .transport, .host_id, .reserve(), .beat(), .stop(), .inspect_host()


def test_reserve_across_hosts_with_different_boot_ids_succeeds(kind, factory) -> None:
    rig = _rig(kind, factory)
    assert rig.controller_boot_id() != rig.host_boot_id()
    result = rig.transport.call(rig.host_id, rig.reserve_request(generation=1, expiry_in_s=120), timeout_s=20)
    assert result["status"] == "ok"
    assert_valid(result["reply"], "executor")
    assert result["reply"]["ok"] is True and result["reply"]["observed_state"] == "reserved"
    rig.cleanup()


@pytest.mark.realtime
def test_beat_extends_the_host_local_deadline(kind, factory) -> None:
    rig = _rig(kind, factory)
    rig.transport.call(rig.host_id, rig.reserve_request(generation=2, expiry_in_s=3, stale_in_s=3), timeout_s=20)
    for _ in range(3):
        rig.sleep(2)  # real seconds on the real twin, SimClock advance on the fake
        reply = rig.transport.call(rig.host_id, rig.beat_request(generation=2, expiry_in_s=3, stale_in_s=3), timeout_s=20)["reply"]
        assert reply["ok"] is True
    rig.run_enforcer()  # the host timer's one-shot
    assert rig.inspect_lease(generation=2)["observed_state"] in {"reserved", "running"}, "beaten lease must not quarantine"
    rig.cleanup()


@pytest.mark.realtime
def test_unbeaten_protected_lease_quarantines_on_host_clock_without_controller(kind, factory) -> None:
    rig = _rig(kind, factory)
    rig.transport.call(rig.host_id, rig.reserve_request(generation=3, expiry_in_s=60, stale_in_s=2, protected=True), timeout_s=20)
    rig.sleep(3)
    rig.run_enforcer()
    reply = rig.inspect_lease(generation=3)
    assert reply["observed_state"] == "quarantined" and rig.stop_calls() == 0, "protected work quarantines, never killed"
    rig.cleanup()


def test_stop_identity_equals_reserve_identity(kind, factory) -> None:
    rig = _rig(kind, factory)
    req = rig.reserve_request(generation=4, expiry_in_s=120)
    rig.transport.call(rig.host_id, req, timeout_s=20)
    stop = rig.stop_request_from(req, mode="owner-release")
    reply = rig.transport.call(rig.host_id, stop, timeout_s=60)["reply"]
    assert reply["ok"] is True and reply["observed_state"] == "free", reply.get("error")
    rig.cleanup()


def test_wrong_key_or_garbage_is_denied_or_unparsable_never_ok(kind, factory) -> None:
    rig = _rig(kind, factory)
    assert rig.call_with_wrong_identity()["status"] == "denied"
    assert rig.call_with_garbage()["status"] in {"unparsable", "failed"}
