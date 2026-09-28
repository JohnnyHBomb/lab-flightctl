"""Acceptance assertions exercised against controlled in-process mutants."""

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
    result = controller.load(
        ChatSelection("pipeline-a", "interactive inference", compatible_lanes=frozenset({"lane-a"})),
        "registration-mutant",
    )
    assert not result.ok
    assert not [call for call in systemd.calls if call["method"] == "start"]


def _assert_failed_unload_remains_quarantined(controller, transport, systemd) -> None:
    load_one(controller, transport)
    systemd.queue("unit-a", "success")
    systemd.queue("unit-a", "failed_stop")
    queue_load(transport, unload_response(request_id="failed-unload-mutant"))
    result = controller.unload(occupant_id="occupant-a", generation=1, request_id="failed-unload-mutant")
    assert not result.ok
    assert controller.state == "quarantined"
    assert [call["method"] for call in systemd.calls] == ["start", "inspect", "stop"]


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
    assert controller.on_reconnect() is False
    assert len([call for call in systemd.calls if call["method"] == "start"]) == starts


def _apply_start_before_registration_mutant(monkeypatch, controller, systemd) -> None:
    original_load = controller.load

    def mutant_load(selection, request_id=None, **kwargs):
        systemd.start("unit-mutant", "invoke-mutant")
        return original_load(selection, request_id, **kwargs)

    monkeypatch.setattr(controller, "load", mutant_load)


def _apply_failed_unload_free_mutant(monkeypatch, controller) -> None:
    original_failure = controller._unload_failure

    def mutant_failure(request_id, occupant, error, response):
        result = original_failure(request_id, occupant, error, response)
        controller._current = None
        return result

    monkeypatch.setattr(controller, "_unload_failure", mutant_failure)


def _apply_reconnect_reload_mutant(monkeypatch, controller) -> None:
    def mutant_reconnect():
        return controller.load(
            ChatSelection("pipeline-b", "interactive inference", compatible_lanes=frozenset({"lane-a"})),
            "reconnect-mutant",
        )

    monkeypatch.setattr(controller, "on_reconnect", mutant_reconnect)


def _run_case(mutation: str, monkeypatch, *, apply_mutation: bool) -> None:
    if mutation == "health":
        controller, transport, _, clock, _ = make_controller()
        load_one(controller, transport)
        adapter = ChatAdapter(controller, generation=1, occupant_id="occupant-a")
        if apply_mutation:
            def mutant_health():
                controller._current.last_activity_monotonic += 1
                return controller.public_status()

            monkeypatch.setattr(controller, "health_check", mutant_health)
        _assert_health_anchor(controller, adapter, clock)
        return

    if mutation == "active-stream":
        controller, transport, _, clock, _ = make_controller()
        load_one(controller, transport)
        adapter = ChatAdapter(controller, generation=1, occupant_id="occupant-a")
        if apply_mutation:
            def mutant_idle_due() -> bool:
                occupant = controller._current
                return occupant is not None and controller.clock.monotonic() - occupant.last_activity_monotonic >= controller.idle_timeout_s

            monkeypatch.setattr(controller, "idle_due", mutant_idle_due)
        _assert_active_stream_is_not_idle(controller, adapter, clock)
        return

    if mutation == "pre-registration":
        controller, transport, systemd, _, _ = make_controller()
        if apply_mutation:
            _apply_start_before_registration_mutant(monkeypatch, controller, systemd)
        _assert_start_requires_registration(controller, transport, systemd)
        return

    if mutation == "failed-unload":
        controller, transport, _, _, _ = make_controller()
        if apply_mutation:
            _apply_failed_unload_free_mutant(monkeypatch, controller)
        _assert_failed_unload_remains_quarantined(controller, transport, controller.systemd)
        return

    controller, transport, systemd, _, _ = make_controller()
    if apply_mutation:
        _apply_reconnect_reload_mutant(monkeypatch, controller)
    _assert_reconnect_does_not_reload(controller, transport, systemd)


@pytest.mark.parametrize("mutation", ["health", "active-stream", "pre-registration", "failed-unload", "reconnect"])
def test_chat_mutations(monkeypatch, mutation: str) -> None:
    """The same behavioural assertion passes normally and kills its mutant."""

    _run_case(mutation, monkeypatch, apply_mutation=False)
    with pytest.raises(AssertionError):
        _run_case(mutation, monkeypatch, apply_mutation=True)
