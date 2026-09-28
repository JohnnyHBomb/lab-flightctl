"""Fenced host-side execution for the frozen executor v1 contract.

The executor deliberately has no transport or privilege implementation.  A
controller supplies an authenticated context and injects the host clock,
Systemd adapter, and GPU probe.  The state file is the durable authority for
generation fences; an uncertain observation never turns that authority into a
free lane.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from contracts.interfaces import Clock, GPUProbe, Systemd


AUTH_UNSET = object()
_MISSING = object()
_KNOWN_SYSTEMD_FAILURES = {
    "denied",
    "failure",
    "failed",
    "failed_start",
    "failed_stop",
    "invocation_mismatch",
    "lost",
    "timeout",
    "unknown",
}
_KNOWN_SUCCESS_STATUSES = {"ok", "success"}
_ACTIVE_STATES = {"starting", "running", "stopping", "quarantined"}
_EXECUTION_CLASSES = {"operator", "booked", "batch", "service", "resident", "standby"}


class StateCorruptError(RuntimeError):
    """Raised internally when the durable state is not a valid JSON object."""


class TrustedController:
    """Opaque controller context used by tests and local callers.

    The request mapping never creates or upgrades this object.  The transport
    boundary is expected to retain the instance supplied to ``Executor`` and
    pass that same instance to ``handle`` when it wants per-call checking.
    """

    __slots__ = ("controller_id", "_proof")

    def __init__(self, controller_id: str, proof: object | None = None) -> None:
        self.controller_id = controller_id
        self._proof = proof if proof is not None else object()


class JsonStateStore:
    """Small atomic JSON store used for the operator-configured state path."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "lanes": {}, "boot_id": None, "clock": None}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateCorruptError(f"cannot read executor state: {exc}") from exc
        if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("lanes"), dict):
            raise StateCorruptError("executor state has an unsupported shape")
        return value

    def save(self, state: Mapping[str, Any]) -> None:
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=str(parent), text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise


class MemoryStateStore:
    """Explicitly non-durable store for callers that only need a unit seam."""

    def __init__(self) -> None:
        self.value: dict[str, Any] | None = None

    def load(self) -> dict[str, Any]:
        if self.value is None:
            return {"version": 1, "lanes": {}, "boot_id": None, "clock": None}
        return copy.deepcopy(self.value)

    def save(self, state: Mapping[str, Any]) -> None:
        self.value = copy.deepcopy(dict(state))


class Executor:
    """Controller-only, fenced executor with host-local deadline enforcement.

    ``trusted_controller`` is an injected transport/authentication context, not
    a request field.  If it is omitted, every request is rejected.  Supplying
    a ``controller_authorizer`` is useful when the real transport has a
    verifier rather than a stable opaque context.
    """

    heartbeat_s = 60.0
    protected_stale_s = 600.0
    preemptible_stale_s = 180.0
    service_grace_s = 120.0
    standby_grace_s = 300.0

    def __init__(
        self,
        clock: Clock,
        systemd: Systemd,
        gpu_probe: GPUProbe,
        state_path: str | os.PathLike[str] | None = None,
        trusted_controller: object | None = None,
        *,
        state_store: Any | None = None,
        controller_authorizer: Callable[[object], bool] | None = None,
        approval_checker: Callable[..., bool] | None = None,
        approved_forced_preemptions: Iterable[str] | Mapping[str, object] | None = None,
        operation_timeout_s: float = 2.0,
        clock_skew_s: float = 30.0,
    ) -> None:
        if state_store is not None and state_path is not None:
            raise ValueError("choose state_path or state_store, not both")
        self.clock = clock
        self.systemd = systemd
        self.gpu_probe = gpu_probe
        self._store = state_store if state_store is not None else (JsonStateStore(state_path) if state_path is not None else MemoryStateStore())
        self._trusted_controller = trusted_controller
        self._controller_authorizer = controller_authorizer
        self._approval_checker = approval_checker
        self._approved_forced_preemptions = approved_forced_preemptions
        self._operation_timeout_s = max(0.01, float(operation_timeout_s))
        self._clock_skew_s = float(clock_skew_s)
        self._lock = threading.RLock()
        self._inflight_starts: set[tuple[str, int]] = set()
        self._start_operations: dict[tuple[str, int], dict[str, object]] = {}
        self._inflight_stops: set[tuple[str, int]] = set()
        self._state_error: str | None = None
        try:
            self._state = self._store.load()
            self._initialise_clock_and_reboot()
        except (StateCorruptError, OSError, TypeError, ValueError) as exc:
            self._state = {"version": 1, "lanes": {}, "boot_id": None, "clock": None}
            self._state_error = str(exc)

    # ------------------------------------------------------------------
    # Public entry points and read-only state

    def handle(
        self,
        request: Mapping[str, object],
        authenticated_controller: object = AUTH_UNSET,
        controller: object = AUTH_UNSET,
        trusted_controller: object = AUTH_UNSET,
    ) -> dict[str, object]:
        """Execute one frozen request and return one frozen reply mapping."""

        auth = authenticated_controller if authenticated_controller is not AUTH_UNSET else controller
        if auth is AUTH_UNSET:
            auth = trusted_controller
        with self._lock:
            if auth is AUTH_UNSET and self._trusted_controller is not None:
                auth = self._trusted_controller
            if not self._authenticated(auth):
                return self._early_rejection(request, "controller authentication required")
            if self._state_error is not None:
                return self._early_rejection(request, f"executor state unavailable: {self._state_error}", uncertain=True)
            try:
                self._sample_clock_locked()
                kind = self._validate_request_shape(request)
            except ValueError as exc:
                return self._early_rejection(request, str(exc))

        if kind == "reserve":
            return self._reserve(request)
        if kind == "start":
            return self._start(request)
        if kind == "beat":
            return self._beat(request)
        if kind == "stop":
            return self._stop(request)
        if kind == "inspect":
            return self._inspect(request)
        return self._early_rejection(request, f"unsupported executor operation: {kind}")

    execute = handle
    process = handle
    handle_request = handle

    def state_snapshot(self, lane: Mapping[str, object] | str) -> dict[str, object]:
        """Return a copy of durable lane state without causing host side effects."""

        key = _lane_key(lane)
        with self._lock:
            record = self._state.get("lanes", {}).get(key)
            return copy.deepcopy(record) if isinstance(record, dict) else {"state": "free", "generation": 0, "closed_generation": 0}

    snapshot = state_snapshot

    def clock_status(self) -> dict[str, object]:
        with self._lock:
            return copy.deepcopy(self._state.get("clock") or {})

    def resynchronise_clock(self) -> None:
        """Explicitly accept the current UTC/monotonic anchor after a jump."""

        with self._lock:
            now = self._read_clock()
            self._state["clock"] = {
                "utc": _utc_text(now[0]),
                "monotonic_s": now[1],
                "boot_id": now[2],
                "frozen": False,
                "skew_s": 0.0,
            }
            self._state["boot_id"] = now[2]
            self._save_locked()

    resync_clock = resynchronise_clock

    def reconcile(self) -> dict[str, dict[str, object]]:
        """Reconcile active fences after a reboot without authorising a kill."""

        with self._lock:
            if self._state_error is not None:
                return {}
            pending = [copy.deepcopy((key, record)) for key, record in self._state.get("lanes", {}).items() if isinstance(record, dict) and record.get("reconcile_required") and record.get("state") in _ACTIVE_STATES]

        results: dict[str, dict[str, object]] = {}
        for key, record in pending:
            identity = record.get("identity")
            if not isinstance(identity, dict) or not identity.get("unit") or not identity.get("invocation"):
                result = {"ok": False, "state": "quarantined", "error": "reboot requires an exact unit invocation"}
                self._quarantine(key, result["error"])
                results[key] = result
                continue
            unit = str(identity["unit"])
            invocation = str(identity["invocation"])
            systemd_result = self._bounded_call(self.systemd.inspect, unit, invocation)
            gpu_result = self._bounded_call(self.gpu_probe.inspect, str(record["lane"]["host_id"]))
            systemd_ok, systemd_reason, cgroup, gpu_from_systemd = self._inspect_active_result(systemd_result, unit, invocation)
            gpu_ok, gpu_reason, tenants = self._active_gpu_observation(gpu_result)
            if not systemd_ok or not gpu_ok:
                reason = "; ".join(part for part in (systemd_reason, gpu_reason) if part) or "reconcile observation unknown"
                self._quarantine(key, reason)
                results[key] = {"ok": False, "state": "quarantined", "error": reason, "cgroup_occupants": cgroup, "gpu_tenants": tenants}
                continue
            with self._lock:
                current = self._state.get("lanes", {}).get(key)
                if not isinstance(current, dict) or current.get("generation") != record.get("generation") or current.get("identity") != record.get("identity"):
                    results[key] = {"ok": False, "state": "quarantined", "error": "fence changed during reconcile"}
                    continue
                current["reconcile_required"] = False
                deadline_error = self._rebase_after_reboot_locked(current)
                if deadline_error is not None:
                    current["state"] = "quarantined"
                    current["quarantine_reason"] = deadline_error
                    self._close_record_locked(current)
                    self._save_locked()
                    results[key] = {
                        "ok": False,
                        "state": "quarantined",
                        "error": deadline_error,
                        "cgroup_occupants": cgroup,
                        "gpu_tenants": tenants,
                    }
                    continue
                if current.get("state") == "quarantined" and not current.get("quarantine_reason"):
                    current["state"] = "running"
                self._save_locked()
                state = str(current.get("state", "running"))
            results[key] = {"ok": True, "state": state, "cgroup_occupants": cgroup, "gpu_tenants": tenants}
        return results

    def enforce_deadlines(self) -> list[dict[str, object]]:
        """Apply same-boot monotonic deadlines while the controller is absent."""

        with self._lock:
            if self._state_error is not None:
                return []
            self._sample_clock_locked()
            now_mono = self._read_clock()[1]
            candidates: list[tuple[str, dict[str, object], str]] = []
            for key, record in self._state.get("lanes", {}).items():
                if not isinstance(record, dict) or record.get("state") not in {"starting", "running"}:
                    continue
                if record.get("reconcile_required"):
                    continue
                due_reason = self._deadline_reason(record, now_mono)
                if due_reason is not None:
                    candidates.append((key, copy.deepcopy(record), due_reason))

        replies: list[dict[str, object]] = []
        for key, record, reason in candidates:
            policy = record.get("policy")
            protected = isinstance(policy, dict) and bool(policy.get("protected"))
            if protected:
                self._quarantine(key, f"protected work deadline exceeded: {reason}")
                replies.append(self._internal_result(key, "quarantined", f"protected work retained: {reason}"))
                continue
            grace = self._grace_seconds(record)
            with self._lock:
                current = self._state.get("lanes", {}).get(key)
                if not isinstance(current, dict) or current.get("generation") != record.get("generation") or current.get("state") not in {"starting", "running"}:
                    continue
                now_mono = self._read_clock()[1]
                started = current.get("grace_started_mono")
                if grace > 0 and not isinstance(started, (int, float)):
                    current["grace_started_mono"] = now_mono
                    current["grace_reason"] = reason
                    self._save_locked()
                    replies.append(self._internal_result(key, "running", f"grace started: {reason}"))
                    continue
                if grace > 0 and isinstance(started, (int, float)) and now_mono < float(started) + grace:
                    replies.append(self._internal_result(key, "running", f"grace active: {reason}"))
                    continue
            reply = self._stop_record(key, record, reason=reason, internal=True)
            replies.append(reply)
        return replies

    tick = enforce_deadlines
    enforce = enforce_deadlines

    # ------------------------------------------------------------------
    # Request handlers

    def _reserve(self, request: Mapping[str, object]) -> dict[str, object]:
        identity = _dict(request.get("identity"))
        policy = _dict(request.get("execution_policy"))
        lane = _dict(identity.get("lane"))
        key = _lane_key(lane)
        with self._lock:
            if self._clock_frozen_locked():
                return self._reply(request, "rejected", False, self._lane_state(key), False, "clock uncertainty freezes admission")
            if identity.get("deadline", {}).get("boot_id") != self._read_clock()[2]:
                return self._reply(request, "rejected", False, "unknown", False, "deadline belongs to another boot")
            record = self._state["lanes"].get(key)
            if isinstance(record, dict) and record.get("state") in _ACTIVE_STATES:
                if self._same_reservation(record, identity, policy):
                    return self._reply(request, "reserved", True, "starting", False, None)
                return self._reply(request, "rejected", False, str(record.get("state", "unknown")), False, "lane already fenced")
            if isinstance(record, dict) and record.get("state") not in {None, "free"}:
                return self._reply(request, "rejected", False, str(record.get("state", "unknown")), False, "lane is not safely free")
            previous_generation = int(record.get("generation", 0)) if isinstance(record, dict) else 0
            closed_generation = int(record.get("closed_generation", 0)) if isinstance(record, dict) else 0
            generation = _positive_int(identity.get("generation"))
            if generation <= max(previous_generation, closed_generation):
                return self._reply(request, "rejected", False, "free", False, "stale or closed generation")
            closed_tokens = list(record.get("closed_tokens", [])) if isinstance(record, dict) else []
            if identity.get("token") in closed_tokens:
                return self._reply(request, "rejected", False, "free", False, "token was already closed")
            if self._reservation_token_owner(key, str(identity.get("token"))) is not None:
                return self._reply(request, "rejected", False, "free", False, "token is already fenced by another generation")
            now = self._read_clock()
            self._state["lanes"][key] = {
                "lane": copy.deepcopy(lane),
                "generation": generation,
                "closed_generation": closed_generation,
                "closed_tokens": closed_tokens,
                "closed_invocations": list(record.get("closed_invocations", [])) if isinstance(record, dict) else [],
                "state": "starting",
                "identity": copy.deepcopy(identity),
                "reservation_identity": copy.deepcopy(identity),
                "policy": copy.deepcopy(policy),
                "workload": None,
                "start_attempted": False,
                "start_in_flight": False,
                "start_uncertain": False,
                "start_result_ok": False,
                "stop_requested": False,
                "stop_attempted": False,
                "stop_result_ok": False,
                "last_heartbeat_mono": now[1],
                "last_heartbeat_utc": _utc_text(now[0]),
                "last_heartbeat_boot_id": now[2],
                "grace_started_mono": None,
                "grace_reason": None,
                "reconcile_required": False,
                "quarantine_reason": None,
                "reserve_request_id": request.get("controller_request_id"),
            }
            self._save_locked()
        return self._reply(request, "reserved", True, "starting", False, None)

    def _start(self, request: Mapping[str, object]) -> dict[str, object]:
        identity = _dict(request.get("identity"))
        lane = _dict(identity.get("lane"))
        key = _lane_key(lane)
        policy = _dict(request.get("execution_policy"))
        workload = _dict(request.get("workload"))
        inspect_existing = False
        generation_key = (key, int(identity["generation"]))
        with self._lock:
            if self._clock_frozen_locked():
                return self._reply(request, "rejected", False, self._lane_state(key), False, "clock uncertainty freezes admission")
            record = self._state["lanes"].get(key)
            if not isinstance(record, dict):
                return self._reply(request, "rejected", False, "free", False, "start has no reservation")
            if not self._start_identity_allowed(record, identity, policy, workload):
                return self._reply(request, "rejected", False, str(record.get("state", "unknown")), False, "reservation or invocation fence mismatch")
            if record.get("reconcile_required"):
                return self._reply(request, "rejected", False, "quarantined", True, "reboot reconciliation required")
            if record.get("state") == "running":
                runtime_identity = _dict(record.get("identity"))
                unit = str(runtime_identity.get("unit", ""))
                invocation = str(runtime_identity.get("invocation", ""))
                if not unit or not invocation:
                    return self._reply(request, "rejected", False, "quarantined", True, "running fence has no exact invocation")
                inspect_existing = True
            if record.get("state") not in {"starting", "quarantined"}:
                if not inspect_existing:
                    return self._reply(request, "rejected", False, str(record.get("state", "unknown")), False, "generation is closed")
            elif record.get("start_attempted"):
                if generation_key in self._inflight_starts:
                    return self._reply(request, "started", False, "quarantined", True, "start is already in progress")
                uncertain = bool(record.get("start_uncertain"))
                if not uncertain and record.get("state") != "starting":
                    return self._reply(request, "rejected", False, "quarantined", True, "start already failed; generation retained")
                unit = str(identity["unit"])
                invocation = str(identity["invocation"])
                inspect_needed = True
            elif not inspect_existing:
                conflict = self._identity_owner_conflict(key, identity)
                if conflict is not None:
                    return self._reply(request, "rejected", False, str(record.get("state", "starting")), False, conflict)
                record["identity"] = copy.deepcopy(identity)
                record["workload"] = copy.deepcopy(workload)
                record["state"] = "starting"
                record["start_attempted"] = True
                record["start_in_flight"] = True
                record["start_uncertain"] = True
                record["stop_requested"] = False
                self._save_locked()
                unit = str(identity["unit"])
                invocation = str(identity["invocation"])
                self._inflight_starts.add((key, int(identity["generation"])))
                self._start_operations[generation_key] = {
                    "identity": copy.deepcopy(identity),
                    "unit": unit,
                    "invocation": invocation,
                    "finished": False,
                    "timed_out": False,
                }
                inspect_needed = False

        if inspect_existing:
            result = self._bounded_call(self.systemd.inspect, unit, invocation)
            active, reason, cgroup, gpu_tenants = self._inspect_active_result(result, unit, invocation)
            if active:
                return self._reply(request, "started", True, "running", False, None, result=result, cgroup=cgroup, gpu_tenants=gpu_tenants)
            self._quarantine(key, reason or "duplicate start could not verify the existing invocation")
            return self._reply(request, "started", False, "quarantined", True, reason or "duplicate start could not verify the existing invocation", result=result, cgroup=cgroup, gpu_tenants=gpu_tenants)

        if inspect_needed:
            return self._recover_start(request, key, unit, invocation)

        result = self._bounded_call(
            self.systemd.start,
            unit,
            invocation,
            on_complete=lambda completed: self._start_operation_finished(generation_key, unit, invocation, completed),
        )
        if self._mark_start_timeout(generation_key, identity):
            return self._reply(
                request,
                "unknown",
                False,
                "quarantined",
                True,
                "systemd start operation timed out; completion remains fenced",
            )
        with self._lock:
            operation = self._start_operations.get(generation_key)
            if operation is not None and operation.get("finished"):
                self._start_operations.pop(generation_key, None)
                self._inflight_starts.discard(generation_key)
        if self._valid_systemd_success(result, unit, invocation):
            cleanup_snapshot: dict[str, object] | None = None
            keep_quarantined = False
            with self._lock:
                current = self._state["lanes"].get(key)
                if not isinstance(current, dict) or current.get("generation") != identity.get("generation") or current.get("identity") != identity:
                    return self._reply(request, "started", False, self._lane_state(key), True, "start fence changed before completion")
                current["start_in_flight"] = False
                current["start_result_ok"] = True
                current["start_uncertain"] = False
                if current.get("stop_requested") or current.get("state") == "stopping":
                    current["state"] = "stopping"
                    cleanup_snapshot = copy.deepcopy(current)
                elif current.get("state") == "starting":
                    current["state"] = "running"
                    now = self._read_clock()
                    current["last_heartbeat_mono"] = now[1]
                    current["last_heartbeat_utc"] = _utc_text(now[0])
                    current["last_heartbeat_boot_id"] = now[2]
                else:
                    keep_quarantined = True
                self._save_locked()
            if cleanup_snapshot is not None:
                cleanup = self._stop_record(key, cleanup_snapshot, reason="stop requested during start", internal=False)
                if cleanup.get("ok") is True:
                    return self._reply(request, "started", False, "free", False, "start completed after stop was requested", cgroup=[], gpu_tenants=[])
                return self._reply(request, "started", False, "quarantined", True, str(cleanup.get("error", "start cleanup is uncertain")))
            if keep_quarantined:
                return self._reply(request, "started", False, "quarantined", True, "start completed after the generation was closed", result=result)
            return self._reply(request, "started", True, "running", False, None, result=result)

        status = str(result.get("status", "unknown")) if isinstance(result, Mapping) else "unknown"
        if status in {"lost", "timeout", "unknown"}:
            with self._lock:
                current = self._state["lanes"].get(key)
                if isinstance(current, dict) and current.get("generation") == identity.get("generation"):
                    current["start_in_flight"] = False
                    current["start_uncertain"] = True
                    self._save_locked()
            return self._recover_start(request, key, unit, invocation)

        with self._lock:
            current = self._state["lanes"].get(key)
            if isinstance(current, dict) and current.get("generation") == identity.get("generation"):
                current["start_in_flight"] = False
                current["start_uncertain"] = False
                self._save_locked()
        self._quarantine(key, f"systemd start failed: {status}")
        return self._reply(request, "unknown", False, "quarantined", True, f"systemd start failed: {status}", result=result)

    def _mark_start_timeout(self, generation_key: tuple[str, int], identity: Mapping[str, object]) -> bool:
        """Retain the fence when the bounded wrapper outlives its caller."""

        with self._lock:
            operation = self._start_operations.get(generation_key)
            if operation is None or operation.get("finished"):
                return False
            operation["timed_out"] = True
            current = self._state.get("lanes", {}).get(generation_key[0])
            if isinstance(current, dict) and current.get("generation") == generation_key[1] and current.get("identity") == dict(identity):
                current["start_in_flight"] = True
                current["start_uncertain"] = True
                current["quarantine_reason"] = "systemd start operation timed out; completion remains fenced"
                if current.get("stop_requested") or current.get("state") == "stopping":
                    current["state"] = "stopping"
                else:
                    current["state"] = "quarantined"
                self._close_record_locked(current)
                self._save_locked()
            return True

    def _start_operation_finished(
        self,
        generation_key: tuple[str, int],
        unit: str,
        invocation: str,
        result: Mapping[str, object],
    ) -> None:
        cleanup_snapshot: dict[str, object] | None = None
        with self._lock:
            operation = self._start_operations.get(generation_key)
            if operation is None:
                return
            operation["finished"] = True
            operation["result"] = copy.deepcopy(dict(result))
            self._inflight_starts.discard(generation_key)
            if not operation.get("timed_out"):
                return
            self._start_operations.pop(generation_key, None)
            current = self._state.get("lanes", {}).get(generation_key[0])
            identity = _dict(operation.get("identity"))
            if not isinstance(current, dict) or current.get("generation") != generation_key[1] or current.get("identity") != identity:
                return
            current["start_in_flight"] = False
            if self._valid_systemd_success(result, unit, invocation):
                current["start_result_ok"] = True
                current["start_uncertain"] = False
                if current.get("stop_requested") or current.get("state") == "stopping":
                    current["state"] = "stopping"
                    cleanup_snapshot = copy.deepcopy(current)
                else:
                    current["state"] = "quarantined"
                    current["quarantine_reason"] = "start completed after timeout; exact cleanup is required"
            else:
                current["start_result_ok"] = False
                current["start_uncertain"] = True
                current["state"] = "quarantined"
                current["quarantine_reason"] = f"systemd start completed uncertainly: {result.get('status', 'unknown')}"
            self._save_locked()
        if cleanup_snapshot is not None:
            self._stop_record(generation_key[0], cleanup_snapshot, reason="stop requested during late start completion", internal=False)

    def _recover_start(self, request: Mapping[str, object], key: str, unit: str, invocation: str) -> dict[str, object]:
        result = self._bounded_call(self.systemd.inspect, unit, invocation)
        active, reason, cgroup, gpu_tenants = self._inspect_active_result(result, unit, invocation)
        if active:
            cleanup_snapshot: dict[str, object] | None = None
            with self._lock:
                current = self._state["lanes"].get(key)
                if isinstance(current, dict) and current.get("identity", {}).get("unit") == unit and current.get("identity", {}).get("invocation") == invocation:
                    current["start_in_flight"] = False
                    current["start_result_ok"] = True
                    current["start_uncertain"] = False
                    if current.get("stop_requested") or current.get("state") == "stopping":
                        current["state"] = "stopping"
                        cleanup_snapshot = copy.deepcopy(current)
                    else:
                        current["state"] = "running"
                        current["last_heartbeat_mono"] = self._read_clock()[1]
                    self._save_locked()
            if cleanup_snapshot is not None:
                cleanup = self._stop_record(key, cleanup_snapshot, reason="stop requested during start recovery", internal=False)
                if cleanup.get("ok") is True:
                    return self._reply(request, "started", False, "free", False, "recovered start was already stopped", cgroup=[], gpu_tenants=[])
                return self._reply(request, "started", False, "quarantined", True, str(cleanup.get("error", "recovered start cleanup is uncertain")))
            return self._reply(request, "started", True, "running", False, None, cgroup=cgroup, gpu_tenants=gpu_tenants)
        with self._lock:
            current = self._state["lanes"].get(key)
            if isinstance(current, dict) and current.get("identity", {}).get("unit") == unit and current.get("identity", {}).get("invocation") == invocation:
                current["start_in_flight"] = False
                self._save_locked()
        self._quarantine(key, reason or "lost start reply could not be reconciled")
        return self._reply(request, "unknown", False, "quarantined", True, reason or "lost start reply could not be reconciled", result=result)

    def _recover_pending_start_for_stop(self, request: Mapping[str, object], key: str, snapshot: dict[str, object]) -> dict[str, object]:
        identity = _dict(snapshot.get("identity"))
        unit = str(identity.get("unit", ""))
        invocation = str(identity.get("invocation", ""))
        result = self._bounded_call(self.systemd.inspect, unit, invocation)
        active, reason, cgroup, gpu_tenants = self._inspect_active_result(result, unit, invocation)
        if not active:
            with self._lock:
                current = self._state["lanes"].get(key)
                if isinstance(current, dict) and current.get("generation") == snapshot.get("generation"):
                    current["start_in_flight"] = False
                    self._save_locked()
            failure = reason or "pending start could not be reconciled before stop"
            self._quarantine(key, failure)
            return self._reply(request, "stopped", False, "quarantined", True, failure, result=result, cgroup=cgroup, gpu_tenants=gpu_tenants)
        with self._lock:
            current = self._state["lanes"].get(key)
            if not isinstance(current, dict) or current.get("generation") != snapshot.get("generation") or current.get("identity") != identity:
                return self._reply(request, "stopped", False, "quarantined", True, "stop fence changed during start recovery")
            current["start_in_flight"] = False
            current["start_result_ok"] = True
            current["start_uncertain"] = False
            current["state"] = "stopping"
            cleanup_snapshot = copy.deepcopy(current)
            self._save_locked()
        return self._stop_record(key, cleanup_snapshot, reason="stop after pending start recovery", internal=False)

    def _beat(self, request: Mapping[str, object]) -> dict[str, object]:
        identity = _dict(request.get("identity"))
        key = _lane_key(_dict(identity.get("lane")))
        with self._lock:
            record = self._state["lanes"].get(key)
            if not isinstance(record, dict) or record.get("state") not in {"starting", "running"} or not self._full_identity_matches(record, identity):
                return self._reply(request, "rejected", False, self._lane_state(key), False, "heartbeat identity mismatch")
            if record.get("reconcile_required") or identity.get("deadline", {}).get("boot_id") != self._read_clock()[2]:
                return self._reply(request, "rejected", False, "quarantined", True, "heartbeat requires same-boot reconciliation")
            now = self._read_clock()
            record["last_heartbeat_mono"] = now[1]
            record["last_heartbeat_utc"] = _utc_text(now[0])
            record["last_heartbeat_boot_id"] = now[2]
            if self._deadline_reason(record, now[1]) is None:
                record["grace_started_mono"] = None
                record["grace_reason"] = None
            self._save_locked()
            state = str(record.get("state", "running"))
        return self._reply(request, "beat", True, state, False, None)

    def _stop(self, request: Mapping[str, object]) -> dict[str, object]:
        identity = _dict(request.get("identity"))
        key = _lane_key(_dict(identity.get("lane")))
        authority = _dict(request.get("stop_authority"))
        pending_restart_snapshot: dict[str, object] | None = None
        with self._lock:
            record = self._state["lanes"].get(key)
            if not isinstance(record, dict) or not self._full_identity_matches(record, identity):
                return self._reply(request, "rejected", False, self._lane_state(key), False, "stop identity mismatch")
            policy = _dict(record.get("policy"))
            if bool(policy.get("protected")):
                approval_id = authority.get("approval_id")
                if authority.get("mode") != "approved-forced-preemption" or not isinstance(approval_id, str) or not self._approval_valid(approval_id, request, record):
                    record["state"] = "quarantined"
                    record["quarantine_reason"] = "protected stop lacks trusted forced-preemption approval"
                    record["closed_generation"] = max(int(record.get("closed_generation", 0)), int(record["generation"]))
                    token = record.get("identity", {}).get("token") if isinstance(record.get("identity"), Mapping) else None
                    if isinstance(token, str) and token not in record.setdefault("closed_tokens", []):
                        record["closed_tokens"].append(token)
                    pair = [record.get("identity", {}).get("unit"), record.get("identity", {}).get("invocation")] if isinstance(record.get("identity"), Mapping) else [None, None]
                    if pair[0] is not None and pair not in record.setdefault("closed_invocations", []):
                        record["closed_invocations"].append(pair)
                    self._save_locked()
                    return self._reply(request, "rejected", False, "quarantined", False, "protected stop lacks trusted forced-preemption approval")
            elif authority.get("mode") != "controller-match" or authority.get("approval_id") is not None:
                return self._reply(request, "rejected", False, str(record.get("state", "unknown")), False, "invalid stop authority")
            generation_key = (key, int(record.get("generation", 0)))
            if record.get("start_in_flight") or generation_key in self._inflight_starts:
                record["state"] = "stopping"
                record["stop_requested"] = True
                record["closed_generation"] = max(int(record.get("closed_generation", 0)), int(record.get("generation", 0)))
                self._save_locked()
                if generation_key in self._inflight_starts:
                    return self._reply(request, "stopped", False, "quarantined", True, "stop queued until start outcome is known")
                pending_restart_snapshot = copy.deepcopy(record)
            snapshot = copy.deepcopy(record) if pending_restart_snapshot is None else None
        if pending_restart_snapshot is not None:
            return self._recover_pending_start_for_stop(request, key, pending_restart_snapshot)
        assert snapshot is not None
        return self._stop_record(key, snapshot, reason="controller stop", internal=False)

    def _inspect(self, request: Mapping[str, object]) -> dict[str, object]:
        identity = _dict(request.get("identity"))
        key = _lane_key(_dict(identity.get("lane")))
        with self._lock:
            record = self._state["lanes"].get(key)
            if not isinstance(record, dict) or record.get("state") == "free":
                return self._reply(request, "inspected", True, "free", False, None)
            if int(record.get("generation", 0)) != int(identity.get("generation", -1)) or record.get("identity", {}).get("instance") != identity.get("instance"):
                return self._reply(request, "rejected", False, str(record.get("state", "unknown")), False, "inspect fence mismatch")
            runtime_identity = _dict(record.get("identity"))
            if not runtime_identity.get("unit") or not runtime_identity.get("invocation"):
                return self._reply(request, "inspected", True, str(record.get("state", "starting")), False, None)
            unit = str(runtime_identity["unit"])
            invocation = str(runtime_identity["invocation"])
        result = self._bounded_call(self.systemd.inspect, unit, invocation)
        active, reason, cgroup, gpu_tenants = self._inspect_active_result(result, unit, invocation)
        if not active and reason:
            self._quarantine(key, reason)
            return self._reply(request, "unknown", False, "quarantined", True, reason, result=result)
        state = "running" if active else "quarantined"
        return self._reply(request, "inspected", True, state, False, None, cgroup=cgroup, gpu_tenants=gpu_tenants)

    # ------------------------------------------------------------------
    # Side-effect and state helpers

    def _stop_record(self, key: str, snapshot: dict[str, object], *, reason: str, internal: bool) -> dict[str, object]:
        identity = _dict(snapshot.get("identity"))
        unit = str(identity.get("unit", ""))
        invocation = str(identity.get("invocation", ""))
        if not unit or not invocation:
            self._quarantine(key, "cannot stop without exact unit and invocation")
            return self._internal_result(key, "quarantined", "cannot stop without exact unit and invocation")

        generation_key = (key, int(snapshot.get("generation", 0)))
        with self._lock:
            current = self._state["lanes"].get(key)
            if not isinstance(current, dict) or current.get("generation") != snapshot.get("generation") or current.get("identity") != identity:
                return self._internal_result(key, "quarantined", "stop fence changed")
            if current.get("state") == "free":
                return self._internal_result(key, "free", "already released")
            if current.get("start_in_flight") or generation_key in self._inflight_starts:
                current["state"] = "stopping"
                current["stop_requested"] = True
                current["closed_generation"] = max(int(current.get("closed_generation", 0)), int(current.get("generation", 0)))
                self._save_locked()
                return self._internal_result(key, "quarantined", "stop queued until start outcome is known")
            if generation_key in self._inflight_stops:
                return self._internal_result(key, "quarantined", "stop is already in progress")
            if current.get("stop_attempted"):
                stop_result_ok = bool(current.get("stop_result_ok"))
                stop_result: Mapping[str, object] = {"ok": stop_result_ok, "status": "recovery"}
            else:
                current["state"] = "stopping"
                current["closed_generation"] = max(int(current.get("closed_generation", 0)), int(current.get("generation", 0)))
                current["stop_attempted"] = True
                current["stop_result_ok"] = False
                current["quarantine_reason"] = None
                self._save_locked()
                self._inflight_stops.add(generation_key)
                stop_result = {}

        if not stop_result:
            stop_result = self._bounded_call(self.systemd.stop, unit, invocation)
            self._inflight_stops.discard(generation_key)
            stop_ok = self._valid_systemd_success(stop_result, unit, invocation)
            with self._lock:
                current = self._state["lanes"].get(key)
                if isinstance(current, dict) and current.get("generation") == snapshot.get("generation"):
                    current["stop_result_ok"] = bool(stop_ok)
                    self._save_locked()
        else:
            stop_ok = bool(stop_result.get("ok"))

        inspect_result = self._bounded_call(self.systemd.inspect, unit, invocation)
        gpu_result = self._bounded_call(self.gpu_probe.inspect, str(_dict(snapshot.get("lane")).get("host_id", "")))
        inspect_ok, inspect_reason, cgroup, systemd_gpu = self._cleanup_systemd_observation(inspect_result, unit, invocation)
        gpu_ok, gpu_reason, gpu_tenants = self._cleanup_gpu_observation(gpu_result)
        if stop_ok and inspect_ok and gpu_ok:
            with self._lock:
                current = self._state["lanes"].get(key)
                if isinstance(current, dict) and current.get("generation") == snapshot.get("generation") and current.get("identity") == identity:
                    token = identity.get("token")
                    if isinstance(token, str) and token not in current.setdefault("closed_tokens", []):
                        current["closed_tokens"].append(token)
                    pair = [identity.get("unit"), identity.get("invocation")]
                    if pair not in current.setdefault("closed_invocations", []):
                        current["closed_invocations"].append(pair)
                    current["state"] = "free"
                    current["closed_generation"] = max(int(current.get("closed_generation", 0)), int(current.get("generation", 0)))
                    current["generation"] = 0
                    current["identity"] = None
                    current["reservation_identity"] = None
                    current["workload"] = None
                    current["quarantine_reason"] = None
                    current["reconcile_required"] = False
                    self._save_locked()
            return self._reply_for_identity(identity, "stop", "stopped", True, "free", False, None, cgroup=[], gpu_tenants=[])

        reason_parts = []
        if not stop_ok:
            reason_parts.append("systemd stop reply was not a verified success")
        if not inspect_ok and inspect_reason:
            reason_parts.append(inspect_reason)
        if not gpu_ok and gpu_reason:
            reason_parts.append(gpu_reason)
        failure = "; ".join(reason_parts) or "cleanup evidence was incomplete"
        self._quarantine(key, failure)
        return self._reply_for_identity(identity, "stop", "stopped", False, "quarantined", True, failure, cgroup=cgroup or _list_strings(systemd_gpu), gpu_tenants=gpu_tenants)

    def _close_record_locked(self, record: dict[str, object]) -> None:
        if isinstance(record.get("generation"), int):
            record["closed_generation"] = max(int(record.get("closed_generation", 0)), int(record["generation"]))
        identity = record.get("identity")
        if isinstance(identity, Mapping):
            token = identity.get("token")
            if isinstance(token, str) and token not in record.setdefault("closed_tokens", []):
                record["closed_tokens"].append(token)
            pair = [identity.get("unit"), identity.get("invocation")]
            if pair[0] is not None and pair not in record.setdefault("closed_invocations", []):
                record["closed_invocations"].append(pair)

    def _quarantine(self, key: str, reason: str) -> None:
        with self._lock:
            record = self._state.get("lanes", {}).get(key)
            if isinstance(record, dict):
                record["state"] = "quarantined"
                record["quarantine_reason"] = reason
                self._close_record_locked(record)
                self._save_locked()

    def _rebase_after_reboot_locked(self, record: dict[str, object]) -> str | None:
        """Move a still-valid monotonic deadline onto the current boot.

        A deadline from the previous boot is never compared with the new
        monotonic clock.  Its UTC anchor is used only to calculate remaining
        time after successful occupancy reconciliation; an already-expired or
        malformed deadline stays quarantined instead of authorising a kill.
        """

        now = self._read_clock()
        record["last_heartbeat_mono"] = now[1]
        record["last_heartbeat_utc"] = _utc_text(now[0])
        record["last_heartbeat_boot_id"] = now[2]
        record["grace_started_mono"] = None
        record["grace_reason"] = None
        identity = record.get("identity")
        if not isinstance(identity, Mapping) or not isinstance(identity.get("deadline"), Mapping):
            return "reboot reconciliation has no valid deadline"
        deadline = dict(identity["deadline"])
        try:
            anchor_utc = _parse_utc(str(deadline["utc_anchor"]))
            deadline_s = float(deadline["deadline_s"])
            anchor_mono = float(deadline["monotonic_anchor_s"])
            remaining = (anchor_utc + timedelta(seconds=deadline_s - anchor_mono) - now[0]).total_seconds()
        except (KeyError, TypeError, ValueError, OverflowError):
            return "reboot reconciliation has an invalid deadline"
        if remaining <= 0:
            return "deadline expired before reboot reconciliation"
        deadline["boot_id"] = now[2]
        deadline["utc_anchor"] = _utc_text(now[0])
        deadline["monotonic_anchor_s"] = now[1]
        deadline["deadline_s"] = now[1] + remaining
        identity_copy = copy.deepcopy(dict(identity))
        identity_copy["deadline"] = deadline
        record["identity"] = identity_copy
        reservation = record.get("reservation_identity")
        if isinstance(reservation, Mapping):
            reservation_copy = copy.deepcopy(dict(reservation))
            reservation_copy["deadline"] = copy.deepcopy(deadline)
            record["reservation_identity"] = reservation_copy
        return None

    def _initialise_clock_and_reboot(self) -> None:
        now = self._read_clock()
        persisted_boot = self._state.get("boot_id")
        if persisted_boot is not None and persisted_boot != now[2]:
            self._state["previous_boot_id"] = persisted_boot
            self._state["reboot_pending"] = True
            for record in self._state.get("lanes", {}).values():
                if isinstance(record, dict) and record.get("state") in _ACTIVE_STATES:
                    record["reconcile_required"] = True
        self._state["boot_id"] = now[2]
        if not isinstance(self._state.get("clock"), dict):
            self._state["clock"] = {"utc": _utc_text(now[0]), "monotonic_s": now[1], "boot_id": now[2], "frozen": False, "skew_s": 0.0}
        self._save_locked()

    def _sample_clock_locked(self) -> None:
        now = self._read_clock()
        previous = self._state.get("clock")
        frozen = bool(previous.get("frozen")) if isinstance(previous, dict) else False
        skew = 0.0
        if isinstance(previous, dict) and previous.get("boot_id") == now[2]:
            try:
                old_utc = _parse_utc(str(previous["utc"]))
                old_mono = float(previous["monotonic_s"])
                skew = (now[0] - old_utc).total_seconds() - (now[1] - old_mono)
                if abs(skew) > self._clock_skew_s:
                    frozen = True
            except (KeyError, TypeError, ValueError):
                frozen = True
        elif isinstance(previous, dict) and previous.get("boot_id") != now[2]:
            self._state["reboot_pending"] = True
            for record in self._state.get("lanes", {}).values():
                if isinstance(record, dict) and record.get("state") in _ACTIVE_STATES:
                    record["reconcile_required"] = True
        self._state["clock"] = {"utc": _utc_text(now[0]), "monotonic_s": now[1], "boot_id": now[2], "frozen": frozen, "skew_s": skew}
        self._state["boot_id"] = now[2]
        self._save_locked()

    def _read_clock(self) -> tuple[datetime, float, str]:
        utc_now = self.clock.utc()
        if utc_now.tzinfo is None:
            utc_now = utc_now.replace(tzinfo=timezone.utc)
        utc_now = utc_now.astimezone(timezone.utc)
        mono = float(self.clock.monotonic())
        boot = str(self.clock.boot_id())
        return utc_now, mono, boot

    def _save_locked(self) -> None:
        self._store.save(self._state)

    def _bounded_call(
        self,
        function: Callable[..., object],
        *args: object,
        on_complete: Callable[[Mapping[str, object]], None] | None = None,
    ) -> Mapping[str, object]:
        """Call an injected host operation with a finite wait.

        The injected interface has no timeout parameter.  A daemon worker keeps
        a wedged adapter from holding the executor lock or blocking the host
        deadline loop forever; once the bound expires the result is uncertain
        and the lane remains fenced.
        """

        result: list[object] = []
        failure: list[BaseException] = []
        completed: dict[str, object] = {
            "ok": False,
            "status": "unknown",
            "unit": args[0] if args else None,
            "invocation": args[1] if len(args) > 1 else None,
        }

        def invoke() -> None:
            try:
                observed = function(*args)
                if isinstance(observed, Mapping):
                    completed.update(dict(observed))
                    result.append(dict(observed))
                else:
                    failure.append(TypeError("host operation returned a non-mapping result"))
            except BaseException as exc:  # pragma: no cover - adapter-specific
                failure.append(exc)
            finally:
                if on_complete is not None:
                    on_complete(completed)

        worker = threading.Thread(target=invoke, name="flightctl-host-call", daemon=True)
        worker.start()
        worker.join(self._operation_timeout_s)
        if worker.is_alive():
            return {"ok": False, "status": "timeout", "unit": args[0] if args else None, "invocation": args[1] if len(args) > 1 else None}
        if failure or not result or not isinstance(result[0], Mapping):
            return {"ok": False, "status": "unknown", "unit": args[0] if args else None, "invocation": args[1] if len(args) > 1 else None}
        return dict(result[0])

    def _clock_frozen_locked(self) -> bool:
        clock = self._state.get("clock")
        return bool(isinstance(clock, dict) and clock.get("frozen"))

    def _authenticated(self, context: object) -> bool:
        if context is AUTH_UNSET or context is None:
            return False
        if self._controller_authorizer is not None:
            try:
                return bool(self._controller_authorizer(context))
            except Exception:
                return False
        if self._trusted_controller is None:
            return False
        if context is self._trusted_controller:
            return True
        if isinstance(context, (str, int, bytes)) and isinstance(self._trusted_controller, type(context)):
            return context == self._trusted_controller
        return False

    def _approval_valid(self, approval_id: str, request: Mapping[str, object], record: Mapping[str, object]) -> bool:
        if self._approval_checker is not None:
            try:
                return bool(self._approval_checker(approval_id, request, record))
            except TypeError:
                try:
                    return bool(self._approval_checker(approval_id, record))
                except Exception:
                    return False
            except Exception:
                return False
        approved = self._approved_forced_preemptions
        if isinstance(approved, Mapping):
            return approval_id in approved and bool(approved[approval_id])
        return approved is not None and approval_id in set(approved)

    def _validate_request_shape(self, request: Mapping[str, object]) -> str:
        if not isinstance(request, Mapping):
            raise ValueError("request must be a mapping")
        if request.get("schema_version") != 1:
            raise ValueError("unsupported executor schema version")
        kind = request.get("kind")
        if kind not in {"reserve", "start", "beat", "stop", "inspect"}:
            raise ValueError("unsupported executor operation")
        if not isinstance(request.get("controller_request_id"), str) or not request["controller_request_id"]:
            raise ValueError("controller_request_id is required")
        if not isinstance(request.get("execution_policy"), Mapping):
            raise ValueError("execution_policy is required")
        kind = request.get("kind")
        allowed_request_keys = {
            "reserve": {"schema_version", "kind", "controller_request_id", "execution_policy", "identity"},
            "start": {"schema_version", "kind", "controller_request_id", "execution_policy", "reservation_acknowledged", "workload", "identity"},
            "beat": {"schema_version", "kind", "controller_request_id", "execution_policy", "identity"},
            "stop": {"schema_version", "kind", "controller_request_id", "execution_policy", "stop_authority", "identity"},
            "inspect": {"schema_version", "kind", "controller_request_id", "execution_policy", "identity"},
        }
        if isinstance(kind, str) and set(request) - allowed_request_keys.get(kind, set()):
            raise ValueError("request contains fields outside the frozen executor contract")
        identity = request.get("identity")
        if not isinstance(identity, Mapping) or not isinstance(identity.get("lane"), Mapping):
            raise ValueError("identity and lane are required")
        if not isinstance(identity.get("generation"), int) or identity["generation"] < 1:
            raise ValueError("positive generation is required")
        lane = identity["lane"]
        if any(not isinstance(lane.get(name), str) or not lane[name] for name in ("site_id", "host_id", "lane_id")):
            raise ValueError("complete lane identity is required")
        policy = request["execution_policy"]
        if policy.get("class") not in _EXECUTION_CLASSES or not isinstance(policy.get("protected"), bool) or not isinstance(policy.get("preemptible"), bool):
            raise ValueError("invalid execution policy")
        if policy.get("protected") and policy.get("preemptible"):
            raise ValueError("protected work cannot be preemptible")
        if set(policy) != {"class", "protected", "preemptible", "grace_s", "max_end", "deadline_kind"} or not isinstance(policy.get("grace_s"), int) or policy["grace_s"] < 0 or not isinstance(policy.get("max_end"), str) or policy.get("deadline_kind") not in {"heartbeat", "lease", "grace", "max-end"}:
            raise ValueError("invalid execution policy shape")
        if set(identity) != {"lane", "generation", "token", "instance", "unit", "invocation", "deadline"}:
            raise ValueError("identity contains fields outside the frozen executor contract")
        deadline = identity.get("deadline")
        if isinstance(deadline, Mapping):
            if set(deadline) != {"kind", "owner_class", "boot_id", "deadline_s", "utc_anchor", "monotonic_anchor_s"} or deadline.get("kind") not in {"heartbeat", "lease", "grace", "max-end"} or deadline.get("owner_class") not in _EXECUTION_CLASSES or not isinstance(deadline.get("boot_id"), str) or not isinstance(deadline.get("deadline_s"), (int, float)) or deadline["deadline_s"] < 0 or not isinstance(deadline.get("utc_anchor"), str) or not isinstance(deadline.get("monotonic_anchor_s"), (int, float)) or deadline["monotonic_anchor_s"] < 0:
                raise ValueError("invalid deadline shape")
        if kind == "reserve":
            if not isinstance(identity.get("token"), str) or len(identity["token"]) < 16 or not isinstance(identity.get("instance"), str) or not identity["instance"] or identity.get("unit") is not None or identity.get("invocation") is not None or not isinstance(identity.get("deadline"), Mapping):
                raise ValueError("reserve identity is incomplete")
        elif kind == "inspect":
            if identity.get("token") is not None or identity.get("unit") is not None or identity.get("invocation") is not None or identity.get("deadline") is not None or not isinstance(identity.get("instance"), str):
                raise ValueError("inspect identity is incomplete")
        else:
            if not all(isinstance(identity.get(field), str) and identity[field] for field in ("token", "instance", "unit", "invocation")) or len(str(identity.get("token", ""))) < 16 or not isinstance(identity.get("deadline"), Mapping):
                raise ValueError("running identity is incomplete")
        if kind == "start" and request.get("reservation_acknowledged") is not True:
            raise ValueError("start requires reservation acknowledgement")
        if kind == "start" and not isinstance(request.get("workload"), Mapping):
            raise ValueError("start workload is required")
        if kind == "start" and set(request["workload"]) != {"workload_id", "class", "image_digest", "parameters_hash", "manifest_hash"}:
            raise ValueError("workload contains fields outside the frozen executor contract")
        if kind == "stop" and not isinstance(request.get("stop_authority"), Mapping):
            raise ValueError("stop authority is required")
        if kind == "stop" and set(request["stop_authority"]) != {"mode", "approval_id"}:
            raise ValueError("stop authority contains fields outside the frozen executor contract")
        return str(kind)

    def _start_identity_allowed(self, record: Mapping[str, object], identity: Mapping[str, object], policy: Mapping[str, object], workload: Mapping[str, object]) -> bool:
        recovering_uncertain_start = bool(record.get("state") == "quarantined" and record.get("start_attempted") and record.get("start_uncertain") and record.get("identity") == dict(identity))
        if record.get("state") == "free" or (int(record.get("closed_generation", 0)) >= int(identity.get("generation", 0)) and not recovering_uncertain_start):
            return False
        if identity.get("token") in record.get("closed_tokens", []) and not recovering_uncertain_start:
            return False
        if [identity.get("unit"), identity.get("invocation")] in record.get("closed_invocations", []) and not recovering_uncertain_start:
            return False
        reservation = _dict(record.get("reservation_identity"))
        for field in ("lane", "generation", "token", "instance", "deadline"):
            if reservation.get(field) != identity.get(field):
                return False
        if record.get("policy") != dict(policy):
            return False
        if workload.get("class") != policy.get("class"):
            return False
        if record.get("identity") is not None:
            saved = _dict(record.get("identity"))
            if saved.get("unit") is not None and saved != dict(identity):
                return False
        return True

    def _identity_owner_conflict(self, key: str, identity: Mapping[str, object]) -> str | None:
        unit = identity.get("unit")
        invocation = identity.get("invocation")
        for other_key, other in self._state.get("lanes", {}).items():
            if other_key == key or not isinstance(other, Mapping) or other.get("state") not in _ACTIVE_STATES:
                continue
            saved = other.get("identity")
            if not isinstance(saved, Mapping):
                continue
            if saved.get("unit") == unit:
                return "systemd unit is already fenced by another generation"
            if saved.get("invocation") == invocation:
                return "systemd invocation is already fenced by another generation"
        return None

    def _reservation_token_owner(self, key: str, token: str) -> str | None:
        for other_key, other in self._state.get("lanes", {}).items():
            if other_key == key or not isinstance(other, Mapping) or other.get("state") not in _ACTIVE_STATES:
                continue
            reservation = other.get("reservation_identity")
            if isinstance(reservation, Mapping) and reservation.get("token") == token:
                return other_key
        return None

    def _full_identity_matches(self, record: Mapping[str, object], identity: Mapping[str, object]) -> bool:
        saved = record.get("identity")
        return isinstance(saved, Mapping) and dict(saved) == dict(identity) and int(record.get("closed_generation", 0)) <= int(identity.get("generation", 0))

    def _same_reservation(self, record: Mapping[str, object], identity: Mapping[str, object], policy: Mapping[str, object]) -> bool:
        return record.get("reservation_identity") == dict(identity) and record.get("policy") == dict(policy)

    def _deadline_reason(self, record: Mapping[str, object], now_mono: float) -> str | None:
        policy = _dict(record.get("policy"))
        identity = _dict(record.get("identity"))
        deadline = identity.get("deadline")
        if isinstance(deadline, Mapping):
            if deadline.get("boot_id") != self._state.get("boot_id"):
                return None
            try:
                if now_mono >= float(deadline["deadline_s"]):
                    return str(deadline.get("kind", "deadline"))
            except (KeyError, TypeError, ValueError):
                return "invalid deadline"
        last_beat = record.get("last_heartbeat_mono")
        if isinstance(last_beat, (int, float)):
            threshold = self.protected_stale_s if bool(policy.get("protected")) else self.preemptible_stale_s
            if now_mono - float(last_beat) >= threshold:
                return "heartbeat-stale"
        return None

    def _grace_seconds(self, record: Mapping[str, object]) -> float:
        policy = _dict(record.get("policy"))
        explicit = policy.get("grace_s")
        if isinstance(explicit, (int, float)) and explicit > 0:
            return float(explicit)
        owner_class = policy.get("class")
        if owner_class in {"service", "resident"}:
            return self.service_grace_s
        if owner_class == "standby":
            return self.standby_grace_s
        return 0.0

    def _lane_state(self, key: str) -> str:
        record = self._state.get("lanes", {}).get(key)
        return str(record.get("state", "free")) if isinstance(record, dict) else "free"

    def _internal_result(self, key: str, state: str, error: str) -> dict[str, object]:
        record = self.state_snapshot(key)
        identity = record.get("identity") if isinstance(record, dict) else None
        if not isinstance(identity, Mapping):
            return {"lane": key, "state": state, "error": error}
        return self._reply_for_identity(identity, "stop", "stopped", state == "free", state, state != "free", None if state == "free" else error)

    def _early_rejection(self, request: object, error: str, *, uncertain: bool = False) -> dict[str, object]:
        if isinstance(request, Mapping):
            kind = str(request.get("kind", "inspect"))
            identity = request.get("identity")
            if isinstance(identity, Mapping) and isinstance(identity.get("lane"), Mapping) and isinstance(identity.get("generation"), int) and isinstance(identity.get("instance"), str):
                echo = _echo_identity(identity)
                return self._reply_for_identity(echo, kind, "unknown" if uncertain else "rejected", False, "unknown" if uncertain else "free", uncertain, error)
        kind = str(request.get("kind", "inspect")) if isinstance(request, Mapping) else "inspect"
        return {"schema_version": 1, "kind": kind, "echoed_identity": {"lane": {"site_id": "invalid", "host_id": "invalid", "lane_id": "invalid"}, "generation": 1, "token": None, "instance": "invalid", "unit": None, "invocation": None, "deadline": None}, "acknowledgement": _reply_acknowledgement(kind, "unknown" if uncertain else "rejected", False), "ok": False, "observed_state": "unknown", "uncertain": uncertain, "cgroup_occupants": [], "gpu_tenants": [], "error": error}

    def _reply(self, request: Mapping[str, object], acknowledgement: str, ok: bool, state: str, uncertain: bool, error: str | None, *, result: Mapping[str, object] | None = None, cgroup: list[str] | None = None, gpu_tenants: list[str] | None = None) -> dict[str, object]:
        identity = _echo_identity(_dict(request.get("identity")))
        return self._reply_for_identity(identity, str(request.get("kind", "inspect")), acknowledgement, ok, state, uncertain, error, result=result, cgroup=cgroup, gpu_tenants=gpu_tenants)

    def _reply_for_identity(self, identity: Mapping[str, object], kind: str, acknowledgement: str, ok: bool, state: str, uncertain: bool, error: str | None, *, result: Mapping[str, object] | None = None, cgroup: list[str] | None = None, gpu_tenants: list[str] | None = None) -> dict[str, object]:
        source = result if isinstance(result, Mapping) else {}
        if cgroup is None:
            cgroup = _list_strings(source.get("cgroup_occupants", source.get("occupants", [])))
        if gpu_tenants is None:
            gpu_tenants = _list_strings(source.get("gpu_tenants", source.get("gpu_occupants", [])))
        if uncertain:
            ok = False
            state = "quarantined" if state not in {"unknown", "quarantined"} else state
            if not error:
                error = "uncertain executor result"
        acknowledgement = _reply_acknowledgement(kind, acknowledgement, bool(ok))
        if not ok and not error:
            error = "executor operation rejected"
        return {"schema_version": 1, "kind": kind, "echoed_identity": copy.deepcopy(dict(identity)), "acknowledgement": acknowledgement, "ok": bool(ok), "observed_state": state if state in {"unknown", "free", "starting", "running", "stopping", "quarantined"} else "unknown", "uncertain": bool(uncertain), "cgroup_occupants": sorted(set(cgroup)), "gpu_tenants": sorted(set(gpu_tenants)), "error": error if not ok or uncertain else None}

    def _valid_systemd_success(self, result: Mapping[str, object], unit: str, invocation: str) -> bool:
        if not isinstance(result, Mapping) or result.get("ok") is not True or result.get("unit") != unit or result.get("invocation") != invocation:
            return False
        if result.get("status") not in _KNOWN_SUCCESS_STATUSES:
            return False
        for key in ("cgroup_occupants", "occupants", "gpu_occupants", "gpu_tenants"):
            if key in result and _strict_string_list(result[key]) is None:
                return False
        return True

    def _inspect_active_result(self, result: Mapping[str, object], unit: str, invocation: str) -> tuple[bool, str | None, list[str], list[str]]:
        if not isinstance(result, Mapping) or result.get("unit") != unit or result.get("invocation") != invocation:
            return False, "contradictory systemd identity", [], []
        status = result.get("status")
        if result.get("ok") is not True or status not in _KNOWN_SUCCESS_STATUSES:
            return False, f"systemd inspection is {status or 'unknown'}", _list_strings(result.get("cgroup_occupants", result.get("occupants", []))), _list_strings(result.get("gpu_tenants", result.get("gpu_occupants", [])))
        for key in ("cgroup_occupants", "occupants", "gpu_occupants", "gpu_tenants"):
            if key in result and _strict_string_list(result[key]) is None:
                return False, f"systemd inspection has malformed {key}", _list_strings(result.get("cgroup_occupants", result.get("occupants", []))), _list_strings(result.get("gpu_tenants", result.get("gpu_occupants", [])))
        cgroup_value = result.get("cgroup_occupants", result.get("occupants", _MISSING))
        if _strict_string_list(cgroup_value) is None:
            return False, "systemd inspection occupancy is missing or malformed", [], _list_strings(result.get("gpu_tenants", result.get("gpu_occupants", [])))
        cgroup = _strict_string_list(cgroup_value) or []
        gpu = _list_strings(result.get("gpu_tenants", result.get("gpu_occupants", [])))
        if "complete" in result and result.get("complete") is not True:
            return False, "systemd inspection is incomplete", cgroup, gpu
        if "active" in result and not isinstance(result.get("active"), bool):
            return False, "systemd inspection has malformed active state", cgroup, gpu
        state = result.get("state")
        if state is not None and state not in {"free", "inactive", "absent", "dead", "running", "starting", "stopping", "active"}:
            return False, "systemd inspection has an unknown state", cgroup, gpu
        if state in {"free", "inactive", "absent", "dead"} or result.get("active") is False:
            return False, "systemd reports the invocation absent", cgroup, gpu
        return True, None, cgroup, gpu

    def _cleanup_systemd_observation(self, result: Mapping[str, object], unit: str, invocation: str) -> tuple[bool, str | None, list[str], list[str]]:
        if not isinstance(result, Mapping) or result.get("unit") != unit or result.get("invocation") != invocation:
            return False, "contradictory cleanup identity", [], []
        cgroup = _occupancy_fields(result, ("cgroup_occupants", "occupants"))
        gpu = _occupancy_fields(result, ("gpu_occupants", "gpu_tenants"), required=False)
        status = result.get("status")
        if result.get("ok") is not True or status not in _KNOWN_SUCCESS_STATUSES:
            return False, f"systemd cleanup inspection is {status or 'unknown'}", cgroup or [], gpu or []
        if cgroup is None:
            return False, "cgroup cleanup occupancy is missing or malformed", [], gpu or []
        if gpu is None:
            return False, "systemd cleanup GPU occupancy is malformed", cgroup, []
        if cgroup:
            return False, "cgroup cleanup is not independently empty", cgroup, gpu
        if gpu:
            return False, "systemd reports unexplained GPU occupants", cgroup, gpu
        if "complete" in result and result.get("complete") is not True:
            return False, "systemd cleanup inspection is incomplete", cgroup, gpu
        if "active" in result and not isinstance(result.get("active"), bool):
            return False, "systemd cleanup inspection has malformed active state", cgroup, gpu
        state = result.get("state")
        if state is not None and state not in {"free", "inactive", "absent", "dead", "running", "starting", "stopping", "active"}:
            return False, "systemd cleanup inspection has an unknown state", cgroup, gpu
        if state in {"running", "starting", "stopping", "active"} or result.get("active") is True:
            return False, "systemd still reports the invocation active", cgroup, gpu
        return True, None, cgroup, gpu

    def _cleanup_gpu_observation(self, result: Mapping[str, object]) -> tuple[bool, str | None, list[str]]:
        if not isinstance(result, Mapping):
            return False, "GPU cleanup output is missing", []
        status = result.get("status")
        if result.get("ok") is not True or status not in _KNOWN_SUCCESS_STATUSES or result.get("unload_ok") is False:
            return False, f"GPU cleanup is {status or 'unknown'}", _list_strings(result.get("gpu_tenants", result.get("tenants", [])))
        for nested_key in ("unload", "probe"):
            if nested_key not in result:
                continue
            nested = result[nested_key]
            if not isinstance(nested, Mapping) or nested.get("ok") is not True or nested.get("status") not in _KNOWN_SUCCESS_STATUSES:
                return False, "GPU cleanup operation failed", _list_strings(result.get("gpu_tenants", result.get("tenants", [])))
        tenants = _occupancy_fields(result, ("gpu_tenants", "tenants", "active_tenants", "occupants"))
        if tenants is None:
            return False, "GPU cleanup output is missing or partial", []
        if tenants:
            return False, "GPU has unexplained tenants", tenants
        if result.get("complete") is False or result.get("certainty") == "unknown":
            return False, "GPU cleanup output is partial", tenants
        if "complete" in result and result.get("complete") is not True:
            return False, "GPU cleanup output is partial", tenants
        if "certainty" in result and result.get("certainty") not in {"known", "complete"}:
            return False, "GPU cleanup certainty is unknown", tenants
        return True, None, tenants

    def _empty_occupancy(self, result: Mapping[str, object]) -> bool:
        cgroup = result.get("cgroup_occupants", result.get("occupants", [])) if isinstance(result, Mapping) else []
        values = _strict_string_list(cgroup)
        return values is not None and not values

    def _active_gpu_observation(self, result: Mapping[str, object]) -> tuple[bool, str | None, list[str]]:
        if not isinstance(result, Mapping):
            return False, "GPU reconcile output is missing", []
        tenants = _occupancy_fields(result, ("gpu_tenants", "tenants", "active_tenants", "occupants"))
        if tenants is None:
            return False, "GPU reconcile output is missing or partial", []
        if result.get("ok") is not True or result.get("status") not in _KNOWN_SUCCESS_STATUSES:
            return False, "GPU reconcile output is uncertain", tenants
        if result.get("complete") is False or ("complete" in result and result.get("complete") is not True):
            return False, "GPU reconcile output is incomplete", tenants
        if "certainty" in result and result.get("certainty") not in {"known", "complete"}:
            return False, "GPU reconcile certainty is unknown", tenants
        return True, None, tenants


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _positive_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("positive generation is required")
    return value


def _lane_key(lane: Mapping[str, object] | str) -> str:
    if isinstance(lane, str):
        return lane
    return "/".join(str(lane.get(part, "")) for part in ("site_id", "host_id", "lane_id"))


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _list_strings(value: object) -> list[str]:
    return _strict_string_list(value) or []


def _strict_string_list(value: object) -> list[str] | None:
    if not isinstance(value, (list, tuple, set)):
        return None
    values = list(value)
    if any(not isinstance(item, str) or not item for item in values):
        return None
    return values


def _occupancy_fields(result: Mapping[str, object], keys: tuple[str, ...], *, required: bool = True) -> list[str] | None:
    """Keep every reported occupant; an empty alias cannot mask uncertainty."""

    present = [key for key in keys if key in result]
    if not present and required:
        return None
    occupants: set[str] = set()
    for key in present:
        values = _strict_string_list(result[key])
        if values is None:
            return None
        occupants.update(values)
    return sorted(occupants)


def _echo_identity(identity: Mapping[str, object]) -> dict[str, object]:
    return copy.deepcopy(dict(identity))


def _reply_acknowledgement(kind: str, requested: str, ok: bool) -> str:
    fixed = {"reserve": "reserved", "start": "started", "beat": "beat", "inspect": "inspected"}
    if kind in fixed:
        return fixed[kind]
    if kind == "stop" and ok:
        return "stopped"
    return requested if requested in {"stopped", "started", "beat", "reserved", "inspected", "rejected", "unknown"} else "unknown"


# Names used by the host package and by small embedding callers.
HostExecutor = Executor
ControllerTrust = TrustedController
