from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

from flightctl.client import InvalidResponse, validate_operation, validate_response
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


def _mutation(operation: str = "queue") -> dict[str, object]:
    return {
        "schema": 1,
        "request_id": "vector-request",
        "status": 200,
        "data": {
            "kind": "mutation",
            "operation": operation,
            "record_type": "queue",
            "record_id": "queue-a",
            "state": "queued",
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
            validate_operation(call)
        if case["name"] == "yield" or case["exit"] == 0:
            assert stdout.getvalue().strip()
            assert not stderr.getvalue()
        else:
            assert stderr.getvalue().startswith("flightctl:")


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
    code = main(["--json", "status", "lane-gpu0"], transport=transport, clock=VectorClock(), stdout=stdout, stderr=stderr)
    assert code == {200: 0, 202: 5, 403: 2, 409: 1, 503: 3}[status]
    expected = copy.deepcopy(template)
    expected["request_id"] = transport.calls[0]["request_id"]
    if isinstance(expected.get("data"), dict) and expected["data"].get("kind") == "pending":
        expected["data"]["request_id"] = expected["request_id"]
    assert json.loads(stdout.getvalue()) == expected
    assert stderr.getvalue() == ""


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
