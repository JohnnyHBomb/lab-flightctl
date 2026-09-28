#!/usr/bin/env bash
# Register a complete roster batch once, then dispatch eligible arms.
set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
python_bin=${ROSTER_PYTHON:-python3}
if [[ $python_bin == */* ]]; then
	[[ -x $python_bin ]] || {
		printf 'run_queue: configured Python is not executable: %s\n' "$python_bin" >&2
		exit 3
	}
else
	command -v "$python_bin" >/dev/null 2>&1 || {
		printf 'run_queue: configured Python was not found: %s\n' "$python_bin" >&2
		exit 3
	}
fi
export ROSTER_DEFAULT_ARM_SCRIPT="$script_dir/run_arm.sh"
export ROSTER_DEFAULT_LOCAL_ARM_SCRIPT="$script_dir/run_local_arm.sh"

exec "$python_bin" - "$@" <<'PY'
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
SHORT_IDENTIFIER = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
CLASSES = {"operator", "booked", "batch", "service", "resident", "standby"}
DEFAULT_RUNTIME_MIN = 240
DEFAULT_RPC_TIMEOUT_S = 30.0
DEFAULT_ARM_TIMEOUT_S = 86400.0
RESPONSE_KEYS = {"schema", "request_id", "status", "data", "error"}
MUTATION_KEYS = {"kind", "operation", "record_type", "record_id", "state", "revision", "reservation"}
PENDING_KEYS = {"kind", "operation", "request_id", "queue_id", "retry_after_s", "wait_deadline", "reason"}
ERROR_KEYS = {"code", "message", "retryable", "failure_class", "details"}
ERROR_CODES = {"invalid", "busy", "denied", "conflict", "unknown", "unsupported_version", "stale_policy", "fenced", "unavailable", "timeout"}
FAILURE_CLASSES = {"client", "policy", "conflict", "transport", "state", "timeout"}
RESERVATION_STATES = {"unassigned", "reserved", "starting", "running", "stopping", "released", "quarantined", "unknown"}
ACTIVE_RESERVATION_STATES = {"reserved", "starting", "running", "stopping", "quarantined"}


class QueueError(Exception):
    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


def fail(message: str, code: int = 2) -> NoReturn:
    raise QueueError(message, code)


def is_identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(IDENTIFIER.fullmatch(value))


def is_short_identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(SHORT_IDENTIFIER.fullmatch(value))


def positive_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        fail(f"{name} must be a positive integer")
    return value


def load_json_file(path_value: str, label: str) -> Any:
    path = Path(path_value)
    if not path.is_file():
        fail(f"{label} is not a regular file: {path}", 3)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"could not read {label}: {exc}", 2)


def load_json_env(name: str, label: str) -> Any | None:
    value = os.environ.get(name)
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        fail(f"{name} is not valid JSON: {exc}")


def parse_workload(value: Any, name: str) -> list[str]:
    if isinstance(value, dict):
        executable = value.get("executable")
        arguments = value.get("args", [])
        if not isinstance(executable, str) or not executable:
            fail(f"{name}.executable must be a non-empty string")
        if not isinstance(arguments, list) or any(not isinstance(item, str) for item in arguments):
            fail(f"{name}.args must be an argv list of strings")
        return [executable, *arguments]
    if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value):
        fail(f"{name} must be a non-empty argv list of strings")
    return list(value)


def graph_has_cycle(arms: list[dict[str, Any]]) -> bool:
    edges = {arm["arm_id"]: set(arm["dependencies"]) for arm in arms}
    for arm in arms:
        predecessor = arm["predecessor"]
        if predecessor is not None:
            edges[arm["arm_id"]].add(predecessor)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        if any(visit(parent) for parent in edges[node]):
            return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in edges)


def prepare_admission(batch: dict[str, Any], path_value: str | None) -> dict[str, Any]:
    supplied: Any
    if path_value:
        supplied = load_json_file(path_value, "admission file")
    else:
        supplied = load_json_env("ROSTER_ADMISSION_JSON", "admission")
    if supplied is None:
        fail("complete admission context is required via ROSTER_ADMISSION_FILE or ROSTER_ADMISSION_JSON")
    else:
        if not isinstance(supplied, dict):
            fail("admission must be a JSON object")
        admission = json.loads(json.dumps(supplied))
        existing = admission.get("batch")
        if existing not in (None, batch):
            fail("admission.batch does not match the manifest")
        admission["batch"] = batch

    required = {"execution", "approval", "pipeline", "delegation", "ingress", "batch"}
    missing = sorted(required.difference(admission))
    if missing:
        fail(f"admission is incomplete; missing: {', '.join(missing)}")
    if admission.get("execution") != "atomic" or admission.get("batch") != batch:
        fail("admission must be atomic and carry the complete batch")
    ingress = admission.get("ingress")
    if not isinstance(ingress, dict) or not is_identifier(ingress.get("controller_id")):
        fail("admission.ingress.controller_id is required")
    return admission


def prepare_manifest(raw: Any, register_only: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(raw, dict):
        fail("manifest must be a JSON object")
    batch_id = raw.get("batch_id")
    if not is_identifier(batch_id):
        fail("manifest.batch_id must be a valid identifier")
    raw_arms = raw.get("arms")
    if not isinstance(raw_arms, list) or not raw_arms:
        fail("manifest.arms must be a non-empty list")
    arm_ids: set[str] = set()
    batch_arms: list[dict[str, Any]] = []
    execution_arms: list[dict[str, Any]] = []
    for index, raw_arm in enumerate(raw_arms):
        if not isinstance(raw_arm, dict):
            fail(f"arms[{index}] must be an object")
        arm_id = raw_arm.get("arm_id")
        if not is_identifier(arm_id):
            fail(f"arms[{index}].arm_id must be a valid identifier")
        if arm_id in arm_ids:
            fail(f"duplicate arm id: {arm_id}")
        arm_ids.add(arm_id)
        predecessor = raw_arm.get("predecessor")
        if predecessor is not None and not is_identifier(predecessor):
            fail(f"{arm_id}.predecessor must be an identifier or null")
        dependencies = raw_arm.get("dependencies")
        if not isinstance(dependencies, list) or any(not is_identifier(item) for item in dependencies):
            fail(f"{arm_id}.dependencies must be an identifier list")
        if len(set(dependencies)) != len(dependencies):
            fail(f"duplicate dependency in arm: {arm_id}")
        batch_arms.append({"arm_id": arm_id, "predecessor": predecessor, "dependencies": list(dependencies)})

        if "lane" not in raw_arm and "lane" not in raw:
            fail(f"{arm_id}.lane is required")
        lane = raw_arm.get("lane", raw.get("lane"))
        if not is_short_identifier(lane):
            fail(f"{arm_id}.lane must be a short identifier")
        purpose = raw_arm.get("purpose", raw.get("purpose", batch_id))
        if not isinstance(purpose, str) or not purpose:
            fail(f"{arm_id}.purpose must be non-empty")
        owner_class = raw_arm.get("class", raw.get("class", "batch"))
        if owner_class not in CLASSES:
            fail(f"{arm_id}.class is unsupported")
        est_min = raw_arm.get("est_min", raw_arm.get("est", raw.get("est_min", DEFAULT_RUNTIME_MIN)))
        max_min = raw_arm.get("max_min", raw_arm.get("max", raw.get("max_min", DEFAULT_RUNTIME_MIN)))
        est_min = positive_integer(est_min, f"{arm_id}.est_min")
        max_min = positive_integer(max_min, f"{arm_id}.max_min")
        if max_min < est_min:
            fail(f"{arm_id}.max_min must not be below est_min")
        if not register_only:
            workload_value = raw_arm.get("workload", raw_arm.get("command"))
            if workload_value is None:
                fail(f"{arm_id}.workload is required")
            workload = parse_workload(workload_value, f"{arm_id}.workload")
        else:
            workload = []
        execution_arms.append(
            {
                "arm_id": arm_id,
                "predecessor": predecessor,
                "dependencies": list(dependencies),
                "lane": lane,
                "purpose": purpose,
                "class": owner_class,
                "est_min": est_min,
                "max_min": max_min,
                "workload": workload,
                "runner": raw_arm.get("runner", raw.get("runner", "remote")),
                "token": raw_arm.get("token"),
                "generation": raw_arm.get("generation"),
            }
        )

    known = set(arm_ids)
    for arm in batch_arms:
        references = [arm["predecessor"], *arm["dependencies"]]
        for reference in references:
            if reference is not None and reference not in known:
                fail(f"dangling arm dependency: {reference}")
    declared_dependencies = raw.get("dependencies", [])
    if not isinstance(declared_dependencies, list) or any(not is_identifier(item) for item in declared_dependencies):
        fail("manifest.dependencies must be an identifier list")
    if len(set(declared_dependencies)) != len(declared_dependencies):
        fail("manifest.dependencies contains duplicates")
    if graph_has_cycle(batch_arms):
        fail("batch arm dependency graph contains a cycle")

    batch = {
        "batch_id": batch_id,
        "arms": batch_arms,
        "dependencies": list(declared_dependencies),
        "registered_before_execution": True,
        "all_arms_visible": True,
    }
    return batch, execution_arms


def executable(value: str, label: str) -> str:
    if not value:
        fail(f"{label} is required", 3)
    if "/" in value:
        if not os.access(value, os.X_OK):
            fail(f"{label} is not executable: {value}", 3)
        return value
    found = shutil.which(value)
    if found is None:
        fail(f"{label} was not found: {value}", 3)
    return found


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def bounded_float(value: str, name: str, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except ValueError:
        fail(f"{name} must be numeric")
    if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        fail(f"{name} must be between {minimum:g} and {maximum:g}")
    return parsed


def retry_delay() -> float:
    return bounded_float(os.environ.get("ROSTER_RETRY_DELAY_S", "0"), "ROSTER_RETRY_DELAY_S", 0.0, 30.0)


def build_request(raw: dict[str, Any], batch: dict[str, Any], admission_path: str | None, request_id: str | None) -> dict[str, Any]:
    admission = prepare_admission(batch, admission_path)
    queue_purpose = raw.get("queue_purpose", raw.get("purpose", batch["batch_id"]))
    if not isinstance(queue_purpose, str) or not queue_purpose:
        fail("queue purpose must be non-empty")
    owner_class = raw.get("queue_class", raw.get("class", "batch"))
    if owner_class not in CLASSES:
        fail("queue class is unsupported")
    max_wait_s = raw.get("max_wait_s")
    if max_wait_s is None:
        max_wait_min = raw.get("max_wait_min", 10)
        max_wait_s = positive_integer(max_wait_min, "max_wait_min") * 60
    else:
        max_wait_s = positive_integer(max_wait_s, "max_wait_s")
    lane = raw.get("queue_lane", raw.get("lane"))
    if lane is not None and not is_short_identifier(lane):
        fail("queue lane must be a short identifier or null")
    args = {"action": "add", "purpose": queue_purpose, "class": owner_class, "max_wait_s": max_wait_s}
    controller_id = admission["ingress"]["controller_id"]
    scope_value = load_json_env("ROSTER_IDEMPOTENCY_SCOPE_JSON", "idempotency scope")
    if scope_value is None:
        scope_value = {"scope": "authenticated-principal", "controller_id": controller_id}
    if not isinstance(scope_value, dict) or set(scope_value) != {"scope", "controller_id"}:
        fail("idempotency scope must contain only scope and controller_id")
    if scope_value.get("scope") not in {"authenticated-principal", "controller"} or not is_identifier(scope_value.get("controller_id")):
        fail("idempotency scope is invalid")
    if scope_value["controller_id"] != controller_id:
        fail("idempotency scope controller does not match admission ingress")
    if request_id is None:
        request_id = os.environ.get("ROSTER_REQUEST_ID")
    if request_id is None:
        request_id = batch["batch_id"]
    if not is_identifier(request_id):
        fail("request id must be a valid identifier")
    fingerprint_input = {"schema": 1, "request_id": request_id, "op": "queue", "lane": lane, "args": args, "idempotency_scope": scope_value, "admission": admission}
    fingerprint = hashlib.sha256(canonical_json(fingerprint_input)).hexdigest()
    request = {
        "schema": 1,
        "request_id": request_id,
        "op": "queue",
        "lane": lane,
        "args": args,
        "idempotency_scope": scope_value,
        "request_fingerprint": fingerprint,
        "admission": admission,
    }
    return request


def bridge_command(bridge: str) -> list[str]:
    command = [executable(bridge, "bridge")]
    args_value = load_json_env("ROSTER_BRIDGE_ARGS_JSON", "bridge arguments")
    if args_value is not None:
        if not isinstance(args_value, list) or any(not isinstance(item, str) for item in args_value):
            fail("ROSTER_BRIDGE_ARGS_JSON must be a string list")
        command.extend(args_value)
    else:
        flag = os.environ.get("ROSTER_BRIDGE_STDIN_FLAG", "--rpc-stdin")
        if flag:
            command.append(flag)
    return command


def invoke_bridge(command: list[str], request: dict[str, Any], timeout_s: float) -> tuple[str, Any | None]:
    payload = json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n"
    try:
        completed = subprocess.run(
            command,
            input=payload,
            text=True,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "lost", None
    except OSError:
        return "lost", None
    if completed.returncode != 0 or not completed.stdout.strip():
        return "lost", None
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return "malformed", None
    if not isinstance(response, dict) or response.get("request_id") != request["request_id"]:
        return "mismatched", response
    if response.get("schema") != 1:
        return "malformed", response
    status = response.get("status")
    if status not in {200, 202, 403, 409, 503}:
        return "malformed", response
    return "response", response


def response_error(response: dict[str, Any]) -> tuple[str, int]:
    status = response.get("status")
    error = response.get("error")
    message = "registration did not complete"
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        message = error["message"]
    elif status == 202 and isinstance(response.get("data"), dict) and isinstance(response["data"].get("reason"), str):
        message = response["data"]["reason"]
    if status == 403:
        return message, 2
    if status == 409:
        return message, 1
    if status == 202:
        return message, 5
    if status == 503:
        return message, 3
    return message, 3


def registration_response(command: list[str], request: dict[str, Any], retries: int, timeout_s: float) -> dict[str, Any]:
    for attempt in range(retries + 1):
        kind, response = invoke_bridge(command, request, timeout_s)
        if kind == "response":
            assert isinstance(response, dict)
            if response["status"] == 503:
                error = response.get("error")
                retryable = isinstance(error, dict) and error.get("retryable") is True
                if retryable and attempt < retries:
                    delay = retry_delay()
                    if delay:
                        time.sleep(delay)
                    continue
            return response
        if kind in {"lost", "mismatched"} and attempt < retries:
            delay = retry_delay()
            if delay:
                time.sleep(delay)
            continue
        if kind == "malformed":
            fail("bridge returned a malformed response", 3)
        fail("registration reply was lost or mismatched", 3)
    fail("registration retry budget exhausted", 3)


def is_nonnegative_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def is_utc_time(value: Any) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def validate_lane_generation(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"lane", "generation", "state"}:
        fail("registration response reservation is malformed", 3)
    lane = value["lane"]
    generation = value["generation"]
    state = value["state"]
    if lane is not None and (not isinstance(lane, dict) or set(lane) != {"site_id", "host_id", "lane_id"} or any(not is_short_identifier(lane[field]) for field in ("site_id", "host_id", "lane_id"))):
        fail("registration response reservation lane is malformed", 3)
    if generation is not None and (not isinstance(generation, int) or isinstance(generation, bool) or generation < 1):
        fail("registration response reservation generation is malformed", 3)
    if state not in RESERVATION_STATES:
        fail("registration response reservation state is malformed", 3)
    if state in ACTIVE_RESERVATION_STATES and (lane is None or generation is None):
        fail("registration response reservation lacks active identity", 3)


def validate_error(value: Any) -> None:
    if not isinstance(value, dict) or not set(value).issubset(ERROR_KEYS) or not {"code", "message", "retryable", "failure_class"}.issubset(value):
        fail("registration response error is malformed", 3)
    if value["code"] not in ERROR_CODES or not isinstance(value["message"], str) or not 0 < len(value["message"]) <= 512:
        fail("registration response error diagnostics are malformed", 3)
    if not isinstance(value["retryable"], bool) or value["failure_class"] not in FAILURE_CLASSES:
        fail("registration response error diagnostics are malformed", 3)
    if "details" in value and not isinstance(value["details"], dict):
        fail("registration response error details are malformed", 3)


def validate_pending(value: Any, expected_request_id: str) -> None:
    if not isinstance(value, dict) or set(value) != PENDING_KEYS:
        fail("registration response pending shape is incomplete", 3)
    if value["kind"] != "pending" or value["operation"] != "queue" or value["request_id"] != expected_request_id:
        fail("registration response pending identity is malformed", 3)
    if value["queue_id"] is not None and not is_identifier(value["queue_id"]):
        fail("registration response pending queue identity is malformed", 3)
    if not isinstance(value["retry_after_s"], int) or isinstance(value["retry_after_s"], bool) or value["retry_after_s"] < 1:
        fail("registration response pending retry interval is malformed", 3)
    deadline = value["wait_deadline"]
    if not isinstance(deadline, dict) or set(deadline) != {"boot_id", "deadline_s", "utc_anchor", "monotonic_anchor_s"}:
        fail("registration response pending deadline is malformed", 3)
    if not is_identifier(deadline["boot_id"]) or not is_nonnegative_number(deadline["deadline_s"]):
        fail("registration response pending deadline is malformed", 3)
    if not is_utc_time(deadline["utc_anchor"]) or not is_nonnegative_number(deadline["monotonic_anchor_s"]):
        fail("registration response pending deadline is malformed", 3)
    if not isinstance(value["reason"], str) or not 0 < len(value["reason"]) <= 512:
        fail("registration response pending reason is malformed", 3)


def validate_mutation(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != MUTATION_KEYS:
        fail("registration response mutation shape is incomplete", 3)
    if value["kind"] != "mutation" or value["operation"] != "queue" or value["record_type"] != "queue":
        fail("registration response is not a queue mutation", 3)
    if not is_identifier(value["record_id"]) or not isinstance(value["revision"], int) or isinstance(value["revision"], bool) or value["revision"] < 1:
        fail("registration response mutation identity is malformed", 3)
    if value["state"] not in {"queued", "eligible"}:
        fail("registration response did not confirm a queued state", 3)
    validate_lane_generation(value["reservation"])


def validate_registration(response: dict[str, Any], expected_request_id: str) -> int:
    """Validate the complete response before exposing any response field."""

    if not isinstance(response, dict) or set(response) != RESPONSE_KEYS:
        fail("registration response envelope is malformed", 3)
    if response["schema"] != 1 or not is_identifier(response["request_id"]) or response["request_id"] != expected_request_id:
        fail("registration response envelope identity is malformed", 3)
    status = response["status"]
    if isinstance(status, bool) or status not in {200, 202, 403, 409, 503}:
        fail("registration response status is malformed", 3)
    if status == 200:
        if response["error"] is not None:
            fail("successful registration response carries an error", 3)
        validate_mutation(response["data"])
    elif status == 202:
        if response["error"] is not None:
            fail("pending registration response carries an error", 3)
        validate_pending(response["data"], expected_request_id)
    else:
        if response["data"] is not None:
            fail("failed registration response carries data", 3)
        validate_error(response["error"])
    return status


def public_registration_response(response: dict[str, Any]) -> dict[str, Any]:
    """Return only the validated, non-secret registration projection."""

    public = {key: response[key] for key in ("schema", "request_id", "status")}
    if response["status"] in {200, 202}:
        public["data"] = response["data"]
        public["error"] = None
    else:
        public["data"] = None
        error = response["error"]
        public["error"] = {key: error[key] for key in ("code", "message", "retryable", "failure_class")}
    return public


def arm_command(arm: dict[str, Any], runner: str, local_runner: str) -> list[str]:
    selected = arm.get("runner", "remote")
    if selected == "local":
        runner = local_runner
    elif selected != "remote":
        fail(f"unsupported runner for {arm['arm_id']}")
    return [
        executable(runner, "arm runner"),
        arm["lane"],
        arm["purpose"],
        "--class",
        arm["class"],
        "--est",
        str(arm["est_min"]),
        "--max",
        str(arm["max_min"]),
        "--",
        *arm["workload"],
    ]


def run_arms(batch_id: str, arms: list[dict[str, Any]], runner: str, local_runner: str, client: str | None, arm_timeout_s: float) -> int:
    completed: set[str] = set()
    failed: str | None = None
    pending = list(arms)
    while pending:
        ready = next(
            (
                arm
                for arm in pending
                if (arm["predecessor"] is None or arm["predecessor"] in completed)
                and all(dependency in completed for dependency in arm["dependencies"])
            ),
            None,
        )
        if ready is None:
            fail("no eligible arm remains after registration; predecessor failed or graph changed", 1)
        pending.remove(ready)
        command = arm_command(ready, runner, local_runner)
        child_env = os.environ.copy()
        if client:
            child_env["FLIGHTCTL_CLIENT"] = client
        child_env["ROSTER_ARM_ID"] = ready["arm_id"]
        child_env["ROSTER_BATCH_ID"] = batch_id
        # A queue arm is fresh unless its manifest explicitly carries an
        # authenticated adoption pair. Never inherit the parent process's
        # token into an unrelated arm.
        child_env.pop("LANE_TOKEN", None)
        child_env.pop("LANE_GENERATION", None)
        token = ready.get("token")
        generation = ready.get("generation")
        if token is not None or generation is not None:
            if not isinstance(token, str) or not token or not isinstance(generation, int) or generation < 1:
                fail(f"{ready['arm_id']} has an incomplete token/generation pair")
            child_env["LANE_TOKEN"] = token
            child_env["LANE_GENERATION"] = str(generation)
        try:
            process = subprocess.Popen(command, env=child_env)
        except OSError as exc:
            fail(f"could not start arm {ready['arm_id']}: {exc}", 3)
        try:
            result = process.wait(timeout=arm_timeout_s)
        except subprocess.TimeoutExpired:
            # This is a PID-bound stop of the lifecycle owner, never a pattern
            # kill or a shared pidfile operation. The owner must quarantine an
            # uncertain lease when its own cleanup cannot complete.
            process.terminate()
            try:
                process.wait(timeout=min(30.0, arm_timeout_s))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=30.0)
            fail(f"arm {ready['arm_id']} exceeded its bounded runtime", 3)
        if result != 0:
            failed = ready["arm_id"]
            break
        completed.add(ready["arm_id"])
    if failed is not None:
        print(f"run_queue: predecessor-dependent batch stopped after arm {failed}", file=sys.stderr)
        return 1
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="run_queue.sh")
    parser.add_argument("manifest_arg", nargs="?")
    parser.add_argument("--manifest")
    parser.add_argument("--admission-file")
    parser.add_argument("--bridge")
    parser.add_argument("--client")
    parser.add_argument("--runner")
    parser.add_argument("--local-runner")
    parser.add_argument("--request-id")
    parser.add_argument("--retries", type=int)
    parser.add_argument("--rpc-timeout", type=float)
    parser.add_argument("--arm-timeout", type=float)
    parser.add_argument("--register-only", action="store_true")
    args = parser.parse_args(argv)
    if args.manifest and args.manifest_arg:
        parser.error("manifest was supplied twice")
    args.manifest = args.manifest or args.manifest_arg or os.environ.get("ROSTER_MANIFEST_FILE")
    if not args.manifest:
        parser.error("a manifest path is required")
    if args.manifest == "-":
        parser.error("manifest stdin is unavailable while the bridge uses stdin; use a file")
    return args


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    raw = load_json_file(args.manifest, "manifest")
    register_only = args.register_only or os.environ.get("ROSTER_REGISTER_ONLY") == "1"
    batch, arms = prepare_manifest(raw, register_only)
    request = build_request(raw, batch, args.admission_file or os.environ.get("ROSTER_ADMISSION_FILE"), args.request_id)

    bridge_value = args.bridge or os.environ.get("FLIGHTCTL_BRIDGE") or os.environ.get("ROSTER_BRIDGE")
    if bridge_value is None:
        bridge_value = args.client or os.environ.get("FLIGHTCTL_CLIENT") or os.environ.get("ROSTER_CLIENT")
    if bridge_value is None:
        fail("FLIGHTCTL_BRIDGE or FLIGHTCTL_CLIENT is required", 3)
    bridge = bridge_command(bridge_value)
    if args.retries is not None:
        retries = args.retries
    else:
        try:
            retries = int(os.environ.get("ROSTER_RPC_RETRIES", "2"))
        except ValueError:
            fail("ROSTER_RPC_RETRIES must be an integer")
    if retries < 0 or retries > 10:
        fail("retries must be between 0 and 10")
    rpc_timeout_s = bounded_float(
        str(args.rpc_timeout) if args.rpc_timeout is not None else os.environ.get("ROSTER_RPC_TIMEOUT_S", str(DEFAULT_RPC_TIMEOUT_S)),
        "RPC timeout",
        0.001,
        300.0,
    )
    response = registration_response(bridge, request, retries, rpc_timeout_s)
    status = validate_registration(response, request["request_id"])
    print(json.dumps(public_registration_response(response), ensure_ascii=False, separators=(",", ":")))
    if status != 200:
        message, code = response_error(response)
        fail(message, code)
    if register_only:
        return 0

    client = args.client or os.environ.get("FLIGHTCTL_CLIENT") or os.environ.get("ROSTER_CLIENT")
    if client is None:
        fail("FLIGHTCTL_CLIENT or ROSTER_CLIENT is required for arm execution", 3)
    client = executable(client, "lifecycle client")
    runner = args.runner or os.environ.get("ROSTER_ARM_SCRIPT") or os.environ.get("ROSTER_RUNNER") or os.environ.get("ROSTER_DEFAULT_ARM_SCRIPT", "")
    local_runner = args.local_runner or os.environ.get("ROSTER_LOCAL_ARM_SCRIPT") or os.environ.get("ROSTER_DEFAULT_LOCAL_ARM_SCRIPT", "")
    if not runner or not local_runner:
        fail("arm runners are not configured", 3)
    # Validate runner selection before any arm can start, after registration is
    # confirmed but before a malformed execution adapter can partially run.
    for arm in arms:
        selected = arm.get("runner", "remote")
        if selected == "remote":
            executable(runner, "arm runner")
        elif selected == "local":
            executable(local_runner, "local arm runner")
        else:
            fail(f"unsupported runner for {arm['arm_id']}")
    arm_timeout_s = bounded_float(
        str(args.arm_timeout) if args.arm_timeout is not None else os.environ.get("ROSTER_ARM_TIMEOUT_S", str(DEFAULT_ARM_TIMEOUT_S)),
        "arm timeout",
        0.001,
        604800.0,
    )
    return run_arms(batch["batch_id"], arms, runner, local_runner, client, arm_timeout_s)


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except QueueError as exc:
        print(f"run_queue: {exc}", file=sys.stderr)
        raise SystemExit(exc.code)
PY
