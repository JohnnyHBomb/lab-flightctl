"""Regression evidence for authority and cleanup reply fences."""

import pytest

from ondemand.chat import ChatSelection
from tests.chat.support import UnitSystemdAdapter
from tests.chat.test_chat import grant_response, load_one, make_controller, queue_load, unload_response


@pytest.mark.parametrize("binding", ["reservation-generation", "reservation-state", "lease-reservation-lane", "principal", "token", "adoption-source"])
def test_load_rejects_incomplete_or_contradictory_grant(binding):
    controller, transport, systemd, _, _ = make_controller()
    response = grant_response(request_id="grant-fence")
    data = response["data"]
    if binding == "reservation-generation":
        data["reservation"]["generation"] = 2
    elif binding == "reservation-state":
        data["reservation"]["state"] = "released"
    elif binding == "lease-reservation-lane":
        data["lease"]["reservation"]["lane"] = {**data["lease"]["lane"], "host_id": "other-host"}
    elif binding == "principal":
        del data["lease"]["principal"]
    elif binding == "token":
        del data["lease"]["token"]
    else:
        data["adoption"]["token_source"] = "authenticated-adoption"
    queue_load(transport, response)
    result = controller.load(ChatSelection("pipeline-a", "interactive inference"), "grant-fence")
    assert not result.ok
    assert not systemd.calls


@pytest.mark.parametrize("operation", ["load", "unload"])
@pytest.mark.parametrize("field,value", [("schema", 2), ("error", {"code": "contradiction"})])
def test_authority_success_envelope_must_be_consistent(operation, field, value):
    controller, transport, systemd, _, _ = make_controller()
    if operation == "unload":
        load_one(controller, transport)
    response = (grant_response if operation == "load" else unload_response)(request_id="envelope-fence")
    response[field] = value
    queue_load(transport, response)
    before = list(systemd.calls)
    if operation == "load":
        result = controller.load(ChatSelection("pipeline-a", "interactive inference"), "envelope-fence")
    else:
        result = controller.unload(request_id="envelope-fence")
    assert not result.ok
    assert systemd.calls == before


@pytest.mark.parametrize("field,value", [("invocation", "other-invocation"), ("status", "unknown"), ("status", "unrecognised"), ("gpu_occupants", None)])
def test_contradictory_stop_reply_cannot_be_overridden_by_empty_inspection(field, value):
    class ContradictoryStop(UnitSystemdAdapter):
        def stop(self, unit, invocation):
            return {**super().stop(unit, invocation), field: value}

    controller, transport, systemd, _, _ = make_controller(systemd=ContradictoryStop())
    load_one(controller, transport)
    queue_load(transport, unload_response(request_id="stop-fence"))
    result = controller.unload(request_id="stop-fence")
    assert not result.ok
    assert controller.state == "quarantined"
    starts = len([call for call in systemd.calls if call["method"] == "start"])
    assert not controller.load(ChatSelection("pipeline-a", "interactive inference"), "successor").ok
    assert len([call for call in systemd.calls if call["method"] == "start"]) == starts
