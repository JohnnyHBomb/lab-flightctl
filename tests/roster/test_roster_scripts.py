from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from tests.contracts.validation import validate_instance, validate_rpc
from tests.fakes.clock import FakeClock


ROOT = Path(__file__).resolve().parents[2]
ARM_SCRIPTS = [ROOT / "roster" / "run_arm.sh", ROOT / "roster" / "run_local_arm.sh"]
QUEUE = ROOT / "roster" / "run_queue.sh"
CLIENT = ROOT / "tests" / "roster" / "fake_client.py"
BRIDGE = ROOT / "tests" / "roster" / "fake_bridge.py"
WORKLOAD_HOOK = ROOT / "tests" / "roster" / "fake_workload.py"
CLEANUP_HOOK = ROOT / "tests" / "roster" / "fake_cleanup.py"

ADMISSION = {
    "execution": "atomic",
    "approval": {"approval_id": None, "required": False, "consume_atomically": True},
    "pipeline": None,
    "delegation": None,
    "ingress": {
        "actor": {"site_id": "site", "tenant_id": "tenant", "issuer": "issuer", "subject": "subject"},
        "subject": None,
        "controller_id": "controller",
        "authenticated_peer": "peer",
        "peer_source": "socket-peer",
        "auth_method": "local",
        "transport_binding": "transport-independent",
        "peer_verified": True,
        "forwarding_headers_ignored": True,
        "operator_elevation": "none",
    },
    "batch": None,
}


def run_script(
    script: Path,
    args: list[str],
    tmp_path: Path,
    *,
    client_path: Path = CLIENT,
    **updates: str,
) -> subprocess.CompletedProcess[str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    trace = tmp_path / "trace.jsonl"
    env = os.environ.copy()
    env.pop("LANE_TOKEN", None)
    env.pop("LANE_GENERATION", None)
    env.update(
        {
            "ROSTER_TRACE_FILE": str(trace),
            "ROSTER_LIFECYCLE_STATE_FILE": str(tmp_path / "lifecycle-state.json"),
            "FLIGHTCTL_CLIENT": str(client_path),
            "ROSTER_PRINCIPAL": "principal-a",
            "PYTHONPATH": str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""),
        }
    )
    env.update(updates)
    return subprocess.run([str(script), *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=10)


def trace_events(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "trace.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def state_value(tmp_path: Path) -> dict[str, Any]:
    path = tmp_path / "lifecycle-state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def events_named(tmp_path: Path, name: str) -> list[dict[str, Any]]:
    return [event for event in trace_events(tmp_path) if event.get("event") == name]


def run_queue(
    manifest: dict[str, Any],
    tmp_path: Path,
    *,
    queue_script: Path = QUEUE,
    client_path: Path = CLIENT,
    bridge_path: Path = BRIDGE,
    **updates: str,
) -> subprocess.CompletedProcess[str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    env_updates = {
        "FLIGHTCTL_BRIDGE": str(bridge_path),
        "FLIGHTCTL_CLIENT": str(client_path),
        "ROSTER_ADMISSION_JSON": json.dumps(ADMISSION),
        "ROSTER_BRIDGE_OUTCOMES": json.dumps(["success"]),
        "ROSTER_CLIENT_OUTCOMES": json.dumps(["success"]),
    }
    env_updates.update(updates)
    args = [
        "--manifest",
        str(manifest_path),
        "--runner",
        str(ROOT / "roster" / "run_arm.sh"),
        "--local-runner",
        str(ROOT / "roster" / "run_local_arm.sh"),
    ]
    return run_script(queue_script, args, tmp_path, client_path=client_path, **env_updates)


def one_batch() -> dict[str, Any]:
    return {
        "batch_id": "batch-a",
        "purpose": "quoted purpose * ; literal",
        "arms": [
            {"arm_id": "arm-a", "predecessor": None, "dependencies": [], "lane": "lane-a", "workload": ["workload", "arg with spaces", "literal;metachar"]},
            {"arm_id": "arm-b", "predecessor": "arm-a", "dependencies": [], "lane": "lane-b", "workload": ["workload", "b"]},
        ],
        "dependencies": [],
    }


def queue_response() -> dict[str, Any]:
    return {
        "schema": 1,
        "request_id": "$request_id",
        "status": 200,
        "data": {
            "kind": "mutation",
            "operation": "queue",
            "record_type": "queue",
            "record_id": "queue-record",
            "state": "queued",
            "revision": 1,
            "reservation": {"lane": None, "generation": None, "state": "unassigned"},
        },
        "error": None,
    }


def fresh_plan(*, generation: int = 7, token: str = "token-fresh-abcdefghijkl") -> dict[str, Any]:
    return {
        "acquire": [{"outcome": "success", "generation": generation, "token": token}],
        "workload": [{"outcome": "success", "duration_s": 1}],
        "cleanup": [{"outcome": "success", "occupants": []}],
    }


def core_lifecycle_events(tmp_path: Path) -> list[str]:
    return [
        str(event["event"])
        for event in trace_events(tmp_path)
        if event.get("event") in {"acquire", "claim", "workload", "cleanup", "release"}
    ]


@pytest.mark.parametrize("script", ARM_SCRIPTS)
def test_arm_fresh_lifecycle_uses_stateful_owner_and_injected_hooks(script: Path, tmp_path: Path) -> None:
    result = run_script(
        script,
        ["lane-a", "quoted purpose * ; literal", "--class", "batch", "--est", "2", "--max", "5", "--", "workload", "arg with spaces", "literal;metachar"],
        tmp_path,
        ROSTER_LIFECYCLE_PLAN=json.dumps(fresh_plan()),
        ROSTER_WORKLOAD_HOOK=str(WORKLOAD_HOOK),
        ROSTER_CLEANUP_HOOK=str(CLEANUP_HOOK),
    )
    assert result.returncode == 0, result.stderr
    assert core_lifecycle_events(tmp_path) == ["acquire", "workload", "cleanup", "release"]
    events = trace_events(tmp_path)
    grant = events_named(tmp_path, "grant")[0]
    cleanup = events_named(tmp_path, "cleanup")[0]
    release = events_named(tmp_path, "release")[0]
    assert grant["generation"] == cleanup["generation"] == release["generation"] == 7
    assert grant["unit"] == cleanup["unit"] == release["unit"]
    assert grant["invocation"] == cleanup["invocation"] == release["invocation"]
    assert events_named(tmp_path, "injected-workload")[0]["argv"] == ["workload", "arg with spaces", "literal;metachar"]
    assert events_named(tmp_path, "injected-cleanup")[0]["argv"] == [grant["unit"], grant["invocation"]]
    assert state_value(tmp_path)["lanes"]["lane-a"]["state"] == "free"
    assert not events_named(tmp_path, "claim")


def test_arm_forwards_runtime_bounds_to_lifecycle_owner(tmp_path: Path) -> None:
    result = run_script(
        ROOT / "roster" / "run_arm.sh",
        ["lane-a", "purpose", "--est", "7", "--max", "11", "--", "workload"],
        tmp_path,
        ROSTER_LIFECYCLE_PLAN=json.dumps(fresh_plan()),
    )
    assert result.returncode == 0, result.stderr
    argv = events_named(tmp_path, "client-argv")[0]["argv"]
    assert argv[argv.index("--est") + 1] == "7"
    assert argv[argv.index("--max") + 1] == "11"


def test_arm_authenticated_adoption_is_claim_bound_and_releases_same_identity(tmp_path: Path) -> None:
    token = "token-adopt-abcdefghijkl"
    result = run_script(
        ROOT / "roster" / "run_arm.sh",
        ["lane-a", "purpose", "--", "workload"],
        tmp_path,
        LANE_TOKEN=token,
        LANE_GENERATION="7",
        ROSTER_LIFECYCLE_PLAN=json.dumps({"claim": [{"outcome": "success"}], "cleanup": [{"outcome": "success", "occupants": []}]}),
    )
    assert result.returncode == 0, result.stderr
    assert core_lifecycle_events(tmp_path) == ["claim", "workload", "cleanup", "release"]
    assert not events_named(tmp_path, "acquire")
    assert events_named(tmp_path, "release")[0]["token"] == token
    assert events_named(tmp_path, "release")[0]["generation"] == 7


@pytest.mark.parametrize("bad_env", [{"LANE_TOKEN": "token-adopt-abcdefghijkl", "LANE_GENERATION": ""}, {"LANE_TOKEN": "", "LANE_GENERATION": "7"}])
def test_arm_adoption_requires_complete_input_pair(tmp_path: Path, bad_env: dict[str, str]) -> None:
    result = run_script(ROOT / "roster" / "run_arm.sh", ["lane-a", "purpose", "--", "workload"], tmp_path, **bad_env)
    assert result.returncode == 2
    assert trace_events(tmp_path) == []


@pytest.mark.parametrize("outcome", ["denied", "stale", "wrong-principal", "malformed"])
def test_arm_adoption_rejects_denied_stale_wrong_principal_and_malformed_grants(tmp_path: Path, outcome: str) -> None:
    result = run_script(
        ROOT / "roster" / "run_arm.sh",
        ["lane-a", "purpose", "--", "workload"],
        tmp_path,
        LANE_TOKEN="token-adopt-abcdefghijkl",
        LANE_GENERATION="7",
        ROSTER_LIFECYCLE_PLAN=json.dumps({"claim": [{"outcome": outcome}], "cleanup": [{"outcome": "success", "occupants": []}]}),
    )
    assert result.returncode in {2, 3}
    assert not events_named(tmp_path, "workload")
    assert not events_named(tmp_path, "cleanup")
    assert not events_named(tmp_path, "release")
    if outcome != "denied":
        assert events_named(tmp_path, "grant-rejected")


@pytest.mark.parametrize("outcome,expected", [("conflict", 1), ("denied", 2), ("timeout", 3), ("malformed", 3)])
def test_failed_acquire_has_no_workload_cleanup_stop_or_release(tmp_path: Path, outcome: str, expected: int) -> None:
    result = run_script(
        ROOT / "roster" / "run_arm.sh",
        ["lane-a", "purpose", "--", "workload"],
        tmp_path,
        ROSTER_LIFECYCLE_PLAN=json.dumps({"acquire": [{"outcome": outcome}]}),
    )
    assert result.returncode == expected
    events = trace_events(tmp_path)
    assert [event["event"] for event in events if event["event"] in {"workload", "cleanup", "release", "stop"}] == []
    assert state_value(tmp_path).get("lanes", {}) == {}


@pytest.mark.parametrize("termination", [signal.SIGINT, signal.SIGTERM])
def test_failed_acquire_signal_paths_do_not_run_cleanup(tmp_path: Path, termination: signal.Signals) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    trace = tmp_path / "trace.jsonl"
    env = os.environ.copy()
    env.update({
        "FLIGHTCTL_CLIENT": str(CLIENT),
        "ROSTER_TRACE_FILE": str(trace),
        "ROSTER_LIFECYCLE_STATE_FILE": str(tmp_path / "lifecycle-state.json"),
        "ROSTER_CLIENT_OUTCOMES": json.dumps(["sleep"]),
        "ROSTER_SHIM_SLEEP_S": "2",
    })
    process = subprocess.Popen([str(ROOT / "roster/run_arm.sh"), "lane-a", "purpose", "--", "workload"], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(100):
            if any(event.get("event") == "client-argv" for event in trace_events(tmp_path)):
                break
            time.sleep(0.01)
        else:
            pytest.fail("client did not reach the real executable seam")
        command_line = Path(f"/proc/{process.pid}/cmdline").read_bytes().decode("utf-8", "replace")
        assert str(CLIENT) in command_line
        process.send_signal(termination)
        process.wait(timeout=3)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
    events = trace_events(tmp_path)
    assert not events_named(tmp_path, "workload")
    assert not events_named(tmp_path, "cleanup")
    assert not events_named(tmp_path, "release")


def test_delayed_cleanup_requires_matching_confirmation_and_preserves_heartbeat(tmp_path: Path) -> None:
    result = run_script(
        ROOT / "roster/run_arm.sh",
        ["lane-a", "purpose", "--", "workload"],
        tmp_path,
        ROSTER_LIFECYCLE_PLAN=json.dumps(
            {
                "acquire": [{"outcome": "success", "generation": 7, "token": "token-cleanup-abcdefghijkl"}],
                "cleanup": [{"outcome": "pending", "occupants": ["pid-owned"], "advance_s": 2}, {"outcome": "success", "occupants": []}],
                "heartbeat": [{"outcome": "success"}],
            }
        ),
        ROSTER_CLEANUP_MAX_S="5",
        ROSTER_CLEANUP_ALLOWANCE_S="5",
    )
    assert result.returncode == 0, result.stderr
    names = [event["event"] for event in trace_events(tmp_path)]
    assert names.index("cleanup-pending") < names.index("heartbeat") < names.index("cleanup") < names.index("release")
    assert events_named(tmp_path, "cleanup-pending")[0]["occupants"] == ["pid-owned"]
    assert state_value(tmp_path)["lanes"]["lane-a"]["state"] == "free"


def test_delayed_old_cleanup_cannot_change_the_current_generation(tmp_path: Path) -> None:
    result = run_script(
        ROOT / "roster" / "run_arm.sh",
        ["lane-a", "purpose", "--", "workload"],
        tmp_path,
        ROSTER_LIFECYCLE_PLAN=json.dumps(
            {
                "acquire": [{"outcome": "success", "generation": 8, "token": "token-current-abcdefghijkl"}],
                "cleanup": [
                    {"outcome": "pending", "occupants": ["pid-current"], "advance_s": 1},
                    {"outcome": "success", "generation": 7, "unit": "unit-old", "invocation": "invocation-old", "occupants": []},
                ],
                "heartbeat": [{"outcome": "success"}],
            }
        ),
        ROSTER_CLEANUP_MAX_S="5",
    )
    assert result.returncode == 3
    assert not events_named(tmp_path, "release")
    assert state_value(tmp_path)["lanes"]["lane-a"]["generation"] == 8
    assert state_value(tmp_path)["lanes"]["lane-a"]["state"] != "quarantined"
    assert events_named(tmp_path, "cleanup-attempt")[-1]["identity_checked"] is False


def test_competing_acquisition_is_rejected_while_cleanup_is_pending(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    trace = tmp_path / "trace.jsonl"
    env = os.environ.copy()
    env.update(
        {
            "FLIGHTCTL_CLIENT": str(CLIENT),
            "ROSTER_TRACE_FILE": str(trace),
            "ROSTER_LIFECYCLE_STATE_FILE": str(tmp_path / "lifecycle-state.json"),
            "ROSTER_PRINCIPAL": "principal-a",
            "PYTHONPATH": str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""),
            "ROSTER_CLEANUP_HOOK": str(WORKLOAD_HOOK),
            "ROSTER_WORKLOAD_HOOK_SLEEP_S": "1",
            "ROSTER_HOOK_TIMEOUT_S": "5",
            "ROSTER_LIFECYCLE_PLAN": json.dumps(
                {
                    "acquire": [{"outcome": "success", "generation": 7, "token": "token-held-abcdefghijkl"}],
                    "cleanup": [{"outcome": "pending", "occupants": ["pid-held"], "advance_s": 1}, {"outcome": "success", "occupants": []}],
                    "heartbeat": [{"outcome": "success"}],
                }
            ),
        }
    )
    first = subprocess.Popen(
        [str(ROOT / "roster" / "run_arm.sh"), "lane-a", "purpose", "--", "workload"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        for _ in range(200):
            if events_named(tmp_path, "cleanup-attempt"):
                break
            time.sleep(0.01)
        else:
            pytest.fail("first arm did not reach cleanup")
        second = run_script(
            ROOT / "roster" / "run_arm.sh",
            ["lane-a", "purpose", "--", "workload"],
            tmp_path,
            ROSTER_ARM_ID="arm-competing",
            ROSTER_LIFECYCLE_PLAN=json.dumps(fresh_plan()),
        )
        assert second.returncode == 1, second.stderr
        assert len(events_named(tmp_path, "workload")) == 1
        assert events_named(tmp_path, "rpc-response")[-1]["request_id"] == "arm-competing:acquire"
    finally:
        if first.poll() is None:
            first.terminate()
        try:
            first.wait(timeout=5)
        except subprocess.TimeoutExpired:
            first.kill()
            first.wait(timeout=5)


def test_cleanup_timeout_quarantines_without_fabricating_free(tmp_path: Path) -> None:
    result = run_script(
        ROOT / "roster/run_arm.sh",
        ["lane-a", "purpose", "--", "workload"],
        tmp_path,
        ROSTER_LIFECYCLE_PLAN=json.dumps(
            {
                "acquire": [{"outcome": "success", "generation": 7, "token": "token-timeout-abcdefghijkl"}],
                "cleanup": [{"outcome": "pending", "occupants": ["pid-owned"], "advance_s": 1}],
                "heartbeat": [{"outcome": "success"}],
            }
        ),
        ROSTER_CLEANUP_MAX_S="2",
        ROSTER_CLOCK_STEP_S="1",
    )
    assert result.returncode == 3
    assert not events_named(tmp_path, "release")
    assert state_value(tmp_path)["lanes"]["lane-a"]["state"] == "quarantined"
    assert 1 <= len(events_named(tmp_path, "heartbeat")) <= 3
    assert events_named(tmp_path, "quarantine")[0]["reason"]


def test_successors_are_visible_before_execution_and_roots_remain_fifo(tmp_path: Path) -> None:
    manifest = {
        "batch_id": "batch-graph",
        "purpose": "graph",
        "arms": [
            {"arm_id": "arm-a", "predecessor": None, "dependencies": [], "lane": "lane-a", "workload": ["workload", "a"]},
            {"arm_id": "arm-b", "predecessor": "arm-a", "dependencies": [], "lane": "lane-b", "workload": ["workload", "b"]},
            {"arm_id": "arm-c", "predecessor": "arm-b", "dependencies": [], "lane": "lane-c", "workload": ["workload", "c"]},
            {"arm_id": "arm-root", "predecessor": None, "dependencies": [], "lane": "lane-root", "workload": ["workload", "root"]},
        ],
        "dependencies": [],
    }
    result = run_queue(manifest, tmp_path)
    assert result.returncode == 0, result.stderr
    events = trace_events(tmp_path)
    request = next(event["request"] for event in events if event["event"] == "bridge-request")
    batch = request["admission"]["batch"]
    assert [arm["arm_id"] for arm in batch["arms"]] == ["arm-a", "arm-b", "arm-c", "arm-root"]
    validate_rpc(request)
    validate_instance(json.loads(result.stdout.splitlines()[0]), "rpc-envelope-v1.schema.json")
    assert [event["lane"] for event in events if event["event"] in {"acquire", "claim"}] == ["lane-a", "lane-b", "lane-c", "lane-root"]
    assert next(event for event in events if event["event"] == "queue-observation")["eligible"] == ["arm-a", "arm-root"]


def test_reversed_manifest_input_still_waits_for_predecessors(tmp_path: Path) -> None:
    manifest = one_batch()
    manifest["arms"] = list(reversed(manifest["arms"]))
    result = run_queue(manifest, tmp_path)
    assert result.returncode == 0, result.stderr
    assert [event["lane"] for event in events_named(tmp_path, "acquire")] == ["lane-a", "lane-b"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda manifest: {**manifest, "arms": [manifest["arms"][0], manifest["arms"][0]]},
        lambda manifest: {**manifest, "arms": [{**manifest["arms"][0], "predecessor": "missing"}, manifest["arms"][1]]},
        lambda manifest: {**manifest, "arms": [{**manifest["arms"][0], "predecessor": "arm-b"}, manifest["arms"][1]]},
    ],
)
def test_invalid_graph_is_rejected_before_registration(tmp_path: Path, mutation: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
    result = run_queue(mutation(one_batch()), tmp_path)
    assert result.returncode == 2
    assert not events_named(tmp_path, "bridge-request")
    assert not events_named(tmp_path, "acquire")


def test_failed_predecessor_does_not_start_successors(tmp_path: Path) -> None:
    result = run_queue(one_batch(), tmp_path, ROSTER_CLIENT_OUTCOMES=json.dumps(["acquire-conflict"]))
    assert result.returncode == 1
    assert [event["lane"] for event in events_named(tmp_path, "acquire")] == ["lane-a"]
    assert not events_named(tmp_path, "workload")


@pytest.mark.parametrize("field,mutate", [("top-level", lambda response: response | {"token": "secret-top-level"}), ("data", lambda response: response | {"data": response["data"] | {"token": "secret-in-data"}}), ("reservation", lambda response: response | {"data": response["data"] | {"reservation": {"lane": 42, "generation": "bad", "state": "reserved"}}})])
def test_registration_validation_is_fail_closed_before_export_or_dispatch(tmp_path: Path, field: str, mutate: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
    response = mutate(queue_response())
    result = run_queue(one_batch(), tmp_path, ROSTER_BRIDGE_OUTCOMES=json.dumps([{"response": response}]), ROSTER_REGISTER_ONLY="1")
    assert result.returncode == 3, field
    assert result.stdout == ""
    assert "secret-" not in result.stderr
    assert not events_named(tmp_path, "client-argv")


def test_boolean_schema_version_is_rejected_before_real_dispatch(tmp_path: Path) -> None:
    response = queue_response() | {"schema": True}
    result = run_queue(
        one_batch(),
        tmp_path,
        ROSTER_BRIDGE_OUTCOMES=json.dumps([{"response": response}]),
    )
    assert result.returncode == 3
    assert result.stdout == ""
    assert not events_named(tmp_path, "client-argv")
    assert not events_named(tmp_path, "workload")


def test_failure_diagnostics_are_validated_and_secret_details_are_not_exported(tmp_path: Path) -> None:
    response = {
        "schema": 1,
        "request_id": "$request_id",
        "status": 403,
        "data": None,
        "error": {"code": "denied", "message": "admission denied", "retryable": False, "failure_class": "policy", "details": {"secret": "do-not-export"}},
    }
    result = run_queue(one_batch(), tmp_path, ROSTER_BRIDGE_OUTCOMES=json.dumps([{"response": response}]), ROSTER_REGISTER_ONLY="1")
    assert result.returncode == 2
    exported = json.loads(result.stdout)
    assert exported["error"] == {"code": "denied", "message": "admission denied", "retryable": False, "failure_class": "policy"}
    assert "do-not-export" not in result.stdout
    assert result.stderr == "run_queue: admission denied\n"


def test_lost_registration_reply_reuses_identity_and_effect(tmp_path: Path) -> None:
    bridge_state = tmp_path / "bridge-state"
    result = run_queue(one_batch(), tmp_path, ROSTER_BRIDGE_OUTCOMES=json.dumps(["lost", "success"]), ROSTER_BRIDGE_OUTCOMES_STATE_FILE=str(bridge_state), ROSTER_REGISTER_ONLY="1")
    assert result.returncode == 0, result.stderr
    requests = [event["request"] for event in events_named(tmp_path, "bridge-request")]
    assert len(requests) == 2
    assert requests[0]["request_id"] == requests[1]["request_id"]
    assert requests[0]["request_fingerprint"] == requests[1]["request_fingerprint"]


def test_lost_registration_reply_replays_a_durable_effect(tmp_path: Path) -> None:
    outcome_state = tmp_path / "bridge-outcomes"
    result = run_queue(
        one_batch(),
        tmp_path,
        ROSTER_BRIDGE_OUTCOMES=json.dumps(["lost-after-effect", "success"]),
        ROSTER_BRIDGE_OUTCOMES_STATE_FILE=str(outcome_state),
        ROSTER_REGISTER_ONLY="1",
    )
    assert result.returncode == 0, result.stderr
    assert len(events_named(tmp_path, "bridge-effect")) == 1
    assert len(events_named(tmp_path, "bridge-replay")) == 1
    requests = [event["request"] for event in events_named(tmp_path, "bridge-request")]
    assert len(requests) == 2
    assert requests[0]["request_id"] == requests[1]["request_id"]
    assert requests[0]["request_fingerprint"] == requests[1]["request_fingerprint"]


def test_lifecycle_success_responses_are_frozen_rpc_envelopes(tmp_path: Path) -> None:
    result = run_script(
        ROOT / "roster" / "run_arm.sh",
        ["lane-a", "purpose", "--", "workload"],
        tmp_path,
        ROSTER_LIFECYCLE_PLAN=json.dumps(fresh_plan()),
    )
    assert result.returncode == 0, result.stderr
    responses = [event["response"] for event in events_named(tmp_path, "rpc-envelope")]
    assert responses
    for response in responses:
        validate_instance(response, "rpc-envelope-v1.schema.json")


@pytest.mark.parametrize("operation,env", [("acquire", {}), ("claim", {"LANE_TOKEN": "token-adopt-abcdefghijkl", "LANE_GENERATION": "7"})])
def test_lost_acquire_or_claim_reply_has_one_logical_effect(tmp_path: Path, operation: str, env: dict[str, str]) -> None:
    plan = {operation: [{"outcome": "lost"}], "cleanup": [{"outcome": "success", "occupants": []}]}
    result = run_script(ROOT / "roster/run_arm.sh", ["lane-a", "purpose", "--", "workload"], tmp_path, ROSTER_LIFECYCLE_PLAN=json.dumps(plan), **env)
    assert result.returncode == 0, result.stderr
    requests = [event for event in events_named(tmp_path, "rpc-request") if event["operation"] == operation]
    assert len(requests) == 2
    assert {event["request_id"] for event in requests} == {f"lane-a:{operation}"}
    assert {event["request_fingerprint"] for event in requests}.__len__() == 1
    assert len(events_named(tmp_path, "grant")) == 1
    assert len(events_named(tmp_path, "workload")) == 1


@pytest.mark.parametrize("operation,env", [("acquire", {}), ("claim", {"LANE_TOKEN": "token-adopt-abcdefghijkl", "LANE_GENERATION": "7"})])
def test_lost_after_effect_persists_acquire_or_claim_before_reply(tmp_path: Path, operation: str, env: dict[str, str]) -> None:
    plan = {operation: [{"outcome": "lost-after-effect"}]}
    result = run_script(
        ROOT / "roster" / "run_arm.sh",
        ["lane-a", "purpose", "--", "workload"],
        tmp_path,
        ROSTER_LIFECYCLE_PLAN=json.dumps(plan),
        ROSTER_OPERATION_RETRIES="0",
        **env,
    )
    assert result.returncode == 3
    assert len(events_named(tmp_path, "grant")) == 1
    assert not events_named(tmp_path, "workload")
    assert state_value(tmp_path)["lanes"]["lane-a"]["state"] == "running"


def test_lost_release_reply_replays_and_releases_once(tmp_path: Path) -> None:
    plan = fresh_plan() | {"release": [{"outcome": "release-lost"}]}
    result = run_script(ROOT / "roster/run_arm.sh", ["lane-a", "purpose", "--", "workload"], tmp_path, ROSTER_LIFECYCLE_PLAN=json.dumps(plan))
    assert result.returncode == 0, result.stderr
    requests = [event for event in events_named(tmp_path, "rpc-request") if event["operation"] == "release"]
    assert len(requests) == 2
    assert len(events_named(tmp_path, "release")) == 1
    assert state_value(tmp_path)["lanes"]["lane-a"]["state"] == "free"


def test_lost_release_effect_is_durable_before_reply_retry(tmp_path: Path) -> None:
    plan = fresh_plan() | {"release": [{"outcome": "release-lost"}]}
    result = run_script(
        ROOT / "roster" / "run_arm.sh",
        ["lane-a", "purpose", "--", "workload"],
        tmp_path,
        ROSTER_LIFECYCLE_PLAN=json.dumps(plan),
        ROSTER_OPERATION_RETRIES="0",
    )
    assert result.returncode == 3
    assert len(events_named(tmp_path, "release")) == 1
    assert state_value(tmp_path)["lanes"]["lane-a"]["state"] == "free"


def test_unknown_final_release_is_quarantined_and_not_success(tmp_path: Path) -> None:
    plan = fresh_plan() | {"release": [{"outcome": "release-unknown"}]}
    result = run_script(ROOT / "roster/run_arm.sh", ["lane-a", "purpose", "--", "workload"], tmp_path, ROSTER_LIFECYCLE_PLAN=json.dumps(plan))
    assert result.returncode == 3
    assert not events_named(tmp_path, "release")
    assert state_value(tmp_path)["lanes"]["lane-a"]["state"] == "quarantined"


def test_quarantined_lane_rejects_a_subsequent_acquisition(tmp_path: Path) -> None:
    first = run_script(
        ROOT / "roster" / "run_arm.sh",
        ["lane-a", "purpose", "--", "workload"],
        tmp_path,
        ROSTER_ARM_ID="arm-first",
        ROSTER_LIFECYCLE_PLAN=json.dumps(fresh_plan() | {"release": [{"outcome": "release-unknown"}]}),
    )
    assert first.returncode == 3
    second = run_script(
        ROOT / "roster" / "run_arm.sh",
        ["lane-a", "purpose", "--", "workload"],
        tmp_path,
        ROSTER_ARM_ID="arm-second",
        ROSTER_LIFECYCLE_PLAN=json.dumps(fresh_plan()),
    )
    assert second.returncode == 1
    assert len(events_named(tmp_path, "workload")) == 1
    assert state_value(tmp_path)["lanes"]["lane-a"]["state"] == "quarantined"


def test_budget_preserves_cleanup_allowance_without_renewing_past_max(tmp_path: Path) -> None:
    plan = fresh_plan() | {"workload": [{"outcome": "success", "duration_s": 4}], "cleanup": [{"outcome": "pending", "occupants": ["pid-owned"], "advance_s": 1}, {"outcome": "success", "occupants": []}], "heartbeat": [{"outcome": "success"}]}
    result = run_script(ROOT / "roster/run_arm.sh", ["lane-a", "purpose", "--est", "1", "--max", "1", "--", "workload"], tmp_path, ROSTER_LIFECYCLE_PLAN=json.dumps(plan), ROSTER_APPROVED_MAX_S="5", ROSTER_CLEANUP_ALLOWANCE_S="2", ROSTER_CLEANUP_MAX_S="2")
    assert result.returncode == 0, result.stderr
    assert state_value(tmp_path)["clock_monotonic_s"] == 5
    assert events_named(tmp_path, "heartbeat")
    assert not any(event["event"] == "renew" for event in trace_events(tmp_path))


def test_fake_clock_drives_bounded_cleanup_polling() -> None:
    clock = FakeClock()
    before = clock.monotonic()
    clock.advance(monotonic_s=60)
    assert clock.monotonic() == before + 60
    assert [call["method"] for call in clock.calls] == ["monotonic", "advance", "monotonic"]


def test_trap_identity_and_quoting_are_pid_and_argv_bound(tmp_path: Path) -> None:
    result = run_script(
        ROOT / "roster/run_arm.sh",
        ["lane-a", "purpose with spaces * ; $HOME", "--", "workload", "arg with spaces", "literal;metachar"],
        tmp_path,
        ROSTER_LIFECYCLE_PLAN=json.dumps(fresh_plan()),
        ROSTER_WORKLOAD_HOOK=str(WORKLOAD_HOOK),
        ROSTER_CLEANUP_HOOK=str(CLEANUP_HOOK),
    )
    assert result.returncode == 0, result.stderr
    argv_event = events_named(tmp_path, "client-argv")[0]
    assert argv_event["argv"][-4:] == ["--", "workload", "arg with spaces", "literal;metachar"]
    assert events_named(tmp_path, "workload")[0]["argv"] == ["workload", "arg with spaces", "literal;metachar"]
    assert events_named(tmp_path, "injected-workload")[0]["argv"] == ["workload", "arg with spaces", "literal;metachar"]
    for script in ARM_SCRIPTS:
        text = script.read_text(encoding="utf-8")
        assert "pkill" not in text
        assert not any(line.lstrip().startswith("trap ") for line in text.splitlines())


def test_workload_timeout_checks_pid_identity_before_termination(tmp_path: Path) -> None:
    result = run_script(
        ROOT / "roster/run_arm.sh",
        ["lane-a", "purpose", "--", "workload"],
        tmp_path,
        ROSTER_LIFECYCLE_PLAN=json.dumps(fresh_plan()),
        ROSTER_WORKLOAD_HOOK=str(WORKLOAD_HOOK),
        ROSTER_WORKLOAD_HOOK_SLEEP_S="1",
        ROSTER_HOOK_TIMEOUT_S="0.05",
    )
    assert result.returncode == 3
    timeout = events_named(tmp_path, "workload-timeout")[0]
    assert timeout["identity_checked"] is True
    assert events_named(tmp_path, "cleanup")
    assert events_named(tmp_path, "release")


def _mutant_client(tmp_path: Path, old: str, new: str) -> Path:
    directory = tmp_path / "mutant-client"
    directory.mkdir(parents=True, exist_ok=True)
    source = (ROOT / "tests/roster/shims.py").read_text(encoding="utf-8")
    assert old in source
    (directory / "shims.py").write_text(source.replace(old, new, 1), encoding="utf-8")
    target = directory / "fake_client.py"
    target.write_text(CLIENT.read_text(encoding="utf-8"), encoding="utf-8")
    target.chmod(0o755)
    return target


def _mutant_queue(tmp_path: Path, old: str, new: str) -> Path:
    source = QUEUE.read_text(encoding="utf-8")
    assert old in source
    tmp_path.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "run_queue-mutant.sh"
    target.write_text(source.replace(old, new, 1), encoding="utf-8")
    target.chmod(0o755)
    return target


def test_required_mutations_are_caught_in_temp_copies(tmp_path: Path) -> None:
    cleanup_before_grant = _mutant_client(
        tmp_path / "cleanup-before-grant",
        "        acquire_code = owner.acquire()\n        if acquire_code:",
        "        cleanup_code = owner.cleanup()\n        acquire_code = owner.acquire()\n        if acquire_code or cleanup_code:",
    )
    result = run_script(ROOT / "roster/run_arm.sh", ["lane-a", "purpose", "--", "workload"], tmp_path / "cleanup-run", client_path=cleanup_before_grant, ROSTER_LIFECYCLE_PLAN=json.dumps(fresh_plan()))
    with pytest.raises(AssertionError):
        assert result.returncode == 0 and core_lifecycle_events(tmp_path / "cleanup-run") == ["acquire", "workload", "cleanup", "release"]
    assert events_named(tmp_path / "cleanup-run", "cleanup-before-grant")

    bypass_adoption = _mutant_client(
        tmp_path / "bypass-adoption",
        '        operation = "claim" if self.input_token else "acquire"',
        '        operation = "acquire"',
    )
    result = run_script(ROOT / "roster/run_arm.sh", ["lane-a", "purpose", "--", "workload"], tmp_path / "adoption-run", client_path=bypass_adoption, LANE_TOKEN="token-adopt-abcdefghijkl", LANE_GENERATION="7", ROSTER_LIFECYCLE_PLAN=json.dumps({"acquire": [{"outcome": "success"}], "cleanup": [{"outcome": "success", "occupants": []}]}))
    with pytest.raises(AssertionError):
        assert result.returncode == 0 and core_lifecycle_events(tmp_path / "adoption-run") == ["claim", "workload", "cleanup", "release"]

    hidden_successors = _mutant_queue(tmp_path / "hidden-successors", '"all_arms_visible": True', '"all_arms_visible": False')
    result = run_queue(one_batch(), tmp_path / "visibility-run", queue_script=hidden_successors, ROSTER_REGISTER_ONLY="1")
    assert result.returncode == 0
    request = next(event["request"] for event in events_named(tmp_path / "visibility-run", "bridge-request"))
    with pytest.raises(AssertionError):
        assert request["admission"]["batch"]["all_arms_visible"] is True

    changed_retry_identity = _mutant_queue(tmp_path / "retry-identity", "kind, response = invoke_bridge(command, request, timeout_s)", 'kind, response = invoke_bridge(command, {**request, "request_id": f"{request[\'request_id\']}-{attempt}"}, timeout_s)')
    bridge_state = tmp_path / "retry-run" / "bridge-state"
    result = run_queue(one_batch(), tmp_path / "retry-run", queue_script=changed_retry_identity, ROSTER_BRIDGE_OUTCOMES=json.dumps(["lost", "success"]), ROSTER_BRIDGE_OUTCOMES_STATE_FILE=str(bridge_state), ROSTER_REGISTER_ONLY="1")
    requests = [event["request"] for event in events_named(tmp_path / "retry-run", "bridge-request")]
    with pytest.raises(AssertionError):
        assert len({request["request_id"] for request in requests}) == 1

    release_before_cleanup = _mutant_client(
        tmp_path / "release-before-cleanup",
        "        cleanup_code = owner.cleanup()\n        if cleanup_code:",
        "        release_code = owner.release()\n        cleanup_code = owner.cleanup()\n        if cleanup_code or release_code:",
    )
    result = run_script(ROOT / "roster/run_arm.sh", ["lane-a", "purpose", "--", "workload"], tmp_path / "release-run", client_path=release_before_cleanup, ROSTER_LIFECYCLE_PLAN=json.dumps(fresh_plan()))
    with pytest.raises(AssertionError):
        assert result.returncode == 0 and core_lifecycle_events(tmp_path / "release-run") == ["acquire", "workload", "cleanup", "release"]
    assert events_named(tmp_path / "release-run", "release-before-cleanup")
