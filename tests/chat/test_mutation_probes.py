"""Acceptance assertions exercised against the required in-process mutants."""

from __future__ import annotations

import pytest

from ondemand.chat import ChatAdapter, ChatSelection
from tests.chat.test_chat import (
    grant_response,
    load_one,
    make_controller,
    queue_load,
    unload_response,
)


def _assert_health_anchor(controller, adapter: ChatAdapter, clock) -> None:
    clock.advance(utc_s=599, monotonic_s=599)
    adapter.health_check()
    adapter.connected()
    assert not controller.idle_due()
    clock.advance(utc_s=1, monotonic_s=1)
    assert controller.idle_due()


def _assert_active_stream_is_not_idle(controller, adapter: ChatAdapter, clock) -> None:
    assert adapter.stream_started("stream-mutant")
    clock.advance(utc_s=601, monotonic_s=601)
    assert not controller.idle_due()


def _assert_start_requires_registration(controller, transport, systemd) -> None:
    transport.queue("denied")
    original_load = controller.load

    def mutant_load(selection, request_id=None, **kwargs):
        systemd.start("unit-mutant", "invoke-mutant")
        return original_load(selection, request_id, **kwargs)

    controller.load = mutant_load
    result = controller.load(
        ChatSelection("pipeline-a", "interactive inference", compatible_lanes=frozenset({"lane-a"})),
        "registration-mutant",
    )
    assert not result.ok
    assert not [call for call in systemd.calls if call["method"] == "start"]


def _assert_failed_unload_remains_quarantined(controller, transport, systemd) -> None:
    load_one(controller, transport)
    systemd.queue("unit-a", "failed_stop")
    queue_load(transport, unload_response(request_id="failed-unload-mutant"))
    result = controller.unload(occupant_id="occupant-a", generation=1, request_id="failed-unload-mutant")
    assert not result.ok
    assert controller.state == "quarantined"


def _assert_reconnect_does_not_reload(controller, transport, systemd) -> None:
    load_one(controller, transport)
    queue_load(transport, unload_response(request_id="reconnect-unload-mutant"))
    assert controller.unload(occupant_id="occupant-a", generation=1, request_id="reconnect-unload-mutant").ok
    queue_load(
        transport,
        grant_response(
            generation=2,
            token="token-bbbbbbbbbbbbbbbb",
            lease_id="occupant-b",
            unit="unit-b",
            invocation="invoke-b",
            request_id="reconnect-mutant",
        ),
    )
    starts = len([call for call in systemd.calls if call["method"] == "start"])
    controller.on_reconnect = lambda: controller.load(
        ChatSelection("pipeline-b", "interactive inference", compatible_lanes=frozenset({"lane-a"})),
        "reconnect-mutant",
    )
    assert controller.on_reconnect() is False
    assert len([call for call in systemd.calls if call["method"] == "start"]) == starts


@pytest.mark.parametrize("mutation", ["health", "active-stream", "pre-registration", "failed-unload", "reconnect"])
def test_chat_mutations(monkeypatch, mutation: str) -> None:
    """Every required mutation must fail its unchanged behavioural assertion."""

    if mutation == "health":
        controller, transport, _, clock, _ = make_controller()
        load_one(controller, transport)
        adapter = ChatAdapter(controller, generation=1, occupant_id="occupant-a")

        def mutant_health():
            controller._current.last_activity_monotonic += 1
            return controller.public_status()

        monkeypatch.setattr(controller, "health_check", mutant_health)
        with pytest.raises(AssertionError):
            _assert_health_anchor(controller, adapter, clock)
        return

    if mutation == "active-stream":
        controller, transport, _, clock, _ = make_controller()
        load_one(controller, transport)
        adapter = ChatAdapter(controller, generation=1, occupant_id="occupant-a")

        def mutant_idle_due() -> bool:
            occupant = controller._current
            return occupant is not None and controller.clock.monotonic() - occupant.last_activity_monotonic >= controller.idle_timeout_s

        monkeypatch.setattr(controller, "idle_due", mutant_idle_due)
        with pytest.raises(AssertionError):
            _assert_active_stream_is_not_idle(controller, adapter, clock)
        return

    if mutation == "pre-registration":
        controller, transport, systemd, _, _ = make_controller()
        with pytest.raises(AssertionError):
            _assert_start_requires_registration(controller, transport, systemd)
        return

    if mutation == "failed-unload":
        controller, transport, systemd, _, _ = make_controller()
        original_failure = controller._unload_failure

        def mutant_failure(request_id, occupant, error, response):
            result = original_failure(request_id, occupant, error, response)
            controller._current = None
            return result

        monkeypatch.setattr(controller, "_unload_failure", mutant_failure)
        with pytest.raises(AssertionError):
            _assert_failed_unload_remains_quarantined(controller, transport, systemd)
        return

    controller, transport, systemd, _, _ = make_controller()
    with pytest.raises(AssertionError):
        _assert_reconnect_does_not_reload(controller, transport, systemd)
