"""Third-review regressions at the public CLI and stdin boundaries."""

import copy
import json
from io import StringIO

import pytest

from flightctl.client import RpcClient, rpc_stdin
from tests.contracts.validation import ContractError, validate_rpc
from tests.fakes.clock import FakeClock

from .test_client import ScriptedTransport, _grant, _response, _run


def _bridge(request, handler):
    transport = ScriptedTransport(handler)
    out, err = StringIO(), StringIO()
    code = rpc_stdin(
        transport=transport, clock=FakeClock(), stream_in=StringIO(json.dumps(request)),
        stream_out=out, stream_err=err,
    )
    return code, out.getvalue(), err.getvalue(), transport.calls


@pytest.mark.parametrize("case", ["instance", "batch-predecessor", "batch-dependency"])
def test_stdin_rejects_remaining_invalid_requests(case):
    request = RpcClient(clock=FakeClock()).make_request("claim", "lane-gpu0", {
        "token": "token-abcdefghijklmnop", "generation": 7,
        "generation_source": "authenticated-adoption",
    })
    if case == "instance":
        request["args"]["instance"] = []
    else:
        request["admission"]["batch"] = {
            "batch_id": "batch-a", "arms": [{
                "arm_id": "arm-a", "predecessor": "missing" if case == "batch-predecessor" else None,
                "dependencies": ["missing"] if case == "batch-dependency" else [],
            }], "dependencies": [], "registered_before_execution": True, "all_arms_visible": True,
        }
    with pytest.raises(ContractError):
        validate_rpc(request)
    code, out, err, calls = _bridge(request, lambda message, _: _grant(message, "claim"))
    assert code == 2
    assert calls == []
    assert out == ""
    assert "invalid RPC stdin" in err


@pytest.mark.parametrize("operation", ["status", "free", "report"])
def test_stdin_accepts_frozen_scheduler_wide_reads(operation):
    request = RpcClient(clock=FakeClock()).make_request("cal", None, {})
    request["op"] = operation
    validate_rpc(request)
    observation = {"certainty": "unknown", "reason": "not observed"}
    data = {
        "status": {"kind": "status", "lane": None, "state": "unknown", "generation": None,
                   "occupancy": observation, "reachability": observation},
        "free": {"kind": "projection", "scope": "free", "lane": None, "windows": [], "observation": observation},
        "report": {"kind": "report", "events": [], "next_cursor": None},
    }[operation]
    code, out, err, calls = _bridge(request, lambda message, _: _response(message, data))
    assert code == 0
    assert calls == [request]
    assert json.loads(out) == _response(request, data)
    assert err == ""


@pytest.mark.parametrize("binding", ["token", "generation", "reservation-generation", "principal", "mode"])
@pytest.mark.parametrize("surface", ["cli", "stdin"])
def test_acquire_never_exports_an_unbound_grant(binding, surface):
    def handler(message, _):
        response = copy.deepcopy(_grant(message))
        grant = response["data"]
        if binding == "token":
            grant["lease"]["token"] = "token-different-value"
        elif binding == "generation":
            grant["lease"]["generation"] += 1
        elif binding == "reservation-generation":
            grant["lease"]["reservation"]["generation"] += 1
        elif binding == "principal":
            grant["lease"]["principal"]["subject"] = "another-principal"
        else:
            grant["adoption"].update(mode="authenticated-adoption", token_source="authenticated-adoption")
        return response

    if surface == "cli":
        transport = ScriptedTransport(handler)
        code, out, err = _run(["--json", "acquire", "lane-gpu0", "purpose"], transport, clock=FakeClock())
        calls = transport.calls
    else:
        request = RpcClient(clock=FakeClock()).make_request("acquire", "lane-gpu0", {
            "purpose": "purpose", "class": "batch", "est_s": 60, "max_s": 60,
        })
        code, out, err, calls = _bridge(request, handler)
    assert code == 3
    assert json.loads(out)["data"] is None
    assert "token-" not in out
    assert err == ""
    assert [call["op"] for call in calls] == ["acquire"]


def test_calendar_does_not_accept_a_free_projection():
    data = {"kind": "projection", "scope": "free", "lane": None, "windows": [],
            "observation": {"certainty": "confirmed", "reason": None}}
    transport = ScriptedTransport(lambda message, _: _response(message, data))
    code, out, err = _run(["--json", "cal"], transport, clock=FakeClock())
    assert code == 3
    assert json.loads(out)["data"] is None
    assert err == ""


def test_stdin_grant_uses_the_envelope_principal():
    request = RpcClient(clock=FakeClock()).make_request("acquire", "lane-gpu0", {
        "purpose": "purpose", "class": "batch", "est_s": 60, "max_s": 60,
    })
    request["admission"]["ingress"]["actor"]["subject"] = "request-principal"

    def handler(message, _):
        response = _grant(message)
        response["data"]["lease"]["principal"] = copy.deepcopy(message["admission"]["ingress"]["actor"])
        return response

    code, out, err, calls = _bridge(request, handler)
    assert code == 0
    assert calls == [request]
    assert json.loads(out) == handler(request, 1)
    assert err == ""


@pytest.mark.parametrize("binding", ["token", "generation"])
def test_stdin_claim_never_exports_a_different_grant(binding):
    request = RpcClient(clock=FakeClock()).make_request("claim", "lane-gpu0", {
        "token": "token-abcdefghijklmnop", "generation": 7,
        "generation_source": "authenticated-adoption",
    })
    request["args"][binding] = "token-different-value" if binding == "token" else 8
    validate_rpc(request)
    code, out, err, calls = _bridge(request, lambda message, _: _grant(message, "claim"))
    assert code == 3
    assert json.loads(out)["data"] is None
    assert "token-" not in out
    assert calls == [request]
    assert err == ""


def test_unknown_calendar_window_stays_unknown():
    data = {"kind": "projection", "scope": "calendar", "lane": None,
            "windows": [{"start": "2026-09-27T20:00:00Z", "end": "2026-09-27T21:00:00Z",
                         "state": "unknown", "certainty": "unknown", "reason": "not observed"}],
            "observation": {"certainty": "unknown", "reason": "not observed"}}
    transport = ScriptedTransport(lambda message, _: _response(message, data))
    code, out, err = _run(["cal"], transport, clock=FakeClock())
    assert code == 0
    assert "state=unknown certainty=unknown" in out
    assert "state=free" not in out
    assert err == ""
