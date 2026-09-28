from __future__ import annotations

import json
import os
import subprocess
import copy
from io import StringIO
from pathlib import Path
from typing import Mapping

import pytest

from flightctl.client import RpcClient, validate_operation
from flightctl.flightctl import main
from tests.fakes.clock import FakeClock
from tests.fakes.ssh import FakeTransport as P0FakeTransport


def _response(message: Mapping[str, object], data: Mapping[str, object], status: int = 200, error=None) -> dict[str, object]:
    return {"schema": 1, "request_id": message["request_id"], "status": status, "data": dict(data) if status == 200 else None, "error": error}


def _mutation(message: Mapping[str, object], operation: str, state: str = "free") -> dict[str, object]:
    return _response(
        message,
        {
            "kind": "mutation",
            "operation": operation,
            "record_type": "lease",
            "record_id": "record-a",
            "state": state,
            "revision": 1,
            "reservation": {"lane": None, "generation": 7, "state": "released"},
        },
    )


def _grant(message: Mapping[str, object], operation: str = "acquire", lane: str = "lane-gpu0", generation: int = 7) -> dict[str, object]:
    token = "token-abcdefghijklmnop"
    mode = "authenticated-adoption" if operation == "claim" else "fresh-acquire"
    lane_ref = {"site_id": "site", "host_id": "host", "lane_id": lane}
    principal = {"site_id": "site", "tenant_id": "tenant", "issuer": "client", "subject": "client"}
    state = "running" if operation == "claim" else "starting"
    lease = {
        "schema_version": 1,
        "lease_id": "lease-a",
        "lane": lane_ref,
        "generation": generation,
        "reservation": {"lane": lane_ref, "generation": generation, "state": state},
        "token": token,
        "instance": "instance-a",
        "principal": principal,
        "class": "batch",
        "purpose": "purpose",
        "estimated_s": 60,
        "started_at": "2026-09-27T20:00:00Z",
        "max_end": "2026-09-27T21:00:00Z",
        "approved_max_end": "2026-09-27T21:00:00Z",
        "heartbeat_at": "2026-09-27T20:01:00Z",
        "deadline": {"boot_id": "boot-a", "deadline_s": 3600, "utc_anchor": "2026-09-27T20:00:00Z", "monotonic_anchor_s": 10},
        "booking_id": None,
        "unit": "unit-a",
        "invocation": "invoke-a",
        "state": state,
    }
    return _response(
        message,
        {
            "kind": "grant",
            "operation": operation,
            "token": token,
            "generation": generation,
            "lease": lease,
            "reservation": {"lane": lane_ref, "generation": generation, "state": state},
            "adoption": {"mode": mode, "principal_bound": True, "generation_bound": True, "token_source": "authenticated-adoption" if operation == "claim" else "controller-grant"},
        },
    )


class ScriptedTransport:
    def __init__(self, handler):
        self.handler = handler
        self._fake = P0FakeTransport()
        self.calls: list[dict[str, object]] = []

    def request(self, endpoint, message, timeout_s):
        copied = json.loads(json.dumps(message))
        self.calls.append(copied)
        response = self.handler(copied, len(self.calls))
        self._fake.queue("success", response=response)
        return self._fake.request(endpoint, copied, timeout_s)


def _run(argv, transport, *, clock=None, handoff=None, **kwargs):
    stdout, stderr = StringIO(), StringIO()
    code = main(argv, transport=transport, clock=clock, handoff=handoff, stdout=stdout, stderr=stderr, **kwargs)
    return code, stdout.getvalue(), stderr.getvalue()


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["acquire", "lane-gpu0", "quoted purpose", "2"], {"op": "acquire", "est_s": 120, "max_s": 120}),
        (["acquire", "lane-gpu0", "quoted purpose"], {"op": "acquire", "est_s": 14400, "max_s": 14400}),
        (["renew", "lane-gpu0", "token-abcdefghijklmnop", "15"], {"op": "renew", "extend_s": 900}),
        (["renew", "lane-gpu0", "token-abcdefghijklmnop"], {"op": "renew", "extend_s": 14400}),
        (["release", "lane-gpu0", "token-abcdefghijklmnop"], {"op": "release", "token": "token-abcdefghijklmnop"}),
    ],
)
def test_positional_compatibility(argv, expected):
    transport = ScriptedTransport(lambda message, _: _mutation(message, str(message["op"])))
    code, _, err = _run(argv, transport)
    assert code in {0, 1}
    assert not err
    assert transport.calls[0]["op"] == expected["op"]
    for key, value in expected.items():
        if key != "op":
            assert transport.calls[0]["args"][key] == value


def test_invalid_positional_does_not_call_transport():
    transport = ScriptedTransport(lambda message, _: _mutation(message, str(message["op"])))
    for argv in (
        ["release", "lane-gpu0", "token-abcdefghijklmnop", "7"],
        ["acquire", "lane-gpu0", "purpose", "nope"],
        ["renew", "lane-gpu0", "token-abcdefghijklmnop", "-1"],
        ["acquire", "", "purpose"],
        ["acquire", "lane-gpu0", ""],
    ):
        code, _, _ = _run(argv, transport)
        assert code == 2
    assert transport.calls == []


def test_token_mutations_are_token_only():
    transport = ScriptedTransport(lambda message, _: _mutation(message, str(message["op"])))
    _run(["renew", "lane-gpu0", "token-abcdefghijklmnop", "2"], transport)
    _run(["release", "lane-gpu0", "token-abcdefghijklmnop"], transport)
    for call in transport.calls:
        assert set(call["args"]) == {"token", "extend_s"} if call["op"] == "renew" else set(call["args"]) == {"token"}
        assert "generation" not in call["args"]


def test_fresh_and_adopt():
    transport = ScriptedTransport(lambda message, _: _grant(message) if message["op"] == "acquire" else _mutation(message, "release"))
    handoffs = []
    code, _, _ = _run(
        ["run", "lane-gpu0", "purpose", "--est", "2", "--max", "5", "--", "workload", "literal;arg"],
        transport,
        handoff=lambda grant, workload: handoffs.append((grant, workload)) or True,
    )
    assert code == 0
    assert [call["op"] for call in transport.calls] == ["acquire", "release"]
    assert handoffs[0][1] == ("workload", "literal;arg")

    transport = ScriptedTransport(lambda message, _: _grant(message, "claim") if message["op"] == "claim" else _mutation(message, "release"))
    env_before = dict(os.environ)
    try:
        os.environ["LANE_TOKEN"] = "token-abcdefghijklmnop"
        os.environ["LANE_GENERATION"] = "7"
        code, _, _ = _run(["run", "lane-gpu0", "purpose", "--", "workload"], transport, handoff=lambda *_: True)
    finally:
        os.environ.clear()
        os.environ.update(env_before)
    assert code == 0
    assert [call["op"] for call in transport.calls] == ["claim", "release"]
    assert transport.calls[0]["args"] == {"token": "token-abcdefghijklmnop", "generation": 7, "generation_source": "authenticated-adoption"}


def test_bad_adoption_has_no_transport_or_handoff():
    transport = ScriptedTransport(lambda message, _: _grant(message, "claim"))
    env_before = dict(os.environ)
    try:
        os.environ["LANE_TOKEN"] = "token-abcdefghijklmnop"
        os.environ.pop("LANE_GENERATION", None)
        code, _, _ = _run(["run", "lane-gpu0", "purpose", "--", "workload"], transport, handoff=lambda *_: pytest.fail("handoff called"))
    finally:
        os.environ.clear()
        os.environ.update(env_before)
    assert code == 2
    assert transport.calls == []


def test_failed_acquire_has_no_workload_or_release():
    error = {"code": "unknown", "message": "controller unavailable", "retryable": True, "failure_class": "transport"}
    transport = ScriptedTransport(lambda message, _: _response(message, {}, 503, error))
    handoffs = []
    code, _, _ = _run(["run", "lane-gpu0", "purpose", "--", "workload"], transport, handoff=lambda *args: handoffs.append(args))
    assert code == 3
    assert [call["op"] for call in transport.calls] == ["acquire"]
    assert handoffs == []


def test_transport_failure_replay():
    transport = P0FakeTransport(
        [
            {"outcome": "lost", "error": "reply lost"},
            {"outcome": "success", "response": _mutation({"request_id": "req-original"}, "release")},
        ]
    )
    code, _, _ = _run(
        ["release", "lane-gpu0", "token-abcdefghijklmnop"],
        transport,
        request_id_factory=lambda: "req-original",
    )
    assert code == 0
    assert len(transport.calls) == 2
    first = transport.calls[0]["message"]
    second = transport.calls[1]["message"]
    assert first["request_id"] == "req-original"
    assert first["request_id"] == second["request_id"]
    assert first["request_fingerprint"] == second["request_fingerprint"]


def test_https_transport_uses_injected_opener_and_fails_closed():
    from flightctl.client import HttpTransport, TransportFailure

    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"schema":1,"request_id":"req-a","status":503,"data":null,"error":null}'

    def opener(request, timeout):
        seen["url"] = request.full_url
        seen["timeout"] = timeout
        return Response()

    response = HttpTransport(opener=opener).request("https://controller.invalid/v1/rpc", {"schema": 1}, 2.5)
    assert response["status"] == 503
    assert seen == {"url": "https://controller.invalid/v1/rpc", "timeout": 2.5}

    class BadResponse(Response):
        def read(self):
            return b"not-json"

    with pytest.raises(TransportFailure):
        HttpTransport(opener=lambda request, timeout: BadResponse()).request("https://controller.invalid/v1/rpc", {}, 1.0)


def test_bounded_wait():
    clock = FakeClock()
    sequence = []

    def handler(message, number):
        sequence.append((message["op"], message["args"]))
        if message["op"] == "queue" and message["args"]["action"] == "add":
            return _mutation(message, "queue", "queued") | {"data": {"kind": "mutation", "operation": "queue", "record_type": "queue", "record_id": "queue-a", "state": "queued", "revision": 1, "reservation": {"lane": None, "generation": None, "state": "unassigned"}}}
        if message["op"] == "acquire":
            return {
                "schema": 1,
                "request_id": message["request_id"],
                "status": 409,
                "data": None,
                "error": {"code": "busy", "message": "lane is occupied", "retryable": True, "failure_class": "conflict"},
            }
        return _mutation(message, "queue", "queued")

    transport = ScriptedTransport(handler)
    code, _, _ = _run(
        ["wait", "lane-gpu0", "purpose", "1", "2"],
        transport,
        clock=clock,
        sleeper=lambda seconds: clock.advance(utc_s=seconds, monotonic_s=seconds),
    )
    assert code == 1
    assert [op for op, _ in sequence] == ["queue", "acquire", "queue", "acquire", "queue"]
    assert sequence[2][1] == {"action": "refresh", "queue_id": "queue-a", "max_wait_s": 120}
    assert sequence[-1][1] == {"action": "remove", "queue_id": "queue-a"}


def test_queue_failure_does_not_remove_unknown_entry():
    error = {"code": "unknown", "message": "queue unavailable", "retryable": True, "failure_class": "transport"}
    transport = ScriptedTransport(lambda message, _: _response(message, {}, 503, error))
    code, _, _ = _run(["wait", "lane-gpu0", "purpose", "1", "1"], transport)
    assert code == 3
    assert len(transport.calls) == 1


def test_json_status_is_unchanged_and_unknown_is_not_free():
    envelope = {
        "schema": 1,
        "request_id": "server-request",
        "status": 503,
        "data": None,
        "error": {"code": "unknown", "message": "unknown state", "retryable": True, "failure_class": "state"},
    }

    class ResponseTransport:
        def __init__(self): self.calls = []
        def request(self, endpoint, message, timeout_s):
            self.calls.append(message)
            return {**envelope, "request_id": message["request_id"]}

    transport = ResponseTransport()
    code, out, err = _run(["--json", "cal", "lane-gpu0"], transport)
    assert code == 3
    assert json.loads(out)["data"] is None
    assert json.loads(out)["error"]["code"] == "unknown"
    assert err == ""


def test_rpc_stdin_bridge():
    transport = ScriptedTransport(lambda message, _: _mutation(message, "queue", "queued"))
    client = RpcClient(transport)
    request = client.make_request("queue", "lane-gpu0", {"action": "list"})
    from flightctl.client import rpc_stdin

    stream_out, stream_err = StringIO(), StringIO()
    code = rpc_stdin(transport=transport, stream_in=StringIO(json.dumps(request)), stream_out=stream_out, stream_err=stream_err)
    assert code == 0
    assert len(transport.calls) == 1
    assert transport.calls[0] == request
    assert json.loads(stream_out.getvalue())["request_id"] == request["request_id"]
    assert stream_err.getvalue() == ""


def test_rpc_stdin_bridge_accepts_complete_batch_registration():
    from flightctl.client import rpc_stdin

    admission = _vector_admission()
    admission["batch"] = {
        "batch_id": "batch-a",
        "arms": [{"arm_id": "arm-a", "predecessor": None, "dependencies": []}],
        "dependencies": [],
        "registered_before_execution": True,
        "all_arms_visible": True,
    }
    request = RpcClient(admission=admission).make_request("queue", "lane-gpu0", {"action": "list"})
    transport = ScriptedTransport(lambda message, _: _mutation(message, "queue", "queued"))
    out, err = StringIO(), StringIO()
    code = rpc_stdin(transport=transport, stream_in=StringIO(json.dumps(request)), stream_out=out, stream_err=err)
    assert code == 0
    assert transport.calls == [request]
    assert json.loads(out.getvalue())["status"] == 200
    assert err.getvalue() == ""


def test_malformed_rpc_stdin_makes_zero_calls():
    from flightctl.client import rpc_stdin

    transport = ScriptedTransport(lambda message, _: pytest.fail("transport called"))
    out, err = StringIO(), StringIO()
    assert rpc_stdin(transport=transport, stream_in=StringIO("{\"schema\": 1}"), stream_out=out, stream_err=err) == 2
    assert transport.calls == []


def test_discover_dispatch(tmp_path):
    proposal = {"schema_version": 1, "status": "needs_review"}
    received = []

    def handler(options):
        received.append(dict(options))
        return proposal

    output = tmp_path / "proposal.json"
    transport = ScriptedTransport(lambda message, _: pytest.fail("RPC called"))
    code, out, err = _run(["discover", "--output", str(output), "--current", "inventory.json"], transport, discovery_handler=handler)
    assert code == 0
    assert received == [{"output": str(output), "current": "inventory.json"}]
    assert json.loads(output.read_text()) == proposal
    assert err == ""
    assert "proposal written to" in out


def test_discover_dispatch_without_handler_is_unavailable():
    transport = ScriptedTransport(lambda message, _: pytest.fail("RPC called"))
    code, out, err = _run(["discover", "--output", "proposal.json"], transport)
    assert code == 3
    assert out == ""
    assert err.startswith("flightctl: unavailable:")
    assert transport.calls == []


def test_client_mutations():
    transport = ScriptedTransport(lambda message, _: _mutation(message, str(message["op"])))
    code, _, _ = _run(["acquire", "lane-gpu0", "purpose", "2"], transport)
    assert code == 0
    assert transport.calls[0]["args"]["est_s"] == 120
    assert "generation" not in transport.calls[0]["args"]

    unknown = {"schema": 1, "request_id": "placeholder", "status": 503, "data": None, "error": {"code": "unknown", "message": "unknown", "retryable": True, "failure_class": "state"}}
    unknown_transport = ScriptedTransport(lambda message, _: {**unknown, "request_id": message["request_id"]})
    code, _, _ = _run(["status", "lane-gpu0"], unknown_transport)
    assert code == 3

    failed = {"schema": 1, "request_id": "placeholder", "status": 503, "data": None, "error": {"code": "unknown", "message": "failed", "retryable": True, "failure_class": "transport"}}
    failed_transport = ScriptedTransport(lambda message, _: {**failed, "request_id": message["request_id"]})
    handoffs = []
    code, _, _ = _run(["run", "lane-gpu0", "purpose", "--", "workload"], failed_transport, handoff=lambda *args: handoffs.append(args))
    assert code == 3
    assert handoffs == []
    assert [call["op"] for call in failed_transport.calls] == ["acquire"]


def test_shell_quoting(tmp_path):
    wrapper = Path(__file__).parents[2] / "lanes.sh"
    assert wrapper.stat().st_mode & 0o111
    result = subprocess.run([str(wrapper), "--help"], text=True, capture_output=True, check=False)
    assert result.returncode == 0
    assert "flightctl acquire" in result.stdout

    shim = Path(__file__).with_name("cli_shim.py")
    marker = tmp_path / "SHOULD_NOT_EXIST"
    purpose = f'spaces "quotes" ; $(touch {marker}) *'
    env = dict(os.environ, FLIGHTCTL_PYTHON=str(shim))
    acquire = subprocess.run(
        [str(wrapper), "acquire", "lane-gpu0", purpose, "2"],
        cwd=wrapper.parent,
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert acquire.returncode == 0
    acquire_payload = json.loads(acquire.stdout)
    assert acquire_payload["calls"][0]["message"]["args"]["purpose"] == purpose
    assert not marker.exists()

    workload = ["workload name", "literal;arg", f"$(touch {marker})", "*"]
    run_result = subprocess.run(
        [str(wrapper), "run", "lane-gpu0", purpose, "--", *workload],
        cwd=wrapper.parent,
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert run_result.returncode == 0
    run_payload = json.loads(run_result.stdout)
    assert run_payload["handoffs"][0][1] == workload
    assert not marker.exists()


@pytest.mark.parametrize("missing", ["principal", "reservation", "unit", "invocation"])
def test_incomplete_grant_never_handoffs_or_releases(missing):
    def handler(message, _):
        if message["op"] == "acquire":
            grant = _grant(message)
            del grant["data"]["lease"][missing]
            return grant
        return _mutation(message, "release")

    transport = ScriptedTransport(handler)
    handoffs = []
    code, _, _ = _run(["run", "lane-gpu0", "purpose", "--", "workload"], transport, handoff=lambda *args: handoffs.append(args))
    assert code == 3
    assert handoffs == []
    assert [call["op"] for call in transport.calls] == ["acquire"]


@pytest.mark.parametrize("binding", ["principal", "generation", "lane", "adoption"])
def test_unbound_grant_never_handoffs_or_releases(binding):
    def handler(message, _):
        if message["op"] == "acquire":
            grant = _grant(message)
            data = grant["data"]
            if binding == "principal":
                data["lease"]["principal"]["subject"] = "different-subject"
            elif binding == "generation":
                data["lease"]["generation"] = 8
            elif binding == "lane":
                data["lease"]["lane"]["lane_id"] = "other-lane"
            else:
                data["adoption"]["principal_bound"] = False
            return grant
        return _mutation(message, "release")

    transport = ScriptedTransport(handler)
    handoffs = []
    code, _, _ = _run(
        ["run", "lane-gpu0", "purpose", "--", "workload"],
        transport,
        handoff=lambda *args: handoffs.append(args),
    )
    assert code == 3
    assert handoffs == []
    assert [call["op"] for call in transport.calls] == ["acquire"]


def test_token_bearing_read_and_incomplete_status_are_unavailable():
    secret = "synthetic-secret-token"
    lane_ref = {"site_id": "site", "host_id": "host", "lane_id": "lane-gpu0"}
    read_lease = {
        "schema_version": 1,
        "lease_id": "lease-a",
        "lane": lane_ref,
        "generation": 7,
        "reservation": {"lane": lane_ref, "generation": 7, "state": "running"},
        "instance": "instance-a",
        "principal": {"site_id": "site", "tenant_id": "tenant", "issuer": "client", "subject": "client"},
        "class": "batch",
        "purpose": "purpose",
        "state": "running",
        "token": secret,
        "token_redacted": True,
    }
    occupancy = _response(
        {"request_id": "req-read"},
        {
            "kind": "occupancy",
            "lane": lane_ref,
            "state": "running",
            "generation": 7,
            "lease": read_lease,
            "occupant": None,
            "observation": {"certainty": "confirmed", "reason": None},
        }
    )
    transport = ScriptedTransport(lambda message, _: {**occupancy, "request_id": message["request_id"]})
    code, out, err = _run(["--json", "status", "lane-gpu0"], transport)
    assert code == 3
    assert secret not in out
    assert err == ""
    assert json.loads(out)["status"] == 503

    malformed = _response({"request_id": "req-status"}, {"kind": "status"})
    transport = ScriptedTransport(lambda message, _: {**malformed, "request_id": message["request_id"]})
    code, out, _ = _run(["status", "lane-gpu0"], transport)
    assert code == 3
    assert "status=None" not in out


def _vector_admission():
    vector = json.loads((Path(__file__).parents[2] / "tests" / "contracts" / "vectors" / "rpc.json").read_text())
    return copy.deepcopy(vector["request_defaults"]["admission"])


@pytest.mark.parametrize("mutation", ["batch", "delegation", "pipeline", "signed_manifest", "proof"])
def test_rpc_stdin_rejects_invalid_nested_security_metadata(mutation):
    from flightctl.client import rpc_stdin

    admission = _vector_admission()
    client = RpcClient(admission=admission)
    if mutation == "pipeline":
        request = client.make_request("chat-load", "lane-gpu0", {"pipeline_ref": "interactive", "purpose": "interactive inference"})
        request["admission"]["pipeline"] = {}
    elif mutation == "signed_manifest":
        request = client.make_request("acquire", "lane-gpu0", {"purpose": "purpose", "class": "batch", "est_s": 60, "max_s": 60, "signed_manifest": None})
        request["args"]["signed_manifest"] = {}
    elif mutation == "proof":
        request = client.make_request(
            "approve",
            None,
            {
                "approval_id": "approval-a",
                "proof": {"scheme": "ssh-sk", "key_id": "key-a", "namespace": "flightctl/approval/v1", "encoding": "openssh-ssh-sk-signature/base64", "signature_b64": "c2ln"},
                "evidence": {"verifier": "controller-a", "verified_at": "2026-09-27T20:01:00Z", "user_presence": "verified", "user_verification": "verified"},
            },
        )
        request["args"]["proof"] = {}
    else:
        request = client.make_request("queue", "lane-gpu0", {"action": "list"})
        request["admission"][mutation] = {}
    transport = ScriptedTransport(lambda message, _: pytest.fail("transport called"))
    out, err = StringIO(), StringIO()
    code = rpc_stdin(transport=transport, stream_in=StringIO(json.dumps(request)), stream_out=out, stream_err=err)
    assert code == 2
    assert transport.calls == []


def test_injected_pipeline_binding_is_preserved():
    admission = _vector_admission()
    admission["pipeline"] = {"pipeline_id": "interactive", "version": "2.0.0", "revision": 8, "purpose": "interactive inference", "policy_hash": "a" * 64}
    request = RpcClient(admission=admission).make_request("chat-load", "lane-gpu0", {"pipeline_ref": "interactive", "purpose": "interactive inference"})
    assert request["admission"]["pipeline"] == admission["pipeline"]


def test_wait_deadline_starts_before_queue_and_clips_transport_timeout():
    clock = FakeClock()
    calls = []
    timeouts = []

    class DeadlineTransport:
        def request(self, endpoint, message, timeout_s):
            calls.append(message)
            timeouts.append(timeout_s)
            if message["op"] == "queue" and message["args"]["action"] == "add":
                clock.advance(utc_s=14, monotonic_s=14)
                return _mutation(message, "queue", "queued")
            if message["op"] == "queue" and message["args"]["action"] == "remove":
                return _mutation(message, "queue", "removed")
            return _grant(message)

    response, _ = RpcClient(DeadlineTransport(), clock=clock).wait_for_lane("lane-gpu0", "purpose", max_wait_s=10)
    assert response["status"] == 409
    assert response["error"]["code"] == "timeout"
    assert [message["op"] for message in calls] == ["queue", "queue"]
    assert calls[-1]["args"]["action"] == "remove"
    assert timeouts[0] <= 10
    assert timeouts[-1] == 0


def test_busy_wait_uses_a_new_admission_request_id_after_refresh():
    clock = FakeClock()
    first_ids = []
    cached = {}

    def handler(message, _):
        if message["op"] == "queue":
            return _mutation(message, "queue", "queued")
        request_id = message["request_id"]
        if request_id not in cached:
            first_ids.append(request_id)
            cached[request_id] = _response(message, {}, 409, {"code": "busy", "message": "busy", "retryable": True, "failure_class": "conflict"}) if clock.monotonic() < 60 else _grant(message)
        return cached[request_id]

    transport = ScriptedTransport(handler)
    response, _ = RpcClient(
        transport,
        clock=clock,
        sleeper=lambda seconds: clock.advance(utc_s=seconds, monotonic_s=seconds),
    ).wait_for_lane("lane-gpu0", "purpose", max_wait_s=120)
    acquire_ids = [call["request_id"] for call in transport.calls if call["op"] == "acquire"]
    assert response["status"] == 200
    assert len(first_ids) == 2
    assert acquire_ids[0] != acquire_ids[1]
    assert clock.monotonic() == 60


def test_calendar_renders_windows_and_configured_timezone(monkeypatch):
    monkeypatch.setenv("FLIGHTCTL_TIMEZONE", "Europe/London")
    response = _response(
        {"request_id": "req-calendar"},
        {
            "kind": "projection",
            "scope": "calendar",
            "lane": {"site_id": "site", "host_id": "host", "lane_id": "lane-gpu0"},
            "windows": [{"start": "2026-09-28T10:00:00Z", "end": "2026-09-28T11:00:00Z", "state": "booked", "certainty": "estimate", "reason": "scheduled"}],
            "observation": {"certainty": "unknown", "reason": "host state unknown"},
        }
    )
    transport = ScriptedTransport(lambda message, _: {**response, "request_id": message["request_id"]})
    code, out, err = _run(["cal", "lane-gpu0"], transport)
    assert code == 0
    assert err == ""
    assert "calendar certainty=unknown timezone=Europe/London" in out
    assert "window 2026-09-28T11:00:00+01:00..2026-09-28T12:00:00+01:00 state=booked" in out


def test_p0_fake_transport_failures_never_handoff_or_release():
    from tests.fakes.clock import FakeClock as P0Clock
    from tests.fakes.ssh import FakeTransport

    for outcome in ("denied", "timeout", "lost", "delayed"):
        transport = FakeTransport([{"outcome": outcome}, {"outcome": outcome}])
        handoffs = []
        code, _, _ = _run(["run", "lane-gpu0", "purpose", "--", "workload"], transport, clock=P0Clock(), handoff=lambda *args: handoffs.append(args))
        assert code == 3
        assert handoffs == []
        assert len(transport.calls) <= 2
