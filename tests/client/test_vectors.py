from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

from flightctl.client import (
    InvalidRequest,
    InvalidResponse,
    RpcClient,
    _fingerprint,
    canonical_local_action_bytes,
    local_action_payload_hash,
    local_action_projection,
    rpc_stdin,
    validate_operation,
    validate_response,
)
from flightctl.flightctl import main
import pytest

from .test_client import ScriptedTransport


ROOT = Path(__file__).parents[2]


class VectorClock:
    def utc(self):
        return datetime(2026, 9, 27, 20, 0, tzinfo=timezone.utc)

    def monotonic(self):
        return 0.0

    def boot_id(self):
        return "boot-a"


def _mutation(operation: str = "queue", state: str = "queued") -> dict[str, object]:
    return {
        "schema": 1,
        "request_id": "vector-request",
        "status": 200,
        "data": {
            "kind": "mutation",
            "operation": operation,
            "record_type": "queue",
            "record_id": "queue-a",
            "state": state,
            "revision": 1,
            "reservation": {"lane": None, "generation": None, "state": "unassigned"},
        },
        "error": None,
    }


def _grant(vector: dict[str, object], operation: str, case_index: int) -> dict[str, object]:
    cases = vector["cases"]
    assert isinstance(cases, list)
    response = copy.deepcopy(cases[case_index]["response"])
    assert isinstance(response, dict)
    data = response["data"]
    assert isinstance(data, dict)
    data["operation"] = operation
    return response


def test_all_cli_vectors(monkeypatch: pytest.MonkeyPatch):
    vector = json.loads((ROOT / "tests" / "contracts" / "vectors" / "cli.json").read_text(encoding="utf-8"))
    rpc = json.loads((ROOT / "tests" / "contracts" / "vectors" / "rpc.json").read_text(encoding="utf-8"))
    admission = rpc["request_defaults"]["admission"]

    for case in vector["cases"]:
        monkeypatch.delenv("LANE_TOKEN", raising=False)
        monkeypatch.delenv("LANE_GENERATION", raising=False)
        for name, value in case.get("env", {}).items():
            monkeypatch.setenv(name, value)
        responses = []
        expected = case["expected_rpc"]
        for index, request in enumerate(expected):
            if case["name"].startswith("run fresh") and index == 0:
                responses.append(_grant(vector, "acquire", 0))
            elif case["name"].startswith("run authenticated") and index == 0:
                responses.append(_grant(vector, "claim", 11))
            elif case["name"].startswith("run authenticated") and index == 1:
                responses.append(_mutation("release", "free"))
            elif request["op"] == "queue" and request["args"].get("action") == "add" and len(expected) > 1:
                responses.append(_mutation())
            elif case["name"] == "yield":
                responses.append(_mutation())
            else:
                responses.append(case["response"])

        def response_for(message, index):
            response = copy.deepcopy(responses[index - 1])
            response["request_id"] = message["request_id"]
            data = response.get("data")
            if isinstance(data, dict) and data.get("kind") == "pending":
                data["request_id"] = message["request_id"]
            return response

        transport = ScriptedTransport(response_for)
        stdout, stderr = StringIO(), StringIO()
        code = main(
            case["argv"],
            transport=transport,
            clock=VectorClock(),
            admission=admission,
            token_lookup=lambda lane: "token-abcdefghijklmnop",
            handoff=lambda grant, workload: True,
            sleeper=lambda seconds: None,
            stdout=stdout,
            stderr=stderr,
        )
        assert code == case["exit"], case["name"]
        assert [call["op"] for call in transport.calls] == [item["op"] for item in expected], case["name"]
        assert [call["lane"] for call in transport.calls] == [item["lane"] for item in expected], case["name"]
        actual_args = [call["args"] for call in transport.calls]
        expected_args = copy.deepcopy([item["args"] for item in expected])
        for actual, wanted in zip(actual_args, expected_args):
            if actual.get("queue_id") == "queue-a" and wanted.get("queue_id") == "queue-from-first-result":
                wanted["queue_id"] = "queue-a"
        assert actual_args == expected_args, case["name"]
        for call in transport.calls:
            assert set(call) == {"schema", "request_id", "op", "lane", "args", "idempotency_scope", "request_fingerprint", "admission"}
            assert call["schema"] == 1
            assert call["idempotency_scope"] == {"scope": "authenticated-principal", "controller_id": "controller-a"}
            assert call["request_fingerprint"] == _fingerprint(call["op"], call["lane"], call["args"])
            assert call["admission"]["execution"] == "atomic"
            validate_operation(call)
        expected_messages = {
            "lane-first positional acquire quoted purpose": "acquired lane-gpu0 generation=7 token=token-abcdefghijklmnop\n",
            "lane-first positional release with server generation lookup": "release free\n",
            "lane-first positional renew TTL minutes to seconds": "flightctl: approved maximum reached\n",
            "lane-first wait converts TTL and wait minutes": "flightctl: pending: lane is occupied\n",
            "default TTL and bounded wait": "flightctl: pending: lane is occupied\n",
            "bounded wait timeout": "flightctl: bounded wait expired\n",
            "calendar projection": "calendar certainty=estimate timezone=UTC reason=future schedule projection\n",
            "cancel": "cancel cancelled\n",
            "book": "flightctl: approval required\n",
            "run fresh acquire and release sequence": "release free\n",
            "run authenticated adoption and release sequence": "release free\n",
            "approve": "approve approved\n",
            "chat load": "chat-load loading\n",
            "preempt unknown": "flightctl: lane state unknown\n",
            "yield": "queue queued\n",
        }
        if case["name"] == "json free read":
            expected_json = copy.deepcopy(responses[-1])
            expected_json["request_id"] = transport.calls[-1]["request_id"]
            assert json.loads(stdout.getvalue()) == expected_json
            assert stderr.getvalue() == ""
        elif case["name"] == "yield":
            assert stdout.getvalue() == expected_messages[case["name"]]
            assert stderr.getvalue() == ""
        elif case["exit"] == 0:
            assert stdout.getvalue() == expected_messages[case["name"]]
            assert stderr.getvalue() == ""
        else:
            assert stdout.getvalue() == ""
            assert stderr.getvalue() == expected_messages[case["name"]]


@pytest.mark.parametrize("status", [200, 202, 403, 409, 503])
def test_status_exit_json_is_an_unchanged_envelope(status):
    lane = {"site_id": "site-a", "host_id": "host-1", "lane_id": "lane-gpu0"}
    if status == 200:
        data = {"kind": "status", "lane": lane, "state": "unknown", "generation": None, "occupancy": {"certainty": "unknown", "reason": "not observed"}, "reachability": {"certainty": "unknown", "reason": "not observed"}}
        error = None
    elif status == 202:
        data = {"kind": "pending", "operation": "queue", "request_id": "placeholder", "queue_id": "queue-a", "retry_after_s": 60, "wait_deadline": {"boot_id": "boot-a", "deadline_s": 600, "utc_anchor": "2026-09-27T20:00:00Z", "monotonic_anchor_s": 10}, "reason": "lane is occupied"}
        error = None
    else:
        data = None
        error = {"code": {403: "denied", 409: "conflict", 503: "unknown"}[status], "message": "scripted response", "retryable": status == 503, "failure_class": "policy" if status == 403 else "conflict" if status == 409 else "state"}
    template = {"schema": 1, "request_id": "placeholder", "status": status, "data": data, "error": error}

    class Transport:
        def __init__(self):
            self.calls = []

        def request(self, endpoint, message, timeout_s):
            self.calls.append(copy.deepcopy(message))
            response = copy.deepcopy(template)
            response["request_id"] = message["request_id"]
            if isinstance(response.get("data"), dict) and response["data"].get("kind") == "pending":
                response["data"]["request_id"] = message["request_id"]
            return response

    transport = Transport()
    stdout, stderr = StringIO(), StringIO()
    if status == 202:
        request = RpcClient(clock=VectorClock()).make_request("queue", "lane-gpu0", {"action": "list"})
        code = rpc_stdin(
            transport=transport,
            clock=VectorClock(),
            stream_in=StringIO(json.dumps(request)),
            stream_out=stdout,
            stream_err=stderr,
        )
    else:
        code = main(["--json", "status", "lane-gpu0"], transport=transport, clock=VectorClock(), stdout=stdout, stderr=stderr)
    assert code == {200: 0, 202: 5, 403: 2, 409: 1, 503: 3}[status]
    expected = copy.deepcopy(template)
    expected["request_id"] = transport.calls[0]["request_id"]
    if isinstance(expected.get("data"), dict) and expected["data"].get("kind") == "pending":
        expected["data"]["request_id"] = expected["request_id"]
    assert json.loads(stdout.getvalue()) == expected
    assert stderr.getvalue() == ""


def test_p01_pending_release_vector_runs_through_cli():
    vector = json.loads((ROOT / "tests" / "contracts" / "vectors" / "p0-1.json").read_text(encoding="utf-8"))
    case = vector["pending_release"]
    valid = copy.deepcopy(case["valid"])

    def handler(message, _):
        response = copy.deepcopy(valid)
        response["request_id"] = message["request_id"]
        response["data"]["request_id"] = message["request_id"]
        return response

    transport = ScriptedTransport(handler)
    stdout, stderr = StringIO(), StringIO()
    code = main(
        ["--json", "release", "lane-gpu0", "token-abcdefghijklmnop"],
        transport=transport,
        clock=VectorClock(),
        stdout=stdout,
        stderr=stderr,
    )
    assert code == 5
    payload = json.loads(stdout.getvalue())
    assert payload["status"] == 202
    assert payload["request_id"] == transport.calls[0]["request_id"]
    assert payload["data"]["operation"] == "release"
    assert stderr.getvalue() == ""
    assert transport.calls[0]["args"] == {"token": "token-abcdefghijklmnop"}

    for invalid in case["invalid"]:
        with pytest.raises(InvalidResponse):
            validate_response(invalid, invalid["request_id"], operation="release", lane="lane-gpu0")


def test_p01_owner_release_and_forced_preemption_stay_distinct():
    vector = json.loads((ROOT / "tests" / "contracts" / "vectors" / "p0-1.json").read_text(encoding="utf-8"))
    owner_release = vector["owner_release"]["valid"]
    assert owner_release["stop_authority"] == {"mode": "owner-release", "approval_id": None}

    rpc = json.loads((ROOT / "tests" / "contracts" / "vectors" / "rpc.json").read_text(encoding="utf-8"))
    client = RpcClient(admission=rpc["request_defaults"]["admission"])
    release = client.make_request("release", "lane-gpu0", {"token": "token-abcdefghijklmnop"})
    preempt = client.make_request("preempt", "lane-gpu0", {"token": "token-abcdefghijklmnop", "approval_id": "approval-a"})
    assert release["args"] == {"token": "token-abcdefghijklmnop"}
    assert release["admission"]["approval"] == {"approval_id": None, "required": False, "consume_atomically": True}
    assert "stop_authority" not in release["args"]
    assert preempt["args"] == {"token": "token-abcdefghijklmnop", "approval_id": "approval-a"}
    assert preempt["admission"]["approval"] == {"approval_id": "approval-a", "required": True, "consume_atomically": True}


def test_p01_content_labels_are_admission_only_and_policy_denial_is_server_result():
    vector = json.loads((ROOT / "tests" / "contracts" / "vectors" / "p0-1.json").read_text(encoding="utf-8"))
    cases = vector["content_labels"]
    valid = cases["valid"]
    request = RpcClient(admission=valid["admission"]).make_request(valid["op"], valid["lane"], valid["args"], request_id=valid["request_id"])
    assert request["admission"]["content_labels"] == ["acceptable-use"]
    assert "content_labels" not in request["args"]
    validate_operation(request)

    denied = cases["policy_denied"]
    denied_client = RpcClient(
        ScriptedTransport(
            lambda message, _: {
                "schema": 1,
                "request_id": message["request_id"],
                "status": 403,
                "data": None,
                "error": {
                    "code": "denied",
                    "message": "content label rejected",
                    "retryable": False,
                    "failure_class": "policy",
                },
            }
        ),
        admission=denied["admission"],
    )
    denied_request = denied_client.make_request(denied["op"], denied["lane"], denied["args"], request_id=denied["request_id"])
    assert denied_request["admission"]["content_labels"] == ["disallowed"]
    denied_response = denied_client.request(denied_request)
    assert denied_response["status"] == 403
    for labels in (cases["invalid"]["admission"]["content_labels"], None, ["acceptable-use", "acceptable-use"], [1], [""]):
        invalid = copy.deepcopy(valid["admission"])
        invalid["content_labels"] = labels
        with pytest.raises(InvalidRequest):
            RpcClient(admission=invalid)


def test_p01_local_action_projection_and_hash_vectors():
    vector = json.loads((ROOT / "tests" / "contracts" / "vectors" / "p0-1.json").read_text(encoding="utf-8"))
    case = vector["local_action"]
    kwargs = {
        "destination_site": case["destination_site"],
        "controller_id": case["controller_id"],
        "policy_hash": case["policy_hash"],
        "manifest_hash": case["manifest_hash"],
    }
    projection = local_action_projection(case["request"], **kwargs)
    assert projection == case["projection"]
    assert canonical_local_action_bytes(projection).decode("utf-8") == case["canonical_utf8"]
    assert local_action_payload_hash(case["request"], **kwargs) == case["payload_hash"]
    assert RpcClient().local_action_payload_hash(case["request"], **kwargs) == case["payload_hash"]

    for mutation in case["negative_mutations"]:
        changed = copy.deepcopy(case["request"])
        target = changed
        for key in mutation["path"][:-1]:
            target = target[key]
        target[mutation["path"][-1]] = mutation["value"]
        if mutation["path"] == ["args", "purpose"]:
            changed["admission"]["pipeline"]["purpose"] = mutation["value"]
        assert local_action_payload_hash(changed, **kwargs) != case["payload_hash"]

    changed = copy.deepcopy(case["request"])
    changed["request_id"] = "another-request"
    changed["request_fingerprint"] = "b" * 64
    changed["admission"]["ingress"]["authenticated_peer"] = "another-peer"
    changed["admission"]["approval"] = {"approval_id": "approval-new", "required": True, "consume_atomically": True}
    assert local_action_payload_hash(changed, **kwargs) == case["payload_hash"]

    changed["op"] = "preempt"
    changed["lane"] = "lane-gpu0"
    changed["args"] = {"token": "token-abcdefghijklmnop", "approval_id": "approval-new"}
    changed["admission"]["pipeline"] = None
    del changed["admission"]["content_labels"]
    with pytest.raises(InvalidRequest):
        local_action_payload_hash(changed, destination_site=case["destination_site"], controller_id=case["controller_id"])
    changed["admission"]["content_labels"] = []
    # A site-policy hash is required when the execution request has no pipeline.
    absent_labels_hash = local_action_payload_hash(
        changed,
        destination_site=case["destination_site"],
        controller_id=case["controller_id"],
        policy_hash=case["policy_hash"],
    )
    assert absent_labels_hash != case["payload_hash"]
    changed["args"]["approval_id"] = "approval-other"
    changed["admission"]["approval"]["approval_id"] = "approval-other"
    assert local_action_payload_hash(
        changed,
        destination_site=case["destination_site"],
        controller_id=case["controller_id"],
        policy_hash=case["policy_hash"],
    ) == absent_labels_hash
    del changed["admission"]["content_labels"]
    assert local_action_payload_hash(
        changed,
        destination_site=case["destination_site"],
        controller_id=case["controller_id"],
        policy_hash=case["policy_hash"],
    ) == absent_labels_hash

    delegated = copy.deepcopy(case["request"])
    delegated["admission"]["delegation"] = {"unsupported": True}
    with pytest.raises(InvalidRequest):
        local_action_payload_hash(delegated, **kwargs)


def test_malformed_or_mismatched_json_response_is_unavailable():
    malformed = {"schema": 1, "request_id": "placeholder", "status": 200, "data": {"kind": "status"}, "error": None}
    mismatched = {"schema": 1, "request_id": "other-request", "status": 200, "data": {"kind": "status"}, "error": None}
    for template in (malformed, mismatched):
        class Transport:
            def request(self, endpoint, message, timeout_s):
                response = copy.deepcopy(template)
                if template is malformed:
                    response["request_id"] = message["request_id"]
                return response

        stdout, stderr = StringIO(), StringIO()
        code = main(["--json", "status", "lane-gpu0"], transport=Transport(), clock=VectorClock(), stdout=stdout, stderr=stderr)
        assert code == 3
        assert json.loads(stdout.getvalue())["status"] == 503
        assert json.loads(stdout.getvalue())["data"] is None
        assert stderr.getvalue() == ""


def test_complete_typed_response_examples_and_missing_fields():
    examples = [
        ("booking", "contracts/booking-v1.schema.json", lambda value: {"kind": "booking", "booking": value}, "booking"),
        ("queue", "contracts/queue-v1.schema.json", lambda value: {"kind": "queue", "entries": [value]}, "entries"),
        ("approval", "contracts/approval-v1.schema.json", lambda value: {"kind": "approval", "approval": value}, "approval"),
        ("report", "contracts/event-v1.schema.json", lambda value: {"kind": "report", "events": [value], "next_cursor": None}, "events"),
    ]
    for kind, relative_path, make_data, required_key in examples:
        document = json.loads((ROOT / relative_path).read_text(encoding="utf-8"))
        value = copy.deepcopy(document["x-examples"]["valid"][0])
        response = {"schema": 1, "request_id": "req-example", "status": 200, "data": make_data(value), "error": None}
        validate_response(response, "req-example")
        broken = copy.deepcopy(response)
        del broken["data"][required_key]
        with pytest.raises(InvalidResponse):
            validate_response(broken, "req-example")
