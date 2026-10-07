"""Fenced host-side execution for the frozen executor v1 contract.

The executor deliberately has no transport or privilege implementation.  A
controller supplies an authenticated context and injects the host clock,
Systemd adapter, and GPU probe.  The state file is the durable authority for
generation fences; an uncertain observation never turns that authority into a
free lane.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
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
_LOCAL_ACTION_OPS = {
    "acquire",
    "renew",
    "release",
    "claim",
    "queue",
    "book",
    "cancel",
    "preempt",
    "chat-load",
    "chat-unload",
}
_LOCAL_ACTION_FIELDS = (
    "schema",
    "op",
    "lane",
    "args",
    "requester",
    "pipeline",
    "content_labels",
    "batch",
    "destination_site",
    "controller_id",
    "policy_hash",
    "manifest_hash",
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_SHORT_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_SHA256_RE = re.compile(r"^[A-Fa-f0-9]{64}$")
_LOCAL_ACTION_DOMAIN = b"flightctl/local-action/v1\0"


class StateCorruptError(RuntimeError):
    """Raised internally when the durable state is not a valid JSON object."""


class LocalActionError(ValueError):
    """Raised when a prospective local RPC cannot be admitted or canonicalised."""


class TrustedController:
    """Opaque controller context used by tests and local callers.

    The request mapping never creates or upgrades this object.  The transport
    boundary is expected to retain the instance supplied to ``Executor`` and
    pass that same instance to ``handle`` when it wants per-call checking.
    """

    __slots__ = ("controller_id", "_proof", "effective_principal")

    def __init__(
        self,
        controller_id: str,
        proof: object | None = None,
        *,
        effective_principal: Mapping[str, object] | None = None,
        principal: Mapping[str, object] | None = None,
    ) -> None:
        self.controller_id = controller_id
        self._proof = proof if proof is not None else object()
        if effective_principal is not None and principal is not None and dict(effective_principal) != dict(principal):
            raise ValueError("effective_principal and principal disagree")
        selected = effective_principal if effective_principal is not None else principal
        self.effective_principal = copy.deepcopy(dict(selected)) if isinstance(selected, Mapping) else None


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
    release_retry_after_s = 60
    release_wait_s = 600.0

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
        owner_release_checker: Callable[..., bool] | None = None,
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
        self._owner_release_checker = owner_release_checker
        self._approved_forced_preemptions = approved_forced_preemptions
        self._operation_timeout_s = max(0.01, float(operation_timeout_s))
        self._clock_skew_s = float(clock_skew_s)
        self._lock = threading.RLock()
        self._inflight_starts: set[tuple[str, int]] = set()
        self._start_operations: dict[tuple[str, int], dict[str, object]] = {}
        self._inflight_stops: set[tuple[str, int]] = set()
        self._owner_contexts: dict[str, object] = {}
        self._release_replay_contexts: dict[tuple[str, str], object] = {}
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
        *,
        release_request_id: str | None = None,
    ) -> dict[str, object]:
        """Execute one frozen request and return one frozen reply mapping."""

        auth = self._resolve_auth(authenticated_controller, controller, trusted_controller)
        with self._lock:
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
            return self._reserve(request, auth)
        if kind == "start":
            return self._start(request)
        if kind == "beat":
            return self._beat(request)
        if kind == "stop":
            return self._stop(request, auth, release_request_id=release_request_id)
        if kind == "inspect":
            return self._inspect(request)
        return self._early_rejection(request, f"unsupported executor operation: {kind}")

    execute = handle
    process = handle
    handle_request = handle

    def release_rpc(
        self,
        stop_request: Mapping[str, object],
        request_id: str,
        authenticated_controller: object = AUTH_UNSET,
        controller: object = AUTH_UNSET,
        trusted_controller: object = AUTH_UNSET,
        *,
        retry_after_s: int | None = None,
    ) -> dict[str, object]:
        """Adapt an owner stop to the frozen RPC release result contract.

        The executor wire operation remains ``stop``.  This boundary is the
        small controller-facing adapter that turns a verified owner stop
        whose exact cleanup is still draining into HTTP-202 pending release,
        while retaining the durable fence.  A pending response is stored
        without a token and is replayed for the same RPC request ID.
        """

        if not _is_identifier(request_id):
            return _rpc_error(request_id if isinstance(request_id, str) else "invalid-request", "invalid", "release request_id is invalid", False, "client")
        auth = self._resolve_auth(authenticated_controller, controller, trusted_controller)
        if not self._authenticated(auth):
            return _rpc_error(request_id, "denied", "controller authentication required", False, "policy")
        try:
            if self._validate_request_shape(stop_request) != "stop" or stop_request.get("stop_authority") != {"mode": "owner-release", "approval_id": None}:
                raise ValueError("release requires an owner stop")
            fingerprint = hashlib.sha256(json.dumps(stop_request, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
        except (TypeError, ValueError):
            return _rpc_error(request_id, "denied", "release requires a valid owner stop", False, "policy")
        identity = _dict(stop_request.get("identity")) if isinstance(stop_request, Mapping) else {}
        key = _lane_key(_dict(identity.get("lane"))) if isinstance(identity.get("lane"), Mapping) else None
        if key is not None:
            with self._lock:
                prior = self._state.get("release_replays", {}).get(key, {}).get(request_id)
                if isinstance(prior, Mapping):
                    owner = prior.get("owner_principal")
                    principal = _effective_principal(auth)
                    bound = self._release_replay_contexts.get((key, request_id), _MISSING)
                    owner_matches = principal == owner if isinstance(owner, Mapping) else bound is auth
                    if not owner_matches:
                        return _rpc_error(request_id, "denied", "release replay owner does not match", False, "policy")
                    if prior.get("fingerprint") != fingerprint:
                        return _rpc_error(request_id, "conflict", "release request ID was reused for another request", False, "conflict")
                    return copy.deepcopy(dict(prior["response"]))

        reply = self.handle(stop_request, authenticated_controller=auth, release_request_id=request_id)
        record = self.state_snapshot(_dict(identity.get("lane"))) if isinstance(identity.get("lane"), Mapping) else {}
        if (reply.get("acknowledgement") == "stopped" and reply.get("observed_state") == "stopping"
                and reply.get("uncertain") is False and record.get("release_pending") is True
                and record.get("identity") == identity):
            pending = self._pending_release_response(request_id, record, retry_after_s=retry_after_s)
            with self._lock:
                current = self._state.get("lanes", {}).get(key) if key is not None else None
                if isinstance(current, dict) and current.get("generation") == record.get("generation"):
                    current["release_pending_response"] = copy.deepcopy(pending)
                    current.setdefault("release_pending_responses", {})[request_id] = copy.deepcopy(pending)
                    current["release_request_id"] = request_id
                self._state.setdefault("release_replays", {}).setdefault(key, {})[request_id] = {
                    "fingerprint": fingerprint,
                    "owner_principal": _effective_principal(auth),
                    "response": copy.deepcopy(pending),
                }
                self._release_replay_contexts[(key, request_id)] = auth
                self._save_locked()
            return pending
        if reply.get("ok") is True:
            return self._release_success_response(request_id, reply)
        if reply.get("uncertain") is True or reply.get("observed_state") == "quarantined":
            return _rpc_error(request_id, "unknown", str(reply.get("error") or "release cleanup is uncertain"), True, "state")
        return _rpc_error(request_id, "denied", str(reply.get("error") or "release was rejected"), False, "policy")

    # Names used by controller adapters and small embedding callers.
    release = release_rpc
    release_request = release_rpc

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

    def _reserve(self, request: Mapping[str, object], authenticated_controller: object) -> dict[str, object]:
        identity = _dict(request.get("identity"))
        policy = _dict(request.get("execution_policy"))
        lane = _dict(identity.get("lane"))
        key = _lane_key(lane)
        with self._lock:
            if self._clock_frozen_locked():
                return self._reply(request, "rejected", False, self._lane_state(key), False, "clock uncertainty freezes admission")
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
            owner_principal = _effective_principal(authenticated_controller)
            self._owner_contexts[key] = authenticated_controller
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
                "owner_principal": owner_principal,
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
                "release_pending": False,
                "release_wait_deadline": None,
                "release_request_id": None,
                "release_pending_response": None,
                "release_pending_responses": {},
                "release_identity": None,
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
                cleanup = self._stop_record(
                    key,
                    cleanup_snapshot,
                    reason="stop requested during start",
                    internal=False,
                    pending_allowed=bool(cleanup_snapshot.get("release_pending")),
                    release_request_id=cleanup_snapshot.get("release_request_id") if isinstance(cleanup_snapshot.get("release_request_id"), str) else None,
                )
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
            self._stop_record(
                generation_key[0],
                cleanup_snapshot,
                reason="stop requested during late start completion",
                internal=False,
                pending_allowed=bool(cleanup_snapshot.get("release_pending")),
                release_request_id=cleanup_snapshot.get("release_request_id") if isinstance(cleanup_snapshot.get("release_request_id"), str) else None,
            )

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
                cleanup = self._stop_record(
                    key,
                    cleanup_snapshot,
                    reason="stop requested during start recovery",
                    internal=False,
                    pending_allowed=bool(cleanup_snapshot.get("release_pending")),
                    release_request_id=cleanup_snapshot.get("release_request_id") if isinstance(cleanup_snapshot.get("release_request_id"), str) else None,
                )
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
        return self._stop_record(
            key,
            cleanup_snapshot,
            reason="stop after pending start recovery",
            internal=False,
            pending_allowed=bool(cleanup_snapshot.get("release_pending")),
            release_request_id=cleanup_snapshot.get("release_request_id") if isinstance(cleanup_snapshot.get("release_request_id"), str) else None,
        )

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

    def _stop(
        self,
        request: Mapping[str, object],
        authenticated_controller: object,
        *,
        release_request_id: str | None = None,
    ) -> dict[str, object]:
        identity = _dict(request.get("identity"))
        key = _lane_key(_dict(identity.get("lane")))
        authority = _dict(request.get("stop_authority"))
        pending_restart_snapshot: dict[str, object] | None = None
        with self._lock:
            record = self._state["lanes"].get(key)
            if not isinstance(record, dict) or not self._full_identity_matches(record, identity):
                return self._reply(request, "rejected", False, self._lane_state(key), False, "stop identity mismatch")
            policy = _dict(record.get("policy"))
            mode = authority.get("mode")
            approval_id = authority.get("approval_id")
            if mode == "owner-release":
                if approval_id is not None or not self._owner_release_authorized(authenticated_controller, request, record):
                    return self._reply(request, "rejected", False, str(record.get("state", "unknown")), False, "owner release is not authorized for this lease")
            elif bool(policy.get("protected")):
                if mode != "approved-forced-preemption" or not isinstance(approval_id, str) or not self._approval_valid(approval_id, request, record):
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
            elif mode == "approved-forced-preemption":
                if not isinstance(approval_id, str) or not self._approval_valid(approval_id, request, record):
                    return self._reply(request, "rejected", False, str(record.get("state", "unknown")), False, "forced preemption lacks trusted approval")
            elif mode != "controller-match" or approval_id is not None:
                return self._reply(request, "rejected", False, str(record.get("state", "unknown")), False, "invalid stop authority")
            generation_key = (key, int(record.get("generation", 0)))
            if record.get("start_in_flight") or generation_key in self._inflight_starts:
                if mode == "owner-release" and self._release_wait_expired_locked(record):
                    self._quarantine(key, "release wait deadline elapsed before start completed")
                    return self._reply(request, "stopped", False, "quarantined", True, "release wait deadline elapsed before start completed")
                record["state"] = "stopping"
                record["stop_requested"] = True
                record["closed_generation"] = max(int(record.get("closed_generation", 0)), int(record.get("generation", 0)))
                if mode == "owner-release":
                    record["release_pending"] = True
                    if not isinstance(record.get("release_wait_deadline"), Mapping):
                        record["release_wait_deadline"] = self._new_release_wait_deadline_locked()
                    if release_request_id is not None:
                        record["release_request_id"] = release_request_id
                    record["release_identity"] = copy.deepcopy(identity)
                self._save_locked()
                if generation_key in self._inflight_starts:
                    return self._reply(
                        request,
                        "stopped",
                        False,
                        "stopping" if mode == "owner-release" else "quarantined",
                        False if mode == "owner-release" else True,
                        "stop queued until start outcome is known",
                    )
                pending_restart_snapshot = copy.deepcopy(record)
            snapshot = copy.deepcopy(record) if pending_restart_snapshot is None else None
        if pending_restart_snapshot is not None:
            return self._recover_pending_start_for_stop(request, key, pending_restart_snapshot)
        assert snapshot is not None
        return self._stop_record(
            key,
            snapshot,
            reason="owner release" if mode == "owner-release" else "controller stop",
            internal=False,
            pending_allowed=mode == "owner-release",
            release_request_id=release_request_id,
        )

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

    def _stop_record(
        self,
        key: str,
        snapshot: dict[str, object],
        *,
        reason: str,
        internal: bool,
        pending_allowed: bool = False,
        release_request_id: str | None = None,
    ) -> dict[str, object]:
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
        if stop_ok and pending_allowed and self._cleanup_is_pending(
            inspect_result,
            gpu_result,
            unit,
            invocation,
        ):
            expired = False
            with self._lock:
                current = self._state["lanes"].get(key)
                if isinstance(current, dict) and current.get("generation") == snapshot.get("generation") and current.get("identity") == identity:
                    if self._release_wait_expired_locked(current):
                        current["state"] = "quarantined"
                        current["release_pending"] = False
                        current["quarantine_reason"] = "release wait deadline elapsed before emptiness was confirmed"
                        self._close_record_locked(current)
                        expired = True
                    else:
                        current["state"] = "stopping"
                        current["release_pending"] = True
                        current["quarantine_reason"] = None
                        if not isinstance(current.get("release_wait_deadline"), Mapping):
                            current["release_wait_deadline"] = self._new_release_wait_deadline_locked()
                        if release_request_id is not None:
                            current["release_request_id"] = release_request_id
                        current["release_identity"] = copy.deepcopy(identity)
                    self._save_locked()
            if expired:
                return self._reply_for_identity(
                    identity,
                    "stop",
                    "stopped",
                    False,
                    "quarantined",
                    True,
                    "release wait deadline elapsed before emptiness was confirmed",
                    cgroup=cgroup,
                    gpu_tenants=gpu_tenants,
                )
            return self._reply_for_identity(
                identity,
                "stop",
                "stopped",
                False,
                "stopping",
                False,
                "matching stop accepted; emptiness not yet confirmed",
                cgroup=cgroup,
                gpu_tenants=gpu_tenants,
            )
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
                    current["release_pending"] = False
                    current["release_wait_deadline"] = None
                    current["release_request_id"] = None
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

    def _cleanup_is_pending(
        self,
        systemd_result: Mapping[str, object],
        gpu_result: Mapping[str, object],
        unit: str,
        invocation: str,
    ) -> bool:
        """Classify a verified, still-draining cleanup separately from uncertainty."""

        if not isinstance(systemd_result, Mapping) or systemd_result.get("unit") != unit or systemd_result.get("invocation") != invocation:
            return False
        if systemd_result.get("ok") is not True or systemd_result.get("status") not in _KNOWN_SUCCESS_STATUSES:
            return False
        if "complete" in systemd_result and systemd_result.get("complete") is not True:
            return False
        cgroup = _occupancy_fields(systemd_result, ("cgroup_occupants", "occupants"))
        systemd_gpu = _occupancy_fields(systemd_result, ("gpu_occupants", "gpu_tenants"), required=False)
        if cgroup is None or systemd_gpu is None or systemd_gpu:
            return False
        if "active" in systemd_result and not isinstance(systemd_result.get("active"), bool):
            return False
        state = systemd_result.get("state")
        if state is not None and state not in {"free", "inactive", "absent", "dead", "running", "starting", "stopping", "active"}:
            return False
        if (cgroup or state in {"running", "starting", "stopping", "active"}) and systemd_result.get("active") is False:
            return False
        if state in {"free", "inactive", "absent", "dead"} and (cgroup or systemd_result.get("active") is True):
            return False

        # The frozen GPU observation has no invocation ownership proof. Any
        # tenant is unexplained; failed/partial unload or probe results remain
        # uncertain even when the matching cgroup is still draining.
        gpu_ok, _, _ = self._cleanup_gpu_observation(gpu_result)
        if not gpu_ok:
            return False

        systemd_active = bool(cgroup) or systemd_result.get("active") is True or state in {"running", "starting", "stopping", "active"}
        return systemd_active

    def _new_release_wait_deadline_locked(self) -> dict[str, object]:
        now = self._read_clock()
        return {
            "boot_id": now[2],
            "deadline_s": now[1] + float(self.release_wait_s),
            "utc_anchor": _utc_text(now[0]),
            "monotonic_anchor_s": now[1],
        }

    def _release_wait_expired_locked(self, record: Mapping[str, object]) -> bool:
        deadline = record.get("release_wait_deadline")
        if not isinstance(deadline, Mapping):
            return False
        if deadline.get("boot_id") != self._state.get("boot_id"):
            return True
        try:
            return self._read_clock()[1] >= float(deadline["deadline_s"])
        except (KeyError, TypeError, ValueError):
            return True

    def _pending_release_response(
        self,
        request_id: str,
        record: Mapping[str, object],
        *,
        retry_after_s: int | None,
    ) -> dict[str, object]:
        deadline = record.get("release_wait_deadline")
        if not isinstance(deadline, Mapping):
            with self._lock:
                deadline = self._new_release_wait_deadline_locked()
        retry = self.release_retry_after_s if retry_after_s is None else retry_after_s
        if not isinstance(retry, int) or isinstance(retry, bool) or retry < 1:
            retry = self.release_retry_after_s
        return {
            "schema": 1,
            "request_id": request_id,
            "status": 202,
            "data": {
                "kind": "pending",
                "operation": "release",
                "request_id": request_id,
                "queue_id": None,
                "retry_after_s": retry,
                "wait_deadline": copy.deepcopy(dict(deadline)),
                "reason": "matching stop accepted; emptiness not yet confirmed",
            },
            "error": None,
        }

    def _release_success_response(self, request_id: str, reply: Mapping[str, object]) -> dict[str, object]:
        identity = _dict(reply.get("echoed_identity"))
        lane = _dict(identity.get("lane"))
        generation = identity.get("generation")
        if not isinstance(generation, int) or generation < 1 or set(lane) != {"site_id", "host_id", "lane_id"}:
            return _rpc_error(request_id, "unknown", "release completed without a valid reservation identity", True, "state")
        return {
            "schema": 1,
            "request_id": request_id,
            "status": 200,
            "data": {
                "kind": "mutation",
                "operation": "release",
                "record_type": "lease",
                "record_id": f"release-{lane['lane_id']}",
                "state": "free",
                "revision": generation,
                "reservation": {"lane": copy.deepcopy(lane), "generation": generation, "state": "released"},
            },
            "error": None,
        }

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
                record["release_pending"] = False
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
        release_deadline = record.get("release_wait_deadline")
        if isinstance(release_deadline, Mapping):
            try:
                release_anchor_utc = _parse_utc(str(release_deadline["utc_anchor"]))
                release_deadline_s = float(release_deadline["deadline_s"])
                release_anchor_mono = float(release_deadline["monotonic_anchor_s"])
                release_remaining = (release_anchor_utc + timedelta(seconds=release_deadline_s - release_anchor_mono) - now[0]).total_seconds()
            except (KeyError, TypeError, ValueError, OverflowError):
                return "reboot reconciliation has an invalid release wait deadline"
            release_deadline_copy = copy.deepcopy(dict(release_deadline))
            release_deadline_copy["boot_id"] = now[2]
            release_deadline_copy["utc_anchor"] = _utc_text(now[0])
            release_deadline_copy["monotonic_anchor_s"] = now[1]
            release_deadline_copy["deadline_s"] = now[1] + max(0.0, release_remaining)
            record["release_wait_deadline"] = release_deadline_copy
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

    def _resolve_auth(self, authenticated_controller: object, controller: object, trusted_controller: object) -> object:
        auth = authenticated_controller if authenticated_controller is not AUTH_UNSET else controller
        if auth is AUTH_UNSET:
            auth = trusted_controller
        if auth is AUTH_UNSET and self._trusted_controller is not None:
            auth = self._trusted_controller
        return auth

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

    def _owner_release_authorized(
        self,
        authenticated_controller: object,
        request: Mapping[str, object],
        record: Mapping[str, object],
    ) -> bool:
        """Check owner release while the reservation fence is held."""

        if self._owner_release_checker is not None:
            try:
                if not bool(self._owner_release_checker(authenticated_controller, request, record)):
                    return False
            except TypeError:
                try:
                    if not bool(self._owner_release_checker(request, record)):
                        return False
                except Exception:
                    return False
            except Exception:
                return False
            # The injected checker is the controller's atomic ownership and
            # user-intent boundary. Token/lane/generation/instance/unit/
            # invocation fencing was checked before this method was called.
            return True

        principal = _effective_principal(authenticated_controller)
        owner = record.get("owner_principal")
        if isinstance(owner, Mapping):
            return principal is not None and dict(owner) == principal
        key = _lane_key(_dict(_dict(request.get("identity")).get("lane")))
        bound = self._owner_contexts.get(key, _MISSING)
        if bound is _MISSING:
            return False
        if isinstance(bound, (str, int, bytes)) and isinstance(authenticated_controller, type(bound)):
            return bound == authenticated_controller
        return bound is authenticated_controller

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


def validate_content_labels(value: object = _MISSING) -> list[str]:
    """Return caller-declared labels in their original order, or reject them."""

    if value is _MISSING:
        return []
    if not isinstance(value, list):
        raise LocalActionError("admission.content_labels must be an array when supplied")
    labels: list[str] = []
    seen: set[str] = set()
    for label in value:
        if not isinstance(label, str) or not _is_identifier(label) or label in seen:
            raise LocalActionError("admission.content_labels must contain unique identifiers")
        labels.append(label)
        seen.add(label)
    return labels


def content_labels_admitted(value: object = _MISSING, content_policy: Iterable[str] | None = None) -> bool:
    """Check labels against an injected current content policy.

    An omitted or empty label list is admitted by this label-only check, but
    it does not assert that any other admission or purpose rule passed.
    """

    labels = validate_content_labels(value)
    if content_policy is None:
        return not labels
    allowed = set(validate_content_labels(list(content_policy)))
    return all(label in allowed for label in labels)


def local_action_projection(
    request: Mapping[str, object],
    *,
    destination_site: str,
    controller_id: str,
    site_policy_hash: str | None = None,
    current_pipeline: Mapping[str, object] | None = None,
    content_policy: Iterable[str] | None = None,
) -> dict[str, object]:
    """Build the frozen, transport-independent local-action hash projection."""

    _validate_local_action_request(request)
    if not _is_short_identifier(destination_site) or not _is_identifier(controller_id):
        raise LocalActionError("local action destination or controller is invalid")
    admission = _dict(request["admission"])
    pipeline = admission["pipeline"]
    if pipeline is not None:
        pipeline = copy.deepcopy(dict(pipeline))
        if current_pipeline is not None:
            if not isinstance(current_pipeline, Mapping):
                raise LocalActionError("current pipeline is invalid")
            _validate_current_pipeline(pipeline, current_pipeline)
            current_policy_hash = current_pipeline.get("policy_hash")
            if not isinstance(current_policy_hash, str) or not _is_sha256(current_policy_hash):
                raise LocalActionError("current pipeline has no valid policy hash")
            policy_hash = current_policy_hash
            if content_policy is None and isinstance(current_pipeline.get("content_policy"), list):
                content_policy = current_pipeline["content_policy"]
        else:
            policy_hash = pipeline.get("policy_hash")
    else:
        policy_hash = site_policy_hash

    if not isinstance(policy_hash, str) or not _is_sha256(policy_hash):
        raise LocalActionError("local action has no current policy hash")
    labels = validate_content_labels(admission.get("content_labels", _MISSING))
    if content_policy is not None and not content_labels_admitted(labels, content_policy):
        raise LocalActionError("content label is outside the current content policy")
    if admission.get("delegation") is not None:
        raise LocalActionError("local action cannot silently discard a delegation")
    args = copy.deepcopy(dict(request["args"]))
    if args.get("signed_manifest") is not None:
        raise LocalActionError("local action cannot silently discard a signed manifest")
    args.pop("approval_id", None)

    projection = {
        "schema": request["schema"],
        "op": request["op"],
        "lane": copy.deepcopy(request["lane"]),
        "args": args,
        "requester": _effective_principal(_dict(admission["ingress"])),
        "pipeline": pipeline,
        "content_labels": labels,
        "batch": copy.deepcopy(admission["batch"]),
        "destination_site": destination_site,
        "controller_id": controller_id,
        "policy_hash": policy_hash,
        "manifest_hash": None,
    }
    if not isinstance(projection["requester"], Mapping):
        raise LocalActionError("authenticated ingress has no effective principal")
    return projection


def canonical_local_action_payload(
    request: Mapping[str, object],
    *,
    destination_site: str,
    controller_id: str,
    site_policy_hash: str | None = None,
    current_pipeline: Mapping[str, object] | None = None,
    content_policy: Iterable[str] | None = None,
) -> tuple[bytes, str]:
    """Return canonical UTF-8 bytes and the domain-separated SHA-256 digest."""

    projection = local_action_projection(
        request,
        destination_site=destination_site,
        controller_id=controller_id,
        site_policy_hash=site_policy_hash,
        current_pipeline=current_pipeline,
        content_policy=content_policy,
    )
    pairs = [[field, projection[field]] for field in _LOCAL_ACTION_FIELDS]
    canonical = _jcs_serialize(pairs)
    digest = hashlib.sha256(_LOCAL_ACTION_DOMAIN + canonical).hexdigest()
    return canonical, digest


def local_action_payload_hash(
    request: Mapping[str, object],
    *,
    destination_site: str,
    controller_id: str,
    site_policy_hash: str | None = None,
    current_pipeline: Mapping[str, object] | None = None,
    content_policy: Iterable[str] | None = None,
) -> str:
    """Return only the canonical local-action payload digest."""

    return canonical_local_action_payload(
        request,
        destination_site=destination_site,
        controller_id=controller_id,
        site_policy_hash=site_policy_hash,
        current_pipeline=current_pipeline,
        content_policy=content_policy,
    )[1]


# Descriptive aliases for callers that name the operation by its hash.
project_local_action = local_action_projection
local_action_hash = local_action_payload_hash


def _validate_local_action_request(request: Mapping[str, object]) -> None:
    if not isinstance(request, Mapping):
        raise LocalActionError("execution RPC must be a mapping")
    required = {"schema", "request_id", "op", "lane", "args", "idempotency_scope", "request_fingerprint", "admission"}
    if set(request) != required:
        raise LocalActionError("execution RPC has fields outside the frozen request contract")
    if type(request.get("schema")) is not int or request.get("schema") != 1 or not isinstance(request.get("request_id"), str) or not _is_identifier(request["request_id"]):
        raise LocalActionError("execution RPC schema or request_id is invalid")
    op = request.get("op")
    if op not in _LOCAL_ACTION_OPS:
        raise LocalActionError("operation is not a local action")
    lane = request.get("lane")
    if lane is not None and (not isinstance(lane, str) or not _is_short_identifier(lane)):
        raise LocalActionError("lane selector is invalid")
    args = request.get("args")
    if not isinstance(args, Mapping):
        raise LocalActionError("execution RPC args must be an object")
    _validate_local_action_args(str(op), args)
    scope = request.get("idempotency_scope")
    if not isinstance(scope, Mapping) or set(scope) != {"scope", "controller_id"} or scope.get("scope") not in {"authenticated-principal", "controller"} or not isinstance(scope.get("controller_id"), str) or not _is_identifier(scope["controller_id"]):
        raise LocalActionError("idempotency scope is invalid")
    if not isinstance(request.get("request_fingerprint"), str) or not _is_sha256(request["request_fingerprint"]):
        raise LocalActionError("request fingerprint is invalid")

    admission = request.get("admission")
    if not isinstance(admission, Mapping) or set(admission) - {"execution", "content_labels", "approval", "pipeline", "delegation", "ingress", "batch"}:
        raise LocalActionError("admission contains unsupported fields")
    if not {"execution", "approval", "pipeline", "delegation", "ingress", "batch"}.issubset(admission) or admission.get("execution") != "atomic":
        raise LocalActionError("admission is incomplete")
    validate_content_labels(admission.get("content_labels", _MISSING))
    approval = admission.get("approval")
    if not isinstance(approval, Mapping) or set(approval) != {"approval_id", "required", "consume_atomically"} or approval.get("consume_atomically") is not True or not isinstance(approval.get("required"), bool):
        raise LocalActionError("approval selection is invalid")
    if approval["required"] and not _is_identifier(approval.get("approval_id")):
        raise LocalActionError("required approval has no identifier")
    if not approval["required"] and approval.get("approval_id") is not None:
        raise LocalActionError("optional approval cannot carry an identifier")
    pipeline = admission.get("pipeline")
    if pipeline is not None:
        _validate_pipeline_binding(pipeline)
    if not (admission.get("delegation") is None or isinstance(admission.get("delegation"), Mapping)):
        raise LocalActionError("delegation is invalid")
    _validate_ingress(admission.get("ingress"))
    if admission.get("batch") is not None:
        _validate_batch_registration(admission.get("batch"))


def _validate_local_action_args(op: str, args: Mapping[str, object]) -> None:
    specs: dict[str, tuple[set[str], set[str]]] = {
        "acquire": ({"purpose", "class", "est_s", "max_s", "booking_id", "queue_id", "pipeline_ref", "signed_manifest"}, {"purpose", "class", "est_s", "max_s"}),
        "renew": ({"token", "instance", "extend_s"}, {"token"}),
        "release": ({"token", "instance", "extend_s"}, {"token"}),
        "claim": ({"token", "generation", "generation_source", "instance", "booking_id", "revision"}, set()),
        "queue": ({"action", "purpose", "class", "max_wait_s", "queue_id"}, {"action"}),
        "book": ({"start", "end", "purpose"}, {"start", "end", "purpose"}),
        "cancel": ({"booking_id", "revision"}, {"booking_id"}),
        "preempt": ({"token", "approval_id"}, {"token", "approval_id"}),
        "chat-load": ({"pipeline_ref", "purpose"}, {"pipeline_ref", "purpose"}),
        "chat-unload": ({"occupant_id", "generation"}, {"occupant_id", "generation"}),
    }
    allowed, required = specs[op]
    if set(args) - allowed or not required.issubset(args):
        raise LocalActionError(f"{op} args are invalid")
    if "purpose" in args and (not isinstance(args["purpose"], str) or not 1 <= len(args["purpose"]) <= 512):
        raise LocalActionError("purpose must contain 1 to 512 characters")
    if "token" in args and (not isinstance(args["token"], str) or len(args["token"]) < 16 or (op != "preempt" and len(args["token"]) > 512)):
        raise LocalActionError("token must contain 16 to 512 characters")
    if "instance" in args and args["instance"] is not None and not _is_identifier(args["instance"]):
        raise LocalActionError("instance is invalid")
    if op == "acquire":
        if not isinstance(args.get("purpose"), str) or not args["purpose"] or args.get("class") not in _EXECUTION_CLASSES or not _positive_integer(args.get("est_s")) or not _positive_integer(args.get("max_s")):
            raise LocalActionError("acquire args are invalid")
        if args.get("pipeline_ref") is not None and (not isinstance(args.get("pipeline_ref"), str) or not _is_short_identifier(args["pipeline_ref"] or "")):
            raise LocalActionError("acquire pipeline_ref is invalid")
        if args.get("signed_manifest") is not None and not isinstance(args.get("signed_manifest"), Mapping):
            raise LocalActionError("acquire signed_manifest is invalid")
        for field in ("booking_id", "queue_id"):
            if args.get(field) is not None and not _is_identifier(args[field]):
                raise LocalActionError(f"acquire {field} is invalid")
    elif op in {"renew", "release"}:
        _validate_token_args(args)
    elif op == "claim":
        adoption = "generation" in args or "generation_source" in args or "token" in args
        booking = "booking_id" in args or "revision" in args
        if adoption == booking or adoption and set(args) - {"token", "generation", "generation_source", "instance"} or booking and set(args) - {"booking_id", "revision"}:
            raise LocalActionError("claim args are invalid")
        if adoption and (not isinstance(args.get("token"), str) or len(args["token"]) < 16 or not _positive_integer(args.get("generation")) or args.get("generation_source") != "authenticated-adoption"):
            raise LocalActionError("claim adoption args are invalid")
        if booking and (not isinstance(args.get("booking_id"), str) or not _is_identifier(args["booking_id"]) or not _positive_integer(args.get("revision"))):
            raise LocalActionError("claim booking args are invalid")
    elif op == "queue":
        action = args.get("action")
        if action == "add":
            if set(args) != {"action", "purpose", "class", "max_wait_s"} or not isinstance(args.get("purpose"), str) or not args["purpose"] or args.get("class") not in _EXECUTION_CLASSES or not _positive_integer(args.get("max_wait_s")):
                raise LocalActionError("queue add args are invalid")
        elif action == "refresh":
            if set(args) != {"action", "queue_id", "max_wait_s"} or not isinstance(args.get("queue_id"), str) or not _is_identifier(args["queue_id"]) or not _positive_integer(args.get("max_wait_s")):
                raise LocalActionError("queue refresh args are invalid")
        elif action == "remove":
            if set(args) != {"action", "queue_id"} or not isinstance(args.get("queue_id"), str) or not _is_identifier(args["queue_id"]):
                raise LocalActionError("queue remove args are invalid")
        elif action == "list":
            if set(args) != {"action"}:
                raise LocalActionError("queue list args are invalid")
        else:
            raise LocalActionError("queue action is invalid")
    elif op == "book":
        if any(not isinstance(args.get(field), str) or not args[field] for field in ("start", "end", "purpose")):
            raise LocalActionError("book args are invalid")
        for field in ("start", "end"):
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", args[field]):
                raise LocalActionError("booking timestamps must be UTC date-times")
            try:
                _parse_utc(args[field])
            except ValueError as exc:
                raise LocalActionError("booking timestamp is invalid") from exc
    elif op == "cancel":
        if not isinstance(args.get("booking_id"), str) or not _is_identifier(args["booking_id"]) or ("revision" in args and not _positive_integer(args.get("revision"))):
            raise LocalActionError("cancel args are invalid")
    elif op == "preempt":
        if not isinstance(args.get("token"), str) or len(args["token"]) < 16 or not isinstance(args.get("approval_id"), str) or not _is_identifier(args["approval_id"]):
            raise LocalActionError("preempt args are invalid")
    elif op == "chat-load":
        if not isinstance(args.get("pipeline_ref"), str) or not _is_short_identifier(args["pipeline_ref"]) or not isinstance(args.get("purpose"), str) or not args["purpose"]:
            raise LocalActionError("chat-load args are invalid")
    elif op == "chat-unload":
        if not isinstance(args.get("occupant_id"), str) or not _is_identifier(args["occupant_id"]) or not _positive_integer(args.get("generation")):
            raise LocalActionError("chat-unload args are invalid")


def _validate_token_args(args: Mapping[str, object]) -> None:
    if not isinstance(args.get("token"), str) or len(args["token"]) < 16 or ("instance" in args and args["instance"] is not None and (not isinstance(args["instance"], str) or not _is_identifier(args["instance"]))) or ("extend_s" in args and not _positive_integer(args.get("extend_s"))):
        raise LocalActionError("token args are invalid")


def _validate_pipeline_binding(pipeline: object) -> None:
    if not isinstance(pipeline, Mapping) or set(pipeline) != {"pipeline_id", "version", "revision", "purpose", "policy_hash"} or not isinstance(pipeline.get("pipeline_id"), str) or not _is_short_identifier(pipeline["pipeline_id"]) or not isinstance(pipeline.get("version"), str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", pipeline["version"]) or not _positive_integer(pipeline.get("revision")) or not isinstance(pipeline.get("purpose"), str) or not pipeline["purpose"] or not isinstance(pipeline.get("policy_hash"), str) or not _is_sha256(pipeline["policy_hash"]):
        raise LocalActionError("pipeline binding is invalid")
    if len(pipeline["purpose"]) > 512:
        raise LocalActionError("pipeline purpose exceeds 512 characters")


def _validate_current_pipeline(request_pipeline: Mapping[str, object], current_pipeline: Mapping[str, object]) -> None:
    for field in ("pipeline_id", "version", "revision", "purpose", "policy_hash"):
        if request_pipeline.get(field) != current_pipeline.get(field):
            raise LocalActionError("execution RPC pipeline is stale")
    if "content_policy" in current_pipeline:
        if not isinstance(current_pipeline["content_policy"], list):
            raise LocalActionError("current pipeline content policy is invalid")
        validate_content_labels(current_pipeline["content_policy"])


def _validate_batch_registration(batch: object) -> None:
    if not isinstance(batch, Mapping) or set(batch) != {"batch_id", "arms", "dependencies", "registered_before_execution", "all_arms_visible"}:
        raise LocalActionError("batch registration is invalid")
    if not isinstance(batch.get("batch_id"), str) or not _is_identifier(batch["batch_id"]) or batch.get("registered_before_execution") is not True or batch.get("all_arms_visible") is not True:
        raise LocalActionError("batch registration metadata is invalid")
    arms = batch.get("arms")
    if not isinstance(arms, list) or not arms:
        raise LocalActionError("batch arms are invalid")
    arm_ids: set[str] = set()
    for arm in arms:
        if not isinstance(arm, Mapping) or set(arm) != {"arm_id", "predecessor", "dependencies"} or not isinstance(arm.get("arm_id"), str) or not _is_identifier(arm["arm_id"]) or arm["arm_id"] in arm_ids:
            raise LocalActionError("batch arm is invalid")
        arm_ids.add(arm["arm_id"])
        predecessor = arm.get("predecessor")
        if predecessor is not None and (not isinstance(predecessor, str) or not _is_identifier(predecessor)):
            raise LocalActionError("batch predecessor is invalid")
        dependencies = arm.get("dependencies")
        if not isinstance(dependencies, list) or any(not isinstance(item, str) or not _is_identifier(item) for item in dependencies) or len(set(dependencies)) != len(dependencies):
            raise LocalActionError("batch dependencies are invalid")
    dependencies = batch.get("dependencies")
    if not isinstance(dependencies, list) or any(not isinstance(item, str) or not _is_identifier(item) for item in dependencies) or len(set(dependencies)) != len(dependencies):
        raise LocalActionError("batch dependency summary is invalid")


def _validate_ingress(ingress: object) -> None:
    required = {"actor", "subject", "controller_id", "authenticated_peer", "peer_source", "auth_method", "transport_binding", "peer_verified", "forwarding_headers_ignored", "operator_elevation"}
    if not isinstance(ingress, Mapping) or set(ingress) != required or not _valid_principal(ingress.get("actor")) or not (ingress.get("subject") is None or _valid_principal(ingress.get("subject"))) or not isinstance(ingress.get("controller_id"), str) or not _is_identifier(ingress["controller_id"]) or not isinstance(ingress.get("authenticated_peer"), str) or not _is_identifier(ingress["authenticated_peer"]) or ingress.get("peer_source") != "socket-peer" or ingress.get("transport_binding") != "transport-independent" or ingress.get("peer_verified") is not True or ingress.get("forwarding_headers_ignored") is not True or ingress.get("auth_method") not in {"local", "tailnet-peer", "ssh-sk", "webauthn"} or ingress.get("operator_elevation") not in {"none", "approval-only"}:
        raise LocalActionError("authenticated ingress is invalid")


def _valid_principal(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"site_id", "tenant_id", "issuer", "subject"}:
        return False
    return isinstance(value.get("site_id"), str) and _is_short_identifier(value["site_id"]) and all(isinstance(value.get(field), str) and _is_identifier(value[field]) for field in ("tenant_id", "issuer", "subject"))


def _effective_principal(context: object) -> dict[str, object] | None:
    value: object = None
    if isinstance(context, TrustedController):
        value = context.effective_principal
    elif isinstance(context, Mapping):
        if "actor" in context and "subject" in context:
            value = context.get("subject") or context.get("actor")
        else:
            value = context.get("effective_principal", context.get("principal"))
    else:
        value = getattr(context, "effective_principal", None)
        if value is None:
            value = getattr(context, "principal", None)
    if not _valid_principal(value):
        return None
    return copy.deepcopy(dict(value))


def _is_identifier(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER_RE.fullmatch(value) is not None


def _is_short_identifier(value: object) -> bool:
    return isinstance(value, str) and _SHORT_IDENTIFIER_RE.fullmatch(value) is not None


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _jcs_serialize(value: object) -> bytes:
    if value is None or isinstance(value, bool):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if isinstance(value, str):
        try:
            encoded = value.encode("utf-16-be")
        except UnicodeEncodeError as exc:
            raise LocalActionError("JCS rejects unpaired Unicode surrogates") from exc
        del encoded
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > 2**53 - 1:
            raise LocalActionError("JCS integer is outside the I-JSON safe range")
        return str(value).encode("ascii")
    if isinstance(value, float):
        return _jcs_number(value).encode("ascii")
    if isinstance(value, list):
        return b"[" + b",".join(_jcs_serialize(item) for item in value) + b"]"
    if isinstance(value, Mapping):
        pairs: list[tuple[bytes, str, object]] = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise LocalActionError("JCS object keys must be strings")
            try:
                sort_key = key.encode("utf-16-be")
            except UnicodeEncodeError as exc:
                raise LocalActionError("JCS rejects unpaired Unicode object keys") from exc
            pairs.append((sort_key, key, item))
        pairs.sort(key=lambda item: item[0])
        rendered: list[bytes] = []
        for _, key, item in pairs:
            rendered.append(_jcs_serialize(key) + b":" + _jcs_serialize(item))
        return b"{" + b",".join(rendered) + b"}"
    raise LocalActionError(f"unsupported JCS value type: {type(value).__name__}")


def _jcs_number(value: float) -> str:
    if not math.isfinite(value):
        raise LocalActionError("JCS rejects non-finite numbers")
    if value == 0:
        return "0"
    negative = value < 0
    raw = repr(abs(value)).lower()
    if "e" in raw:
        mantissa, exponent_text = raw.split("e", 1)
        exponent = int(exponent_text)
    else:
        mantissa = raw
        exponent = 0
    if "." in mantissa:
        whole, fraction = mantissa.split(".", 1)
    else:
        whole, fraction = mantissa, ""
    digits = whole + fraction
    decimal_position = len(whole) + exponent
    digits = digits.rstrip("0") or "0"
    magnitude_exponent = decimal_position - 1
    if -6 <= magnitude_exponent < 21:
        if decimal_position <= 0:
            rendered = "0." + ("0" * -decimal_position) + digits
        elif decimal_position >= len(digits):
            rendered = digits + ("0" * (decimal_position - len(digits)))
        else:
            rendered = digits[:decimal_position] + "." + digits[decimal_position:]
    else:
        coefficient = digits[0] if len(digits) == 1 else digits[0] + "." + digits[1:]
        sign = "+" if magnitude_exponent >= 0 else "-"
        rendered = f"{coefficient}e{sign}{abs(magnitude_exponent)}"
    return "-" + rendered if negative else rendered


def _rpc_error(request_id: str, code: str, message: str, retryable: bool, failure_class: str) -> dict[str, object]:
    return {
        "schema": 1,
        "request_id": request_id,
        "status": 503 if code in {"unknown", "timeout", "unavailable"} else 403 if code == "denied" else 409 if code in {"busy", "conflict", "fenced"} else 403,
        "data": None,
        "error": {"code": code, "message": message[:512], "retryable": retryable, "failure_class": failure_class},
    }


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


# ----------------------------------------------------------------------
# Executor protocol v2 (contracts/v2/executor.schema.json), holder mode

_V2_DUE = ("expiry", "max-end", "heartbeat-stale")  # the deadline kinds that act, in report order; grace never acts
_V2_PROTECTED_STOP_MODES = frozenset({"owner-release", "operator-stop", "approved-forced-preemption", "holder-lost"})
_V2_UTC = "%Y-%m-%dT%H:%M:%SZ"
_V2_SCHEMA = Path(__file__).resolve().parents[1] / "contracts" / "v2" / "executor.schema.json"  # read at run time, never copied
_V2_SCHEMA_FILES: dict[Path, Any] = {}  # each frozen schema file as read, kept for the life of the process
_V2_DATE_TIME = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.[0-9]+)?Z")
_V2_PATTERN_ANCHOR = re.compile(r"\\.|\[(?:\\.|[^\\\]])*\]|(\$)")  # an escape, a class, or (group 1) a $ that anchors
_V2_TYPES: dict[str, Callable[[Any], bool]] = {  # a boolean is never a number; an integer is a number with no fraction (7, 7.0)
    "null": lambda value: value is None, "boolean": lambda value: isinstance(value, bool), "string": lambda value: isinstance(value, str),
    "object": lambda value: isinstance(value, dict), "array": lambda value: isinstance(value, list),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool) or isinstance(value, float) and value.is_integer()}


class ExecutorV2:
    """Executor protocol v2 for holder-mode leases: reserve, beat, stop, inspect, ceiling and extend, and the deadline enforcer.

    A received relative deadline is anchored on this host's own monotonic clock and boot when the
    request is handled (G02): the sender's clocks never set one and no boot id is compared across
    hosts. Each record in the store document's ``lanes`` holds the lane's highest generation and its
    fence; every change is saved before the reply is built. A lane is freed only on an occupancy
    observation that proves it empty. The injected Inhibitor port keeps the host awake for exactly
    the lease (D-pow-3): a reserve that asks for it holds it before the fence is written, and the
    lane is freed only after its release is confirmed; an unconfirmed call is never taken as done.
    """

    def __init__(self, clock: Clock, *, host_id: str, store: Any, lane_cards: Mapping[str, Iterable[str]],
                 occupancy: Any | None = None, inhibitor: Any | None = None, max_clock_skew_s: float = 30.0, timeout_s: float = 10.0) -> None:
        self.clock = clock
        self.host_id = host_id
        self._store = store
        self._lane_cards = lane_cards
        self._occupancy = occupancy
        self._inhibitor = inhibitor
        self._max_clock_skew_s = max_clock_skew_s
        self._timeout_s = timeout_s
        self._state = store.load()

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Answer one v2 request with one v2 reply; every expected failure is a returned refusal."""

        now = (self.clock.utc().astimezone(timezone.utc), self.clock.monotonic(), self.clock.boot_id())
        if not _v2_valid(request, "request"):  # first: a definite invalid refusal that saves nothing and calls no port
            fields = request if isinstance(request, dict) else {}
            identity = fields.get("identity") if _v2_valid(fields.get("identity"), "identity") else None
            kind, request_id = fields.get("kind"), fields.get("controller_request_id")  # returned as sent: they may nest too deep to copy
            described = {"kind": kind if isinstance(kind, str) else None, "controller_request_id": None,
                         "identity": None if kind == "reserve" else identity}  # a reserve's definite refusal describes no lane
            reply = self._reply(described, now, "invalid", "the request is not valid against contracts/v2/executor.schema.json#/$defs/request")
            return {**reply, "kind": kind, "controller_request_id": request_id, "echoed_identity": copy.deepcopy(identity)}
        kind = request["kind"]
        handler = {"reserve": self._reserve, "beat": self._renew, "ceiling": self._renew, "extend": self._renew, "stop": self._stop,
                   "inspect": self._inspect}.get(kind)
        if (now[0] - _parse_utc(request["sent_at"])).total_seconds() > self._max_clock_skew_s:
            return self._reply(request, now, "clock_skew", f"the request is more than {self._max_clock_skew_s} s old on this host's clock")
        if handler is None or (kind == "inspect" and request["scope"] == "host"):
            return self._reply(request, now, "unavailable", f"this executor does not serve this {kind} request yet")
        lane = request["lane"] if kind == "inspect" and request["scope"] == "lane" else request["identity"]["lane"]
        if lane["host_id"] != self.host_id or lane["lane_id"] not in self._lane_cards:
            return self._reply(request, now, "not_found", "this host does not serve the lane")
        return handler(request, now, _lane_key(lane))

    def enforce_deadlines(self) -> list[dict[str, Any]]:
        """The host timer's one-shot: act on each reserved fence of this boot with a passed deadline."""

        monotonic, boot = self.clock.monotonic(), self.clock.boot_id()
        acted = []
        for key in sorted(self._state["lanes"], key=lambda lane_key: lane_key.split("/")):
            lane = self._state["lanes"][key]
            fence = lane.get("fence")
            due = _v2_due(fence, monotonic) if fence and fence["state"] == "reserved" and fence["boot_id"] == boot else []
            if not due:
                continue
            if fence["execution_policy"]["protected"]:  # a protected lease is quarantined, never probed or stopped
                self._save(key, {**lane, "fence": {**fence, "state": "quarantined"}})
                state = "quarantined"
            else:
                state = "quarantined" if self._release(key, "quarantined")[0] else "free"
            acted.append({"lane": fence["identity"]["lane"], "generation": fence["identity"]["generation"], "due": due, "state": state})
        return copy.deepcopy(acted)

    def reconcile_inhibitors(self) -> dict[str, Any]:
        """After a restart: hold again each unit a fence records held (any state, any boot) that the port's list lacks, and release
        each listed unit no fence records, one call each. One list first; an unknown list holds and releases nothing; nothing is saved."""

        report: dict[str, Any] = {"held": [], "released": [], "failed": [], "error": None}
        if self._inhibitor is None:
            report["error"] = {"code": "unavailable", "message": "this executor has no inhibitor port", "layer": "executor", "cause": None}
            return report
        try:  # error: the list's own error, or why its answer cannot be read
            listed = self._inhibitor.list(timeout_s=self._timeout_s)
            error, units = (listed.get("error"), listed.get("units")) if isinstance(listed, Mapping) else ("its answer is not a mapping", None)
        except Exception as exc:
            error, units = f"it raised {exc!r}", None
        if error is not None or not isinstance(units, list) or not all(isinstance(unit, str) for unit in units):  # unknown, never empty
            report["error"] = copy.deepcopy(dict(error)) if isinstance(error, Mapping) else {
                "code": "inhibitor_failed", "message": f"the inhibitor list is unknown: {error or 'no list of unit names'}"[:1024],
                "layer": "executor", "cause": None}
            return report
        recorded = {}  # each unit a fence records held, named after the fence's own lane and generation -> its identity
        for lane in self._state["lanes"].values():
            fence = lane.get("fence")
            if fence is not None and fence.get("inhibitor_held"):
                who = fence["identity"]
                recorded[f"flightctl-awake-{who['lane']['lane_id']}-g{who['generation']}.service"] = who
        for unit in sorted(set(recorded) - set(units)):  # recorded, not listed: held again
            report["held" if self._inhibit(recorded[unit], hold=True)[0] else "failed"].append(unit)
        for unit in sorted(set(units) - set(recorded)):  # recorded by no fence: released as read back from its name, else failed
            read = re.fullmatch(r"flightctl-awake-(.+)-g([1-9][0-9]{0,99})\.service", unit)  # lane id to the last -g; g07 is unread, never g7
            # (and a generation of more than 100 digits is unread: int() of over 4300 digits raises and would stop the others)
            released = read is not None and self._inhibit({"lane": {"lane_id": read[1]}, "generation": int(read[2])}, hold=False)[0]
            report["released" if released else "failed"].append(unit)
        report["failed"].sort()
        return report

    def _reserve(self, request: Mapping[str, Any], now: tuple[datetime, float, str], key: str) -> dict[str, Any]:
        identity, policy = request["identity"], request["execution_policy"]
        lane = self._state["lanes"].get(key, {})
        fence, highest = lane.get("fence"), lane.get("generation", 0)
        if not any(deadline["kind"] == "max-end" for deadline in request["deadlines"]):
            return self._reply(request, now, "invalid", "a reserve must carry a max-end deadline")
        if policy["work_mode"] == "unit":
            return self._reply(request, now, "unavailable", "unit work is not served yet")
        awake = request["awake"]["hold_inhibitor"]
        if awake and self._inhibitor is None:
            return self._reply(request, now, "unavailable", "this executor has no inhibitor port for the awake inhibitor")
        if fence and fence["identity"] == identity and fence["state"] == "reserved":
            return self._reply(request, now)  # a retried reserve: the fence stays exactly as it was anchored
        if fence:
            return self._reply(request, now, "fenced", f"the lane is fenced ({fence['state']})")
        if identity["generation"] <= highest:
            return self._reply(request, now, "stale_generation", f"generation {identity['generation']} is not above the lane's highest, {highest}")
        if awake:  # D-pow-3: the inhibitor first, then the fence
            held, cause = self._inhibit(identity, hold=True)
            if not held:  # whatever the port did, release once: definite only when that release is confirmed
                return self._reply(request, now, "inhibitor_failed", "the awake inhibitor was not confirmed held",
                                   definite=self._inhibit(identity, hold=False)[0], cause=cause)
        fence = {"identity": copy.deepcopy(identity), "execution_policy": copy.deepcopy(policy), "state": "reserved", "boot_id": now[2],
                 "inhibitor_held": awake, "local_deadlines": [_v2_anchor(deadline, now) for deadline in request["deadlines"]]}
        try:
            self._save(key, {"generation": identity["generation"], "fence": fence})
        except Exception as exc:  # nothing was kept: definite only when no inhibitor was held or its release is confirmed
            released = not awake or self._inhibit(identity, hold=False)[0]
            return self._reply(request, now, "unavailable", f"the fence could not be saved: {exc}"[:1024], definite=released)
        return self._reply(request, now)

    def _renew(self, request: Mapping[str, Any], now: tuple[datetime, float, str], key: str) -> dict[str, Any]:
        """Beat, ceiling and extend renew a kind of the live reserved fence in place; only an approved extend moves max-end later."""

        lane = self._state["lanes"].get(key, {})
        fence, kind = lane.get("fence"), request["kind"]
        if kind == "beat" and any(deadline["kind"] == "max-end" for deadline in request["deadlines"]):
            return self._reply(request, now, "invalid", "a beat never moves max-end; only an approved extend does")
        if kind == "extend" and not request.get("approval_id"):
            return self._reply(request, now, "invalid", "an extend moves max-end later only with an approval_id")
        if not fence:
            return self._reply(request, now, "not_found", "the lane holds no fence")
        if fence["identity"] != request["identity"]:
            return self._reply(request, now, "identity_mismatch", "the identity differs from the reserved one")
        if fence["boot_id"] != now[2]:
            return self._reply(request, now, "reconcile_required", "the fence was reserved before this host's current boot")
        due = _v2_due(fence, now[1])
        if fence["state"] != "reserved" or due:
            return self._reply(request, now, "conflict", f"the fence is {fence['state']}; passed deadlines: {', '.join(due) or 'none'}")
        kinds = {deadline["kind"]: deadline for deadline in fence["local_deadlines"]}
        received = request["deadlines"] if kind == "beat" else [request["max_end"]]
        for new in (_v2_anchor(deadline, now) for deadline in received):
            if kind != "ceiling" or new["monotonic_deadline_s"] < kinds[new["kind"]]["monotonic_deadline_s"]:  # a ceiling only shortens
                kinds[new["kind"]] = new
        self._save(key, {**lane, "fence": {**fence, "local_deadlines": list(kinds.values())}})
        return self._reply(request, now)

    def _stop(self, request: Mapping[str, Any], now: tuple[datetime, float, str], key: str) -> dict[str, Any]:
        fence = self._state["lanes"].get(key, {}).get("fence")
        mode = request["stop_authority"]["mode"]
        if not fence:
            return self._reply(request, now, "not_found", "the lane holds no fence")
        if fence["identity"] != request["identity"]:
            return self._reply(request, now, "identity_mismatch", "the identity differs from the reserved one")
        if fence["execution_policy"]["protected"] and mode not in _V2_PROTECTED_STOP_MODES:
            return self._reply(request, now, "denied", f"a {mode} stop cannot end a lease reserved protected")
        code, observation = self._release(key, "stopping")
        why = "the awake inhibitor release is not confirmed" if code == "inhibitor_failed" else f"the lane is not proven empty ({code})"
        return self._reply(request, now, code, why, definite=code is None, occupancy=observation)

    def _inspect(self, request: Mapping[str, Any], now: tuple[datetime, float, str], key: str) -> dict[str, Any]:
        """Read-only: the lane's fence, its occupancy (one probe while it holds a fence) and the holder-mode unit, which is never started."""

        fence = self._state["lanes"].get(key, {}).get("fence")
        observation = self._probe(fence) if fence else None
        identity = request["identity"] if request["scope"] == "lease" else (fence or {}).get("identity")
        return self._reply(request, now, "conflict" if fence and fence["state"] == "quarantined" else None, "the lane's fence is quarantined",
                           occupancy=observation if isinstance(observation, Mapping) else None, key=key,
                           unit=_v2_absent_unit(identity, now[0].strftime(_V2_UTC)) if identity else None)

    def _release(self, key: str, failed_state: str) -> tuple[str | None, Mapping[str, Any] | None]:
        """Probe the lane once; remove the fence only on an emptiness proof and a confirmed inhibitor release, else keep it in failed_state."""

        lane = self._state["lanes"][key]
        fence = lane["fence"]
        observation = self._probe(fence)
        if not isinstance(observation, Mapping):
            observation, code = None, "probe_unknown"
        elif observation.get("status") != "ok" or "empty" not in observation or "tenants" not in observation:
            code = "probe_unknown"
        else:
            code = None if observation["empty"] is True and observation["tenants"] == [] else "gpu_tenant"
        if code is None and fence.get("inhibitor_held") and not self._inhibit(fence["identity"], hold=False)[0]:
            code = "inhibitor_failed"  # the lane is empty, but the inhibitor may still be held: the fence stays, holding it
        self._save(key, {**lane, "fence": None if code is None else {**fence, "state": failed_state}})
        return code, observation

    def _probe(self, fence: Mapping[str, Any]) -> Any:
        """One occupancy probe of the fence's lane with the fence's tenant thresholds; None when there is no probe or it raised."""

        lane_id = fence["identity"]["lane"]["lane_id"]
        try:
            tenant = fence["execution_policy"]["external_tenant"]
            return None if self._occupancy is None else self._occupancy.occupancy(
                self.host_id, lane_id, list(self._lane_cards[lane_id]), noise_allowlist=tenant["noise_allowlist"],
                noise_cap_mib=tenant["noise_cap_mib"], lane_noise_mib=tenant["lane_noise_mib"], timeout_s=self._timeout_s)
        except Exception:  # a probe that raised proves nothing
            return None

    def _inhibit(self, identity: Mapping[str, Any], *, hold: bool) -> tuple[bool, dict[str, Any] | None]:
        """One Inhibitor call for the lease: (confirmed, the port's typed error). Only a mapping with the wanted held and no error confirms."""

        lane_id, generation = identity["lane"]["lane_id"], identity["generation"]
        try:
            result = (self._inhibitor.hold(lane_id, generation, why=f"flightctl holds this host awake for lease {identity['lease_id']}",
                                           timeout_s=self._timeout_s)
                      if hold else self._inhibitor.release(lane_id, generation, timeout_s=self._timeout_s))
            error = result.get("error") if isinstance(result, Mapping) else None
            confirmed = isinstance(result, Mapping) and result.get("held") is hold and error is None
            return confirmed, dict(error) if isinstance(error, Mapping) else None
        except Exception:  # a port that raised (or is missing), or a result that cannot be read, confirmed nothing
            return False, None

    def _save(self, key: str, lane: Mapping[str, Any]) -> None:
        document = {**self._state, "lanes": {**self._state["lanes"], key: lane}}
        self._store.save(document)
        self._state = document  # only a saved document becomes the executor's state

    def _reply(self, request: Mapping[str, Any], now: tuple[datetime, float, str], code: str | None = None, message: str = "", *,
               definite: bool = True, occupancy: Mapping[str, Any] | None = None, unit: Mapping[str, Any] | None = None,
               cause: Mapping[str, Any] | None = None, key: str | None = None) -> dict[str, Any]:
        """The reply describes one lane: key (a lane-scope inspect's own lane) or else the request identity's."""

        utc, monotonic, boot = now
        identity = request.get("identity")
        fence = self._state["lanes"].get(key or (_lane_key(identity["lane"]) if identity else None), {}).get("fence")
        if code is not None and request["kind"] == "reserve" and fence:
            definite = False  # a reserve refusal is definite only while the lane holds no fence
        remaining, inhibitor = None, None
        if fence and fence["identity"] == identity and fence["boot_id"] == boot:
            max_end = next(deadline["monotonic_deadline_s"] for deadline in fence["local_deadlines"] if deadline["kind"] == "max-end")
            remaining = max(0.0, max_end - monotonic)
        if fence and fence.get("inhibitor_held"):  # named after the fence's own lane and generation
            who = fence["identity"]
            inhibitor = {"held": True, "unit": f"flightctl-awake-{who['lane']['lane_id']}-g{who['generation']}.service", "what": "idle"}
        stamp = utc.strftime(_V2_UTC)
        reply = {
            "schema_version": 2, "kind": request["kind"], "controller_request_id": request["controller_request_id"],
            "echoed_identity": identity, "ok": code is None, "definite": definite,
            "observed_state": fence["state"] if fence else "free" if definite else "unknown",
            "host_boot": {"host_id": self.host_id, "boot_id": boot, "observed_at": stamp}, "host_utc": stamp,
            "fences": [{"identity": fence["identity"], "state": fence["state"], "local_deadlines": fence["local_deadlines"],
                        "inhibitor_held": inhibitor is not None, "rebooted_since_reserve": fence["boot_id"] != boot}] if fence else [],
            "unit": unit, "occupancy": occupancy, "inhibitor": inhibitor, "stage": None, "log_lines": [], "next_cursor": None,
            "error": None if code is None else {"code": code, "message": message, "layer": "executor", "cause": cause},
            "dry_run": False, "output": None, "max_end_remaining_s": remaining,
        }
        if request["kind"] == "session":
            reply["sessions"] = []
        return copy.deepcopy(reply)


def _v2_anchor(deadline: Mapping[str, Any], now: tuple[datetime, float, str]) -> dict[str, Any]:
    """G02: a received duration becomes a deadline on this host's own monotonic clock and boot."""

    return {"kind": deadline["kind"], "boot_id": now[2], "monotonic_deadline_s": now[1] + deadline["in_s"],
            "utc_estimate": (now[0] + timedelta(seconds=deadline["in_s"])).strftime(_V2_UTC)}


def _v2_absent_unit(identity: Mapping[str, Any], observed_at: str) -> dict[str, Any]:
    """A holder-mode lease never starts its unit: an inspect reports it absent (unit.schema.json observation, state absent)."""

    return {"kind": "unit-observation", "unit": identity["unit"], "run_id": identity["run_id"], "invocation_id": None, "state": "absent",
            "load_state": "not-found", "active_state": None, "sub_state": None, "result": None, "main_pid": None, "exit_status": None,
            "cgroup": None, "cgroup_pids": [], "cgroup_empty": True, "observed_at": observed_at, "error": None}


def _v2_due(fence: Mapping[str, Any], monotonic: float) -> list[str]:
    """The fence's passed expiry, max-end and heartbeat-stale deadlines, in that order."""

    return [kind for kind in _V2_DUE if any(d["kind"] == kind and monotonic >= d["monotonic_deadline_s"] for d in fence["local_deadlines"])]


def _v2_valid(value: Any, definition: str) -> bool:
    """Valid against executor.schema.json#/$defs/<definition> as JSON Schema draft 2020-12 evaluates the frozen files, with
    every number finite, every date-time asserted and each pattern's $ matching only at the very end (ECMA-262)."""

    pending = [value]
    while pending:  # NaN and the infinities, which Python's json parses, are not JSON numbers; an integer of any size is finite
        item = pending.pop()
        if isinstance(item, float) and not math.isfinite(item):
            return False
        pending.extend(item.values() if isinstance(item, dict) else item if isinstance(item, list) else ())
    try:
        return _v2_evaluate(value, *_v2_resolve(f"#/$defs/{definition}", _V2_SCHEMA)) is not None
    except (TypeError, RecursionError):  # what JSON cannot carry (a key that is not a string) or nests past Python's limit
        return False


def _v2_resolve(ref: str, base: Path) -> tuple[Any, Path]:
    """The subschema a $ref names, resolved relative to base (the file that holds the $ref), and the file that holds it."""

    name, _, pointer = ref.partition("#")
    path = Path(os.path.normpath(base.parent / name)) if name else base
    if path not in _V2_SCHEMA_FILES:
        _V2_SCHEMA_FILES[path] = json.loads(path.read_text(encoding="utf-8"))
    schema = _V2_SCHEMA_FILES[path]
    for token in pointer.split("/")[1:]:  # a JSON pointer: ~1 is /, ~0 is ~, an array is indexed by number
        token = token.replace("~1", "/").replace("~0", "~")
        schema = schema[int(token)] if isinstance(schema, list) else schema[token]
    return schema, path


def _v2_evaluate(value: Any, schema: Any, base: Path) -> set[str] | None:
    """None when value is invalid against schema (held in the file base), else the names of value's properties that schema
    evaluated: the annotations unevaluatedProperties reads. Only the keywords the frozen files use are judged."""

    if isinstance(schema, bool):
        return set() if schema else None
    seen: set[str] = set()

    def passes(subschema: Any, path: Path = base) -> bool:  # an in-place subschema: what it evaluated counts only when it passes
        found = _v2_evaluate(value, subschema, path)
        seen.update(found or ())
        return found is not None

    if ("$ref" in schema and not passes(*_v2_resolve(schema["$ref"], base)) or not all(passes(sub) for sub in schema.get("allOf", ()))
            or "anyOf" in schema and not any([passes(sub) for sub in schema["anyOf"]])  # a list: every passing branch counts
            or "oneOf" in schema and [passes(sub) for sub in schema["oneOf"]].count(True) != 1
            or "not" in schema and _v2_evaluate(value, schema["not"], base) is not None
            or "if" in schema and not passes(schema.get("then", True) if passes(schema["if"]) else schema.get("else", True))):
        return None
    types = schema.get("type", ())
    if (types and not any(_V2_TYPES[name](value) for name in ([types] if isinstance(types, str) else types))
            or "const" in schema and not _v2_equal(value, schema["const"])
            or "enum" in schema and not any(_v2_equal(value, item) for item in schema["enum"])):
        return None
    if _V2_TYPES["number"](value) and (value < schema.get("minimum", value) or value > schema.get("maximum", value)
                                       or "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]):
        return None
    if isinstance(value, str) and (not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", len(value))
                                   or "pattern" in schema and not re.search(_V2_PATTERN_ANCHOR.sub(
                                       lambda part: r"\Z" if part[1] else part[0], schema["pattern"]), value)  # ECMA-262: $ ends the string
                                   or schema.get("format") == "date-time" and not _v2_date_time(value)):
        return None
    if isinstance(value, list) and (not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", len(value))
                                    or schema.get("uniqueItems") and any(_v2_equal(item, other) for at, item in enumerate(value) for other in value[:at])
                                    or any(_v2_evaluate(item, schema.get("items", True), base) is None for item in value)):
        return None
    if isinstance(value, dict):
        properties, others = schema.get("properties", {}), schema.get("additionalProperties", True)
        if (any(name not in value for name in schema.get("required", ()))
                or any(_v2_evaluate(name, schema.get("propertyNames", True), base) is None for name in value)
                or any(_v2_evaluate(item, properties.get(name, others), base) is None for name, item in value.items())):
            return None
        seen.update(name for name in value if name in properties or "additionalProperties" in schema)
        if any(_v2_evaluate(value[name], schema.get("unevaluatedProperties", True), base) is None for name in value if name not in seen):
            return None
        seen.update(value if "unevaluatedProperties" in schema else ())
    return seen


def _v2_equal(one: Any, other: Any) -> bool:
    """JSON equality (const, enum, uniqueItems): a boolean is never a number, 7 equals 7.0, arrays and objects by content."""

    if isinstance(one, list) and isinstance(other, list):
        return len(one) == len(other) and all(map(_v2_equal, one, other))
    if isinstance(one, dict) and isinstance(other, dict):
        return one.keys() == other.keys() and all(_v2_equal(item, other[key]) for key, item in one.items())
    return (isinstance(one, bool), one) == (isinstance(other, bool), other)


def _v2_date_time(value: str) -> bool:
    """YYYY-MM-DDTHH:MM:SS, an optional fraction, then Z, naming a real UTC date and time (a leap second does not)."""

    match = _V2_DATE_TIME.fullmatch(value)
    if match is None:
        return False
    try:
        datetime(*map(int, match.groups()))
    except ValueError:  # no such year, month, day, hour, minute or second
        return False
    return True
