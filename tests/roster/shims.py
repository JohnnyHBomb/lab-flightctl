from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

# Executable shims are launched by an absolute path, so Python's initial path
# contains tests/roster rather than the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.contracts.validation import validator
from tests.fakes.clock import FakeClock
from tests.fakes.ssh import FakeTransport


def _trace(event: dict[str, Any]) -> None:
    trace_path = os.environ.get("ROSTER_TRACE_FILE")
    if not trace_path:
        return
    with Path(trace_path).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")


def _load_json_env(name: str, default: Any) -> Any:
    value = os.environ.get(name)
    if value is None:
        return copy.deepcopy(default)
    return json.loads(value)


def _next_scripted(name: str, default: str = "success") -> Any:
    outcomes = _load_json_env(name, [default])
    if not isinstance(outcomes, list) or not outcomes:
        raise SystemExit(f"{name} must be a non-empty JSON list")
    state_path = os.environ.get(f"{name}_STATE_FILE")
    index = 0
    if state_path:
        path = Path(state_path)
        if path.exists():
            index = int(path.read_text(encoding="utf-8"))
        path.write_text(str(index + 1), encoding="utf-8")
    return outcomes[min(index, len(outcomes) - 1)]


def _default_queue_response(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": 1,
        "request_id": request["request_id"],
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


def _response_for(request: dict[str, Any], scripted: Any) -> dict[str, Any]:
    if isinstance(scripted, dict) and "response" in scripted:
        response = copy.deepcopy(scripted["response"])
    elif isinstance(scripted, dict) and "status" in scripted:
        response = copy.deepcopy(scripted)
    else:
        response = _default_queue_response(request)
    if response.get("request_id") == "$request_id":
        response["request_id"] = request["request_id"]
    return response


def _bridge_effects_file() -> Path | None:
    value = os.environ.get("ROSTER_BRIDGE_EFFECTS_FILE")
    if value:
        return Path(value)
    outcome_state = os.environ.get("ROSTER_BRIDGE_OUTCOMES_STATE_FILE")
    if outcome_state:
        return Path(f"{outcome_state}.effects")
    trace_path = os.environ.get("ROSTER_TRACE_FILE")
    return Path(f"{trace_path}.bridge-effects") if trace_path else None


def _load_bridge_effects() -> dict[str, Any]:
    path = _bridge_effects_file()
    if path is None or not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid bridge effects: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit("bridge effects must be an object")
    return value


def _save_bridge_effects(effects: dict[str, Any]) -> None:
    path = _bridge_effects_file()
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(effects, sort_keys=True, separators=(",", ":")), encoding="utf-8")


@lru_cache(maxsize=1)
def _response_validator() -> Any:
    return validator("rpc-envelope-v1.schema.json")


def _is_frozen_response(response: Any) -> bool:
    # Use the frozen schema itself; a second partial schema hid invalid grants.
    return isinstance(response, dict) and "status" in response and _response_validator().is_valid(response)


def bridge_main() -> int:
    raw = sys.stdin.read()
    try:
        request = json.loads(raw)
    except json.JSONDecodeError:
        return 2
    if not isinstance(request, dict):
        return 2
    effects = _load_bridge_effects()
    request_id = request.get("request_id")
    fingerprint = request.get("request_fingerprint")
    cached = effects.get(request_id) if isinstance(request_id, str) else None
    if cached is not None:
        _trace({"event": "bridge-request", "request": request, "outcome": "durable-replay"})
        if not isinstance(cached, dict) or cached.get("request_fingerprint") != fingerprint or not isinstance(cached.get("response"), dict):
            _trace({"event": "bridge-replay-conflict", "request_id": request_id})
            return 75
        _trace({"event": "bridge-replay", "request_id": request_id})
        print(json.dumps(cached["response"], sort_keys=True, separators=(",", ":")))
        return 0

    scripted = _next_scripted("ROSTER_BRIDGE_OUTCOMES")
    outcome = scripted.get("outcome", "success") if isinstance(scripted, dict) else scripted
    response = _response_for(request, scripted)
    if request.get("op") == "queue" and request.get("args", {}).get("action") == "add":
        batch = request.get("admission", {}).get("batch", {})
        arms = batch.get("arms", []) if isinstance(batch, dict) else []
        eligible = [arm["arm_id"] for arm in arms if arm.get("predecessor") is None and not arm.get("dependencies")]
        _trace({"event": "queue-observation", "visible": [arm.get("arm_id") for arm in arms], "eligible": eligible})
    _trace({"event": "bridge-request", "request": request, "outcome": outcome})
    transport_outcome = "lost" if outcome == "lost-after-effect" else str(outcome)
    if outcome == "lost-after-effect" and _is_frozen_response(response):
        effects[request_id] = {"request_fingerprint": fingerprint, "response": response}
        _save_bridge_effects(effects)
        _trace({"event": "bridge-effect", "request_id": request_id})
    transport = FakeTransport([{"outcome": transport_outcome, "response": response}])
    result = transport.request("rpc-stdin", request, float(os.environ.get("ROSTER_TEST_TIMEOUT_S", "1")))
    _trace({"event": "transport", "result": dict(result)})
    if result.get("status") != "ok":
        return 75
    print(json.dumps(result["response"], sort_keys=True, separators=(",", ":")))
    return 0


def _state_file() -> Path | None:
    value = os.environ.get("ROSTER_LIFECYCLE_STATE_FILE")
    return Path(value) if value else None


def _load_state() -> dict[str, Any]:
    path = _state_file()
    if path is None or not path.exists():
        return {"lanes": {}, "requests": {}, "script_indexes": {}, "clock_monotonic_s": 0.0}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"invalid lifecycle state: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit("lifecycle state must be an object")
    for key, default in (("lanes", {}), ("requests", {}), ("script_indexes", {})):
        if not isinstance(value.get(key, default), dict):
            raise SystemExit(f"lifecycle state {key} must be an object")
        value.setdefault(key, default)
    value.setdefault("clock_monotonic_s", 0.0)
    return value


def _save_state(state: dict[str, Any]) -> None:
    path = _state_file()
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, sort_keys=True, separators=(",", ":")), encoding="utf-8")


def _number(value: Any, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) and result >= 0 else default


def _proc_cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


class LifecycleOwner:
    """Stateful test seam for the frozen run lifecycle."""

    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.state = _load_state()
        self.clock = FakeClock(monotonic_s=float(self.state.get("clock_monotonic_s", 0.0)))
        self.plan = self._load_plan()
        self.legacy = None if self.plan is not None else self._legacy_outcome()
        self.lane = argv[1]
        self.purpose = argv[2]
        self.workload = argv[argv.index("--") + 1 :]
        self.arm_id = os.environ.get("ROSTER_ARM_ID", self.lane)
        self.batch_id = os.environ.get("ROSTER_BATCH_ID")
        self.principal = os.environ.get("ROSTER_PRINCIPAL", "principal-a")
        self.input_token = os.environ.get("LANE_TOKEN")
        generation_value = os.environ.get("LANE_GENERATION")
        self.input_generation = int(generation_value) if generation_value and generation_value.isdigit() else None
        self.token: str | None = None
        self.generation: int | None = None
        self.lease_id: str | None = None
        self.unit: str | None = None
        self.invocation: str | None = None
        self.cleanup_confirmed = False
        self.started_s = self.clock.monotonic()
        self.approved_max_s = _number(os.environ.get("ROSTER_APPROVED_MAX_S"), self._argv_max_minutes() * 60)
        self.cleanup_allowance_s = _number(os.environ.get("ROSTER_CLEANUP_ALLOWANCE_S"), 30.0)
        self.cleanup_max_s = _number(os.environ.get("ROSTER_CLEANUP_MAX_S"), self.cleanup_allowance_s)
        retries = os.environ.get("ROSTER_OPERATION_RETRIES", "2")
        try:
            self.retries = max(0, min(10, int(retries)))
        except ValueError:
            self.retries = 2
        self._persist()

    def _load_plan(self) -> dict[str, Any] | None:
        value = os.environ.get("ROSTER_LIFECYCLE_PLAN")
        if value is None:
            return None
        try:
            plan = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"ROSTER_LIFECYCLE_PLAN is not valid JSON: {exc}") from exc
        if not isinstance(plan, dict):
            raise SystemExit("ROSTER_LIFECYCLE_PLAN must be an object")
        return plan

    def _legacy_outcome(self) -> dict[str, Any]:
        scripted = _next_scripted("ROSTER_CLIENT_OUTCOMES")
        if isinstance(scripted, dict):
            return dict(scripted)
        return {"outcome": scripted}

    def _argv_max_minutes(self) -> float:
        try:
            index = self.argv.index("--max")
            return max(0.001, float(self.argv[index + 1]))
        except (ValueError, IndexError):
            return 240.0

    def _argv_value(self, option: str, default: str) -> str:
        try:
            index = self.argv.index(option)
            return self.argv[index + 1]
        except (ValueError, IndexError):
            return default

    def _argv_seconds(self, option: str, default: int = 240) -> int:
        try:
            return max(1, int(self._argv_value(option, str(default)))) * 60
        except ValueError:
            return default * 60

    def _persist(self) -> None:
        self.state["clock_monotonic_s"] = self.clock.monotonic()
        _save_state(self.state)

    def _advance(self, seconds: float) -> None:
        seconds = max(0.0, seconds)
        if seconds:
            self.clock.advance(monotonic_s=seconds)
            _trace(self._identity("clock", advanced_s=seconds, monotonic_s=self.clock.monotonic()))
            self._persist()

    def _identity(self, event: str, **extra: Any) -> dict[str, Any]:
        value: dict[str, Any] = {
            "event": event,
            "lane": self.lane,
            "generation": self.generation,
            "token": self.token,
            "unit": self.unit,
            "invocation": self.invocation,
        }
        value.update(extra)
        return value

    def _step(self, operation: str) -> dict[str, Any]:
        if self.plan is None:
            return dict(self.legacy or {"outcome": "success"})
        raw = self.plan.get(operation, [{"outcome": "success"}])
        steps = raw if isinstance(raw, list) else [raw]
        if not steps:
            steps = [{"outcome": "success"}]
        index = int(self.state["script_indexes"].get(operation, 0))
        self.state["script_indexes"][operation] = index + 1
        self._persist()
        selected = steps[min(index, len(steps) - 1)]
        if isinstance(selected, str):
            return {"outcome": selected}
        if not isinstance(selected, dict):
            return {"outcome": "malformed"}
        return dict(selected)

    def _request_id(self, operation: str) -> str:
        return f"{self.arm_id}:{operation}"

    def _lane_ref(self, lane_id: str | None = None) -> dict[str, str]:
        return {"site_id": "site", "host_id": "host", "lane_id": lane_id or self.lane}

    def _principal_ref(self, subject: str | None = None) -> dict[str, str]:
        return {"site_id": "site", "tenant_id": "tenant", "issuer": "issuer", "subject": subject or self.principal}

    def _trace_envelope(self, response: dict[str, Any]) -> None:
        _trace(self._identity("rpc-envelope", response=response))

    def _response_status(self, response: dict[str, Any]) -> str:
        status = response.get("status")
        return {200: "ok", 403: "denied", 409: "conflict", 503: "timeout"}.get(status, "unknown")

    def _error_response(
        self,
        request_id: str,
        *,
        status: int,
        code: str,
        message: str,
        retryable: bool,
        failure_class: str,
    ) -> dict[str, Any]:
        return {
            "schema": 1,
            "request_id": request_id,
            "status": status,
            "data": None,
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
                "failure_class": failure_class,
            },
        }

    def _current_lane_matches(
        self,
        *,
        token: str | None = None,
        generation: int | None = None,
        unit: str | None = None,
        invocation: str | None = None,
    ) -> bool:
        # Hooks may complete after another owner has advanced the reservation.
        # Refresh the durable observation before any matching cleanup/release.
        if _state_file() is not None:
            self.state = _load_state()
        lane_state = self.state["lanes"].get(self.lane)
        if not isinstance(lane_state, dict):
            return False
        expected = {
            "token": self.token if token is None else token,
            "generation": self.generation if generation is None else generation,
            "unit": self.unit if unit is None else unit,
            "invocation": self.invocation if invocation is None else invocation,
        }
        return all(lane_state.get(key) == value for key, value in expected.items())

    def _lane_allows_acquire(self, operation: str) -> bool:
        lane_state = self.state["lanes"].get(self.lane)
        if not isinstance(lane_state, dict):
            return True
        state = lane_state.get("state")
        if operation == "claim" and state in {"starting", "running"} and lane_state.get("token") == self.input_token and lane_state.get("generation") == self.input_generation and lane_state.get("principal") == self.principal:
            return True
        return state not in {"reserved", "starting", "running", "stopping", "quarantined", "unknown"}

    def _rpc(self, operation: str, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str, str]:
        request_id = self._request_id(operation)
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        for attempt in range(self.retries + 1):
            _trace(self._identity("rpc-request", operation=operation, request_id=request_id, request_fingerprint=fingerprint, attempt=attempt))
            cached = self.state["requests"].get(request_id)
            if cached is not None:
                if cached.get("request_fingerprint") != fingerprint:
                    _trace(self._identity("rpc-response", operation=operation, request_id=request_id, status="conflict"))
                    return None, "conflict", request_id
                response = cached.get("response")
                if not isinstance(response, dict):
                    return None, "malformed", request_id
                self._trace_envelope(response)
                _trace(self._identity("rpc-replay", operation=operation, request_id=request_id, effect_count=cached.get("effect_count", 0)))
                return dict(response), self._response_status(response), request_id

            if operation in {"acquire", "claim"} and not self._lane_allows_acquire(operation):
                response = self._error_response(
                    request_id,
                    status=409,
                    code="conflict",
                    message="lane is occupied or quarantined",
                    retryable=False,
                    failure_class="conflict",
                )
                self._cache_request(request_id, fingerprint, response)
                self._trace_envelope(response)
                _trace(self._identity("rpc-response", operation=operation, request_id=request_id, status="conflict"))
                return response, "conflict", request_id

            if operation == "release" and not self._current_lane_matches(
                token=payload.get("token"),
                generation=payload.get("generation"),
                unit=payload.get("unit"),
                invocation=payload.get("invocation"),
            ):
                response = self._error_response(
                    request_id,
                    status=409,
                    code="fenced",
                    message="release identity does not match the current reservation",
                    retryable=False,
                    failure_class="conflict",
                )
                self._cache_request(request_id, fingerprint, response)
                self._trace_envelope(response)
                _trace(self._identity("rpc-response", operation=operation, request_id=request_id, status="conflict"))
                return response, "conflict", request_id

            step = self._step(operation)
            outcome = str(step.get("outcome", "success"))
            if outcome == "sleep":
                time.sleep(_number(os.environ.get("ROSTER_SHIM_SLEEP_S"), 1.0))
                return None, "timeout", request_id
            if outcome in {"acquire-conflict", "claim-conflict", "conflict"}:
                response = self._error_response(
                    request_id,
                    status=409,
                    code="conflict",
                    message="lane conflict",
                    retryable=False,
                    failure_class="conflict",
                )
                self._cache_request(request_id, fingerprint, response)
                self._trace_envelope(response)
                return response, "conflict", request_id
            if outcome in {"acquire-denied", "claim-denied", "denied", "wrong-principal-denied"}:
                response = self._error_response(
                    request_id,
                    status=403,
                    code="denied",
                    message="claim denied",
                    retryable=False,
                    failure_class="policy",
                )
                self._cache_request(request_id, fingerprint, response)
                self._trace_envelope(response)
                return response, "denied", request_id
            if outcome in {"acquire-timeout", "claim-timeout", "timeout"}:
                response = self._error_response(
                    request_id,
                    status=503,
                    code="timeout",
                    message="controller timeout",
                    retryable=True,
                    failure_class="timeout",
                )
                self._cache_request(request_id, fingerprint, response)
                self._trace_envelope(response)
                return response, "timeout", request_id
            if outcome == "lost-before-effect":
                _trace(self._identity("rpc-response", operation=operation, request_id=request_id, status="lost"))
                if attempt < self.retries:
                    continue
                return None, "lost", request_id

            if operation in {"acquire", "claim"}:
                response = self._grant_response(operation, step)
                if outcome == "malformed":
                    response["data"] = {"kind": "grant", "operation": operation}
            else:
                if any(step.get(key, value) != value for key, value in (("token", self.token), ("generation", self.generation), ("unit", self.unit), ("invocation", self.invocation))):
                    response = self._error_response(
                        request_id,
                        status=409,
                        code="fenced",
                        message="release identity does not match the current reservation",
                        retryable=False,
                        failure_class="conflict",
                    )
                else:
                    response = self._release_response(step)
            self._trace_envelope(response)

            if outcome in {"lost", "lost-after-effect", "release-lost"}:
                self._cache_request(request_id, fingerprint, response)
                if outcome in {"lost-after-effect", "release-lost"}:
                    if operation in {"acquire", "claim"} and self._validate_grant(response, operation, request_id):
                        self._mark_grant_effect(request_id, response)
                    elif operation == "release" and self._validate_release(response, request_id):
                        self._apply_release_effect(request_id, response)
                _trace(self._identity("rpc-response", operation=operation, request_id=request_id, status="lost"))
                if attempt < self.retries:
                    continue
                return None, "lost", request_id

            if outcome in {"release-unknown", "unknown", "release-timeout"}:
                response = self._error_response(
                    request_id,
                    status=503,
                    code="unknown",
                    message="release outcome unknown",
                    retryable=False,
                    failure_class="state",
                )
                self._cache_request(request_id, fingerprint, response)
                self._trace_envelope(response)
                _trace(self._identity("rpc-response", operation=operation, request_id=request_id, status="unknown"))
                return response, "unknown", request_id

            self._cache_request(request_id, fingerprint, response)
            _trace(self._identity("rpc-response", operation=operation, request_id=request_id, status=self._response_status(response)))
            return response, self._response_status(response), request_id
        return None, "lost", request_id

    def _cache_request(self, request_id: str, fingerprint: str, response: dict[str, Any]) -> None:
        self.state["requests"][request_id] = {"request_fingerprint": fingerprint, "response": response, "effect_count": 0}
        self._persist()

    def _grant_response(self, operation: str, step: dict[str, Any]) -> dict[str, Any]:
        lane_id = str(step.get("lane_id", self.lane))
        if operation == "claim":
            token = self.input_token or "token-abcdefghijklmnop"
            generation = self.input_generation or 1
        else:
            token = str(step.get("token", f"token-{self.arm_id.replace('_', '-')}-abcdefghijkl"))
            existing = self.state["lanes"].get(self.lane, {})
            generation = int(step.get("generation", existing.get("generation", 0) + 1))
        principal = str(step.get("principal", self.principal))
        if step.get("outcome") == "stale" and operation == "claim":
            generation = max(1, generation - 1)
        if step.get("outcome") == "wrong-principal":
            principal = "other-principal"
        mode = "authenticated-adoption" if operation == "claim" else "fresh-acquire"
        state = str(step.get("state", "running"))
        lease_id = str(step.get("lease_id", f"lease-{self.arm_id}-{generation}"))
        instance = str(step.get("instance", f"instance-{self.arm_id}-{generation}"))
        unit = str(step.get("unit", f"unit-{self.arm_id}-{generation}"))
        invocation = str(step.get("invocation", f"invocation-{self.arm_id}-{generation}"))
        lane = self._lane_ref(lane_id)
        reservation = {"lane": lane, "generation": generation, "state": state}
        lease = {
            "schema_version": 1,
            "lease_id": lease_id,
            "lane": lane,
            "generation": generation,
            "reservation": reservation,
            "token": token,
            "instance": instance,
            "principal": self._principal_ref(principal),
            "class": self._argv_value("--class", "batch"),
            "purpose": self.purpose,
            "estimated_s": self._argv_seconds("--est"),
            "started_at": "2026-09-28T00:00:00Z",
            "max_end": "2026-09-29T00:00:00Z",
            "approved_max_end": "2026-09-29T00:00:00Z",
            "heartbeat_at": "2026-09-28T00:00:00Z",
            "deadline": {
                "boot_id": "boot-a",
                "deadline_s": max(1, int(self.approved_max_s)),
                "utc_anchor": "2026-09-28T00:00:00Z",
                "monotonic_anchor_s": 0,
            },
            "booking_id": None,
            "unit": unit,
            "invocation": invocation,
            "state": state,
        }
        return {
            "schema": 1,
            "request_id": self._request_id(operation),
            "status": 200,
            "data": {
                "kind": "grant",
                "operation": operation,
                "token": token,
                "generation": generation,
                "lease": lease,
                "reservation": reservation,
                "adoption": {
                    "mode": mode,
                    "principal_bound": True,
                    "generation_bound": True,
                    "token_source": "authenticated-adoption" if operation == "claim" else "controller-grant",
                },
            },
            "error": None,
        }

    def _release_response(self, step: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": 1,
            "request_id": self._request_id("release"),
            "status": 200,
            "data": {
                "kind": "mutation",
                "operation": "release",
                "record_type": "lease",
                "record_id": self.lease_id or f"lease-{self.arm_id}-{self.generation}",
                "state": str(step.get("state", "free")),
                "revision": int(step.get("revision", 2)),
                "reservation": {
                    "lane": self._lane_ref(),
                    "generation": step.get("generation", self.generation),
                    "state": str(step.get("reservation_state", "released")),
                },
            },
            "error": None,
        }

    def _validate_grant(self, response: dict[str, Any], operation: str, request_id: str | None = None) -> bool:
        if not _is_frozen_response(response) or response.get("status") != 200 or (request_id is not None and response.get("request_id") != request_id):
            return False
        data = response.get("data")
        required = {"kind", "operation", "token", "generation", "lease", "reservation", "adoption"}
        if not isinstance(data, dict) or set(data) != required or data.get("kind") != "grant" or data.get("operation") != operation:
            return False
        token = data.get("token")
        generation = data.get("generation")
        if not isinstance(token, str) or not 16 <= len(token) <= 512 or not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            return False
        lease = data.get("lease")
        if not isinstance(lease, dict) or lease.get("principal", {}).get("subject") != self.principal:
            return False
        if lease.get("token") != token or lease.get("generation") != generation or lease.get("state") not in {"starting", "running"}:
            return False
        if lease.get("lane", {}).get("lane_id") != self.lane or lease.get("reservation") != data.get("reservation"):
            return False
        reservation = data.get("reservation")
        if not isinstance(reservation, dict) or set(reservation) != {"lane", "generation", "state"}:
            return False
        lane = reservation.get("lane")
        if not isinstance(lane, dict) or set(lane) != {"site_id", "host_id", "lane_id"} or lane.get("lane_id") != self.lane:
            return False
        if reservation.get("generation") != generation or reservation.get("state") not in {"reserved", "starting", "running"}:
            return False
        adoption = data.get("adoption")
        if not isinstance(adoption, dict) or set(adoption) != {"mode", "principal_bound", "generation_bound", "token_source"}:
            return False
        expected_mode = "authenticated-adoption" if operation == "claim" else "fresh-acquire"
        expected_source = "authenticated-adoption" if operation == "claim" else "controller-grant"
        if adoption != {"mode": expected_mode, "principal_bound": True, "generation_bound": True, "token_source": expected_source}:
            return False
        if not isinstance(lease.get("unit"), str) or not isinstance(lease.get("invocation"), str):
            return False
        if operation == "claim" and (token != self.input_token or generation != self.input_generation):
            return False
        return True

    def _validate_release(self, response: dict[str, Any], request_id: str | None = None) -> bool:
        if not _is_frozen_response(response) or response.get("status") != 200 or (request_id is not None and response.get("request_id") != request_id):
            return False
        data = response.get("data")
        if not isinstance(data, dict) or set(data) != {"kind", "operation", "record_type", "record_id", "state", "revision", "reservation"}:
            return False
        reservation = data.get("reservation")
        return (
            data.get("kind") == "mutation"
            and data.get("operation") == "release"
            and data.get("record_type") == "lease"
            and data.get("record_id") == self.lease_id
            and data.get("state") == "free"
            and isinstance(data.get("revision"), int)
            and not isinstance(data.get("revision"), bool)
            and data.get("revision", 0) >= 1
            and isinstance(reservation, dict)
            and reservation.get("lane", {}).get("lane_id") == self.lane
            and reservation.get("generation") == self.generation
            and reservation.get("state") == "released"
        )

    def _mark_grant_effect(self, request_id: str, response: dict[str, Any]) -> bool:
        cached = self.state["requests"].get(request_id)
        data = response["data"]
        lease = data["lease"]
        token = str(data["token"])
        generation = int(data["generation"])
        unit = str(lease["unit"])
        invocation = str(lease["invocation"])
        current = self.state["lanes"].get(self.lane)
        if isinstance(current, dict) and current.get("state") in {"reserved", "starting", "running", "stopping", "quarantined", "unknown"} and not all(current.get(key) == value for key, value in (("token", token), ("generation", generation), ("unit", unit), ("invocation", invocation))):
            _trace(self._identity("grant-rejected-occupied", request_id=request_id, current_generation=current.get("generation")))
            return False
        self.token = token
        self.generation = generation
        self.lease_id = str(lease["lease_id"])
        self.unit = unit
        self.invocation = invocation
        if cached is not None and cached.get("effect_count", 0):
            return True
        self.state["lanes"][self.lane] = {
            "state": "running",
            "generation": self.generation,
            "token": self.token,
            "principal": self.principal,
            "lease_id": self.lease_id,
            "unit": self.unit,
            "invocation": self.invocation,
            "occupants": [],
        }
        if cached is not None:
            cached["effect_count"] = int(cached.get("effect_count", 0)) + 1
        self._persist()
        _trace(self._identity("grant", request_id=request_id, effect_count=1))
        return True

    def _apply_release_effect(self, request_id: str, response: dict[str, Any]) -> bool:
        if not self._current_lane_matches():
            _trace(self._identity("release-rejected-current-identity", request_id=request_id))
            return False
        cached = self.state["requests"].get(request_id)
        if cached is not None and cached.get("effect_count", 0):
            return True
        lane_state = self.state["lanes"].get(self.lane)
        if not isinstance(lane_state, dict):
            return False
        lane_state["state"] = "free"
        lane_state["occupants"] = []
        if cached is not None:
            cached["effect_count"] = int(cached.get("effect_count", 0)) + 1
        _trace(self._identity("release", request_id=request_id))
        self._persist()
        return True

    def acquire(self) -> int:
        operation = "claim" if self.input_token else "acquire"
        if self.input_token and self.input_generation is None:
            _trace(self._identity("claim-invalid-input"))
            return 2
        self._trace_operation(operation)
        payload = {
            "lane": self.lane,
            "purpose": self.purpose,
            "principal": self.principal,
            "class": self._argv_value("--class", "batch"),
            "est_s": self._argv_seconds("--est"),
            "max_s": self._argv_seconds("--max"),
        }
        if operation == "claim":
            payload.update({"token": self.input_token, "generation": self.input_generation})
        response, status, request_id = self._rpc(operation, payload)
        if status != "ok" or response is None:
            return {"conflict": 1, "denied": 2, "timeout": 3, "lost": 3, "unknown": 3}.get(status, 3)
        if not self._validate_grant(response, operation, request_id):
            _trace(self._identity("grant-rejected", operation=operation, request_id=request_id))
            return 3
        if self.state["requests"].get(request_id, {}).get("workload_started"):
            _trace(self._identity("workload-replay-rejected", request_id=request_id))
            return 3
        return 0 if self._mark_grant_effect(request_id, response) else 3

    def _trace_operation(self, operation: str) -> None:
        _trace(self._identity(operation, purpose=self.purpose, arm_id=self.arm_id))

    def run_workload(self) -> int:
        operation = "claim" if self.input_token else "acquire"
        self.state["requests"][self._request_id(operation)]["workload_started"] = True
        self._persist()
        for phase in ("loading", "gate"):
            if self.plan is not None and phase in self.plan:
                step = self._step(phase)
                if not self._consume_runtime(phase, step):
                    return 3
        _trace(self._identity("workload", argv=self.workload, batch_id=self.batch_id))
        step = self._step("workload")
        outcome = str(step.get("outcome", "success"))
        if self.plan is None and self.legacy and self.legacy.get("outcome") == "sleep":
            time.sleep(_number(os.environ.get("ROSTER_SHIM_SLEEP_S"), 1.0))
            return 3
        if not self._consume_runtime("workload", step):
            return 3
        hook = os.environ.get("ROSTER_WORKLOAD_HOOK")
        if hook:
            env = os.environ.copy()
            env.update({"ROSTER_OWNER_UNIT": str(self.unit), "ROSTER_OWNER_INVOCATION": str(self.invocation)})
            try:
                process = subprocess.Popen([hook, *self.workload], env=env)
            except OSError:
                return 3
            command_line = _proc_cmdline(process.pid)
            _trace(self._identity("workload-process", pid=process.pid, command_line=command_line))
            try:
                process.wait(timeout=_number(os.environ.get("ROSTER_HOOK_TIMEOUT_S"), 5.0))
            except subprocess.TimeoutExpired:
                identity_ok = hook in _proc_cmdline(process.pid) or hook in command_line
                _trace(self._identity("workload-timeout", pid=process.pid, identity_checked=identity_ok))
                if identity_ok:
                    process.terminate()
                    process.wait(timeout=5)
                else:
                    return 3
            if process.returncode != 0:
                return 3
        if outcome in {"failure", "workload-failed"}:
            return 3
        return 0

    def _consume_runtime(self, phase: str, step: dict[str, Any]) -> bool:
        remaining_s = max(0.0, self.approved_max_s - (self.clock.monotonic() - self.started_s))
        duration_s = _number(step.get("duration_s"), 0.0)
        self._advance(min(duration_s, remaining_s))
        if duration_s > remaining_s or remaining_s == 0:
            _trace(self._identity("runtime-expired", phase=phase))
            return False
        return True

    def _quarantine(self, reason: str) -> int:
        if not self._current_lane_matches():
            _trace(self._identity("quarantine-rejected-current-identity"))
            return 3
        lane_state = self.state["lanes"].get(self.lane)
        if isinstance(lane_state, dict):
            lane_state["state"] = "quarantined"
            lane_state["quarantine_reason"] = reason
        _trace(self._identity("quarantine", reason=reason))
        self._persist()
        return 3

    def _heartbeat(self, step: dict[str, Any]) -> bool:
        outcome = str(step.get("outcome", "success"))
        if outcome in {"denied", "unknown", "timeout", "heartbeat-failed"}:
            _trace(self._identity("heartbeat-failed", outcome=outcome))
            return False
        _trace(self._identity("heartbeat", monotonic_s=self.clock.monotonic()))
        return True

    def _cleanup_hook(self) -> bool:
        hook = os.environ.get("ROSTER_CLEANUP_HOOK")
        if not hook:
            return True
        try:
            process = subprocess.Popen([hook, str(self.unit), str(self.invocation)], env=os.environ.copy())
            process.wait(timeout=_number(os.environ.get("ROSTER_HOOK_TIMEOUT_S"), 5.0))
        except (OSError, subprocess.TimeoutExpired):
            return False
        return process.returncode == 0

    def cleanup(self) -> int:
        if self.token is None or self.unit is None or self.invocation is None:
            _trace(self._identity("cleanup-before-grant"))
            return 3
        start = self.clock.monotonic()
        while True:
            if not self._current_lane_matches():
                _trace(self._identity("cleanup-rejected-current-identity"))
                return 3
            step = self._step("cleanup")
            outcome = str(step.get("outcome", "success"))
            identity_matches = all(step.get(key, value) == value for key, value in (("token", self.token), ("generation", self.generation), ("unit", self.unit), ("invocation", self.invocation)))
            occupants = step.get("occupants", [])
            if not isinstance(occupants, list):
                occupants = ["unknown"]
            _trace(self._identity("cleanup-attempt", outcome=outcome, occupants=occupants, identity_checked=identity_matches))
            if not identity_matches:
                _trace(self._identity("cleanup-rejected-stale"))
                return 3
            if not self._cleanup_hook():
                return self._quarantine("cleanup identity or hook was not confirmed")
            if not self._current_lane_matches():
                _trace(self._identity("cleanup-rejected-current-identity"))
                return 3
            if outcome in {"success", "confirmed"} and not occupants:
                self.cleanup_confirmed = True
                lane_state = self.state["lanes"].get(self.lane)
                if isinstance(lane_state, dict):
                    lane_state["state"] = "stopping"
                    lane_state["occupants"] = []
                _trace(self._identity("cleanup"))
                self._persist()
                return 0
            if outcome not in {"pending", "occupied", "cleanup-pending"}:
                return self._quarantine("cleanup outcome was unknown")
            lane_state = self.state["lanes"].get(self.lane)
            if isinstance(lane_state, dict):
                lane_state["state"] = "stopping"
                lane_state["occupants"] = occupants
            self._persist()
            _trace(self._identity("cleanup-pending", occupants=occupants))
            if self.plan is None:
                self._heartbeat({"outcome": "success"})
                return self._quarantine("cleanup remained pending")
            heartbeat = self._step("heartbeat")
            if not self._heartbeat(heartbeat):
                return self._quarantine("heartbeat was not confirmed")
            step_s = _number(step.get("advance_s"), _number(os.environ.get("ROSTER_CLOCK_STEP_S"), 1.0))
            if step_s <= 0:
                return self._quarantine("cleanup polling made no clock progress")
            self._advance(step_s)
            elapsed = self.clock.monotonic() - start
            if elapsed > self.cleanup_max_s or self.clock.monotonic() - self.started_s > self.approved_max_s + self.cleanup_allowance_s:
                return self._quarantine("cleanup allowance expired")

    def release(self) -> int:
        if not self.cleanup_confirmed:
            _trace(self._identity("release-before-cleanup"))
            return self._quarantine("release attempted before cleanup")
        _trace(self._identity("release-request", purpose=self.purpose, arm_id=self.arm_id))
        response, status, request_id = self._rpc("release", {"lane": self.lane, "token": self.token, "generation": self.generation, "unit": self.unit, "invocation": self.invocation})
        if status == "unknown":
            return self._quarantine("release outcome is unknown")
        if status != "ok" or response is None:
            return 3
        if not self._validate_release(response, request_id):
            return self._quarantine("release identity was not confirmed")
        return 0 if self._apply_release_effect(request_id, response) else 3


def client_main() -> int:
    argv = sys.argv[1:]
    _trace({"event": "client-argv", "argv": argv, "token": os.environ.get("LANE_TOKEN"), "generation": os.environ.get("LANE_GENERATION")})
    if not argv or argv[0] != "run":
        return 2
    try:
        separator = argv.index("--")
    except ValueError:
        return 2
    if separator < 2 or not argv[separator + 1 :]:
        return 2
    owner = LifecycleOwner(argv)
    try:
        acquire_code = owner.acquire()
        if acquire_code:
            return acquire_code
        workload_code = owner.run_workload()
        cleanup_code = owner.cleanup()
        if cleanup_code:
            return cleanup_code
        release_code = owner.release()
        return release_code or workload_code
    finally:
        owner._persist()


def workload_main() -> int:
    _trace({"event": "injected-workload", "argv": sys.argv[1:], "pid": os.getpid(), "unit": os.environ.get("ROSTER_OWNER_UNIT"), "invocation": os.environ.get("ROSTER_OWNER_INVOCATION")})
    delay = _number(os.environ.get("ROSTER_WORKLOAD_HOOK_SLEEP_S"), 0.0)
    if delay:
        time.sleep(delay)
    try:
        return int(os.environ.get("ROSTER_WORKLOAD_HOOK_EXIT", "0"))
    except ValueError:
        return 3


def cleanup_main() -> int:
    _trace({"event": "injected-cleanup", "argv": sys.argv[1:], "pid": os.getpid(), "unit": os.environ.get("ROSTER_OWNER_UNIT"), "invocation": os.environ.get("ROSTER_OWNER_INVOCATION")})
    try:
        return int(os.environ.get("ROSTER_CLEANUP_HOOK_EXIT", "0"))
    except ValueError:
        return 3


def main() -> int:
    mode = os.environ.get("ROSTER_SHIM_MODE")
    if mode == "bridge":
        return bridge_main()
    if mode == "client":
        return client_main()
    if mode == "workload":
        return workload_main()
    if mode == "cleanup":
        return cleanup_main()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
