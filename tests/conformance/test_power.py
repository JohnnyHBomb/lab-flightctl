"""Waker and Inhibitor conformance (power.schema.json). The real waker case needs a sleeping target
and is run by the gauge as acceptance T07; here the real twin is exercised on an AWAKE host
(already-awake path and answer probe) so hosted/lab-ci runs never send wake packets."""

import pytest

from tests.contracts_v2.validation import assert_valid

from .conftest import port_params



def _impl(kind, factory):
    from . import registry
    return factory(registry.target())


@pytest.mark.parametrize("kind,factory", port_params("inhibitor"))
def test_hold_is_listed_and_release_removes_it(kind, factory) -> None:
    inh = _impl(kind, factory)
    held = inh.hold("conformance", 1, why="conformance check", timeout_s=20)
    assert held["error"] is None and held["unit"] == "flightctl-awake-conformance-g1.service"
    if kind == "real":
        assert held["held"] is True
        assert "flightctl-awake-conformance-g1.service" in inh.list(timeout_s=10)["units"]
    released = inh.release("conformance", 1, timeout_s=20)
    assert released["error"] is None
    assert "flightctl-awake-conformance-g1.service" not in inh.list(timeout_s=10)["units"]


@pytest.mark.parametrize("kind,factory", port_params("inhibitor"))
def test_hold_is_idle_block_not_sleep(kind, factory) -> None:
    inh = _impl(kind, factory)
    assert inh.what == "idle", "polkit refuses sleep inhibitors over non-interactive ssh (measured 2 Oct)"


@pytest.mark.fake_only
@pytest.mark.parametrize("kind,factory", port_params("inhibitor"))
def test_refused_inhibitor_is_a_typed_error(kind, factory) -> None:
    inh = _impl(kind, factory)
    inh.script_next("refused")
    held = inh.hold("conformance", 2, why="x", timeout_s=5)
    assert held["held"] is False and held["error"]["code"] == "inhibitor_failed"


@pytest.mark.parametrize("kind,factory", port_params("waker"))
def test_awake_host_answers_and_wake_is_already_awake(kind, factory) -> None:
    waker = _impl(kind, factory)
    assert waker.answered(waker.test_host, timeout_s=10) is True
    attempt = waker.wake(waker.test_host, waker.test_profile, reason="conformance")
    assert_valid(attempt, "power", "wake_attempt")
    assert attempt["result"] == "already-awake"


@pytest.mark.fake_only
@pytest.mark.parametrize("kind,factory", port_params("waker"))
def test_sim_host_that_never_wakes_times_out_never_free(kind, factory) -> None:
    waker = _impl(kind, factory)
    waker.sim_sleep(waker.test_host, wakes=False)
    attempt = waker.wake(waker.test_host, waker.test_profile, reason="conformance")
    waker.sim_advance(181)
    assert waker.answered(waker.test_host, timeout_s=1) is False
    assert attempt["result"] in {"pending", "timed-out"}
