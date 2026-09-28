from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Executable shims are launched by an absolute path, so Python's initial path
# contains tests/roster rather than the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

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


def bridge_main() -> int:
    raw = sys.stdin.read()
    try:
        request = json.loads(raw)
    except json.JSONDecodeError:
        return 2
    if not isinstance(request, dict):
        return 2
    scripted = _next_scripted("ROSTER_BRIDGE_OUTCOMES")
    outcome = scripted.get("outcome", "success") if isinstance(scripted, dict) else scripted
    response = _response_for(request, scripted)
    if request.get("op") == "queue" and request.get("args", {}).get("action") == "add":
        batch = request.get("admission", {}).get("batch", {})
        arms = batch.get("arms", []) if isinstance(batch, dict) else []
        eligible = [arm["arm_id"] for arm in arms if arm.get("predecessor") is None and not arm.get("dependencies")]
        _trace({"event": "queue-observation", "visible": [arm.get("arm_id") for arm in arms], "eligible": eligible})
    _trace({"event": "bridge-request", "request": request, "outcome": outcome})
    transport = FakeTransport([{"outcome": str(outcome), "response": response}])
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
                _trace(self._identity("rpc-replay", operation=operation, request_id=request_id, effect_count=cached.get("effect_count", 0)))
                return (dict(response) if isinstance(response, dict) else None), "ok", request_id

            step = self._step(operation)
            outcome = str(step.get("outcome", "success"))
            if outcome == "sleep":
                time.sleep(_number(os.environ.get("ROSTER_SHIM_SLEEP_S"), 1.0))
                return None, "timeout", request_id
            if outcome in {"acquire-conflict", "claim-conflict", "conflict"}:
                response = {"status": "conflict", "message": "lane conflict"}
                self._cache_request(request_id, fingerprint, response)
                return response, "conflict", request_id
            if outcome in {"acquire-denied", "claim-denied", "denied", "wrong-principal-denied"}:
                response = {"status": "denied", "message": "claim denied"}
                self._cache_request(request_id, fingerprint, response)
                return response, "denied", request_id
            if outcome in {"acquire-timeout", "claim-timeout", "timeout"}:
                response = {"status": "timeout", "message": "controller timeout"}
                self._cache_request(request_id, fingerprint, response)
                return response, "timeout", request_id
            if outcome == "lost-before-effect":
                _trace(self._identity("rpc-response", operation=operation, request_id=request_id, status="lost"))
                if attempt < self.retries:
                    continue
                return None, "lost", request_id

            if operation in {"acquire", "claim"}:
                response = self._grant_response(operation, step)
                if outcome == "malformed":
                    response = {"kind": "grant", "operation": operation}
            else:
                response = self._release_response(step)

            if outcome in {"lost", "lost-after-effect", "release-lost"}:
                self._cache_request(request_id, fingerprint, response)
                _trace(self._identity("rpc-response", operation=operation, request_id=request_id, status="lost"))
                if attempt < self.retries:
                    continue
                return None, "lost", request_id

            if outcome in {"release-unknown", "unknown", "release-timeout"}:
                response = {"status": "unknown", "message": "release outcome unknown"}
                self._cache_request(request_id, fingerprint, response)
                _trace(self._identity("rpc-response", operation=operation, request_id=request_id, status="unknown"))
                return response, "unknown", request_id

            self._cache_request(request_id, fingerprint, response)
            _trace(self._identity("rpc-response", operation=operation, request_id=request_id, status="ok"))
            return response, "ok", request_id
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
        return {
            "kind": "grant",
            "operation": operation,
            "token": token,
            "generation": generation,
            "principal": principal,
            "reservation": {
                "lane": {"site_id": "site", "host_id": "host", "lane_id": lane_id},
                "generation": generation,
                "state": str(step.get("state", "running")),
            },
            "adoption": {
                "mode": mode,
                "principal_bound": True,
                "generation_bound": True,
                "token_source": "authenticated-adoption" if operation == "claim" else "controller-grant",
            },
            "unit": str(step.get("unit", f"unit-{self.arm_id}-{generation}")),
            "invocation": str(step.get("invocation", f"invocation-{self.arm_id}-{generation}")),
        }

    def _release_response(self, step: dict[str, Any]) -> dict[str, Any]:
        return {
            "kind": "release",
            "operation": "release",
            "token": step.get("token", self.token),
            "generation": step.get("generation", self.generation),
            "unit": step.get("unit", self.unit),
            "invocation": step.get("invocation", self.invocation),
            "state": str(step.get("state", "free")),
        }

    def _validate_grant(self, response: dict[str, Any], operation: str) -> bool:
        required = {"kind", "operation", "token", "generation", "principal", "reservation", "adoption", "unit", "invocation"}
        if set(response) != required or response.get("kind") != "grant" or response.get("operation") != operation:
            return False
        token = response.get("token")
        generation = response.get("generation")
        if not isinstance(token, str) or not 16 <= len(token) <= 512 or not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            return False
        if response.get("principal") != self.principal:
            return False
        reservation = response.get("reservation")
        if not isinstance(reservation, dict) or set(reservation) != {"lane", "generation", "state"}:
            return False
        lane = reservation.get("lane")
        if not isinstance(lane, dict) or set(lane) != {"site_id", "host_id", "lane_id"} or lane.get("lane_id") != self.lane:
            return False
        if reservation.get("generation") != generation or reservation.get("state") not in {"reserved", "starting", "running"}:
            return False
        adoption = response.get("adoption")
        if not isinstance(adoption, dict) or set(adoption) != {"mode", "principal_bound", "generation_bound", "token_source"}:
            return False
        expected_mode = "authenticated-adoption" if operation == "claim" else "fresh-acquire"
        expected_source = "authenticated-adoption" if operation == "claim" else "controller-grant"
        if adoption != {"mode": expected_mode, "principal_bound": True, "generation_bound": True, "token_source": expected_source}:
            return False
        if not isinstance(response.get("unit"), str) or not isinstance(response.get("invocation"), str):
            return False
        if operation == "claim" and (token != self.input_token or generation != self.input_generation):
            return False
        return True

    def _mark_grant_effect(self, request_id: str, response: dict[str, Any]) -> None:
        cached = self.state["requests"].get(request_id)
        self.token = str(response["token"])
        self.generation = int(response["generation"])
        self.unit = str(response["unit"])
        self.invocation = str(response["invocation"])
        if cached is not None and cached.get("effect_count", 0):
            return
        self.state["lanes"][self.lane] = {
            "state": "running",
            "generation": self.generation,
            "token": self.token,
            "principal": self.principal,
            "unit": self.unit,
            "invocation": self.invocation,
            "occupants": [],
        }
        if cached is not None:
            cached["effect_count"] = int(cached.get("effect_count", 0)) + 1
        self._persist()
        _trace(self._identity("grant", request_id=request_id, effect_count=1))

    def acquire(self) -> int:
        operation = "claim" if self.input_token else "acquire"
        if self.input_token and self.input_generation is None:
            _trace(self._identity("claim-invalid-input"))
            return 2
        self._trace_operation(operation)
        payload = {"lane": self.lane, "purpose": self.purpose, "principal": self.principal}
        if operation == "claim":
            payload.update({"token": self.input_token, "generation": self.input_generation})
        response, status, request_id = self._rpc(operation, payload)
        if status != "ok" or response is None:
            return {"conflict": 1, "denied": 2, "timeout": 3, "lost": 3, "unknown": 3}.get(status, 3)
        if not self._validate_grant(response, operation):
            _trace(self._identity("grant-rejected", operation=operation, request_id=request_id))
            return 3
        self._mark_grant_effect(request_id, response)
        return 0

    def _trace_operation(self, operation: str) -> None:
        _trace(self._identity(operation, purpose=self.purpose, arm_id=self.arm_id))

    def run_workload(self) -> int:
        _trace(self._identity("workload", argv=self.workload, batch_id=self.batch_id))
        step = self._step("workload")
        outcome = str(step.get("outcome", "success"))
        if self.plan is None and self.legacy and self.legacy.get("outcome") == "sleep":
            time.sleep(_number(os.environ.get("ROSTER_SHIM_SLEEP_S"), 1.0))
            return 3
        self._advance(_number(step.get("duration_s"), 0.0))
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

    def _quarantine(self, reason: str) -> int:
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
            step = self._step("cleanup")
            outcome = str(step.get("outcome", "success"))
            identity_matches = all(step.get(key, value) == value for key, value in (("token", self.token), ("generation", self.generation), ("unit", self.unit), ("invocation", self.invocation)))
            occupants = step.get("occupants", [])
            if not isinstance(occupants, list):
                occupants = ["unknown"]
            _trace(self._identity("cleanup-attempt", outcome=outcome, occupants=occupants, identity_checked=identity_matches))
            if not identity_matches or not self._cleanup_hook():
                return self._quarantine("cleanup identity or hook was not confirmed")
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
            _trace(self._identity("cleanup-pending", occupants=occupants))
            if self.plan is None:
                self._heartbeat({"outcome": "success"})
                return self._quarantine("cleanup remained pending")
            heartbeat = self._step("heartbeat")
            if not self._heartbeat(heartbeat):
                return self._quarantine("heartbeat was not confirmed")
            step_s = _number(step.get("advance_s"), _number(os.environ.get("ROSTER_CLOCK_STEP_S"), 1.0))
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
        expected = {"kind", "operation", "token", "generation", "unit", "invocation", "state"}
        if set(response) != expected or response.get("kind") != "release" or response.get("operation") != "release" or response.get("token") != self.token or response.get("generation") != self.generation or response.get("unit") != self.unit or response.get("invocation") != self.invocation or response.get("state") != "free":
            return self._quarantine("release identity was not confirmed")
        lane_state = self.state["lanes"].get(self.lane)
        if isinstance(lane_state, dict):
            lane_state["state"] = "free"
            lane_state["occupants"] = []
        _trace(self._identity("release", request_id=request_id))
        self._persist()
        return 0


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
