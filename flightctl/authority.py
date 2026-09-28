"""The sole transactional Flightctl authority boundary.

This module owns policy, identity binding, durable mutation ordering, and the
executor call trace.  It is deliberately transport-neutral: the injected
transport is the only object used for executor calls and the injected clock is
the only source of time used for policy decisions.
"""

from __future__ import annotations

import copy
import hashlib
import json
import secrets
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping

from .auth import (
    APPROVAL_SIGNED_FIELDS,
    AuthError,
    ApprovalVerifier,
    AuthenticatedPeer,
    PeerAuthenticationError,
    PeerAuthenticator,
    approval_digest,
    canonical_bytes,
)
from .store import SQLiteStore, StoreError, StoreUnavailable, utc_text


CLASSES = ("operator", "booked", "batch", "service", "resident", "standby")
CLASS_RANK = {name: index for index, name in enumerate(CLASSES)}
PROTECTED_CLASSES = {"operator", "booked", "batch"}
ACTIVE_LEASE_STATES = {"starting", "running", "stopping", "quarantined"}
ACTIVE_QUEUE_STATES = {"queued", "eligible"}


class AuthorityError(RuntimeError):
    """Base class for controller failures."""


class _Reject(Exception):
    def __init__(self, status: int, code: str, message: str, *, retryable: bool = False, failure_class: str = "policy", details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable
        self.failure_class = failure_class
        self.details = dict(details or {})


class RealClock:
    def utc(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        import time

        return time.monotonic()

    def boot_id(self) -> str:
        # The controller does not need a host-specific boot identifier to be
        # useful in tests; a process identifier still prevents accidental
        # reuse across real process restarts.
        import os

        return f"process-{os.getpid()}"


def _parse_time(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise _Reject(403, "invalid", "invalid UTC timestamp", failure_class="client") from exc
    if result.tzinfo is None or result.utcoffset() != timezone.utc.utcoffset(result):
        raise _Reject(403, "invalid", "timestamp must be UTC", failure_class="client")
    return result.astimezone(timezone.utc)


def _time_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _principal_copy(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value.get(key) for key in ("site_id", "tenant_id", "issuer", "subject")}


def _same_principal(left: Mapping[str, Any] | None, right: Mapping[str, Any] | None) -> bool:
    return isinstance(left, Mapping) and isinstance(right, Mapping) and _principal_copy(left) == _principal_copy(right)


def _lane_ref(site_id: str, host_id: str, lane_id: str) -> dict[str, str]:
    return {"site_id": site_id, "host_id": host_id, "lane_id": lane_id}


def _request_fingerprint_value(op: str, lane: str | None, args: Mapping[str, Any], principal: Mapping[str, Any] | None = None, admission: Mapping[str, Any] | None = None, idempotency_scope: Mapping[str, Any] | None = None) -> str:
    payload: dict[str, Any] = {"op": op, "lane": lane, "args": copy.deepcopy(dict(args))}
    if principal is not None:
        payload["principal"] = _principal_copy(principal)
    if admission is not None:
        payload["admission"] = copy.deepcopy(dict(admission))
    if idempotency_scope is not None:
        payload["idempotency_scope"] = copy.deepcopy(dict(idempotency_scope))
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def request_fingerprint(request_or_op: Mapping[str, Any] | str, lane: str | None = None, args: Mapping[str, Any] | None = None, principal: Mapping[str, Any] | None = None) -> str:
    """Compute the server-side request fingerprint used by idempotency."""

    if isinstance(request_or_op, Mapping):
        request = request_or_op
        return _request_fingerprint_value(str(request.get("op")), request.get("lane"), request.get("args", {}), principal, request.get("admission"), request.get("idempotency_scope"))
    return _request_fingerprint_value(request_or_op, lane, args or {}, principal)


fingerprint_for = request_fingerprint


@dataclass(frozen=True)
class _Context:
    peer: AuthenticatedPeer
    principal: dict[str, Any]
    roles: frozenset[str]
    quota_key: str


class Authority:
    """Authenticated, durable RPC authority.

    The constructor accepts neutral inventory/policy structures from the v1
    examples.  Existing records are never overwritten on restart; only absent
    lane definitions are bootstrapped.
    """

    def __init__(
        self,
        store: SQLiteStore | str = ":memory:",
        transport: Any | None = None,
        clock: Any | None = None,
        *,
        inventory: Mapping[str, Any] | None = None,
        policy: Mapping[str, Any] | None = None,
        pipelines: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None = None,
        identity_mapping: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]] | None = None,
        whois: Callable[[str], Mapping[str, Any]] | Any | None = None,
        peer_authenticator: PeerAuthenticator | None = None,
        approval_verifier: ApprovalVerifier | None = None,
        approval_keys: Mapping[str, Any] | None = None,
        site_id: str | None = None,
        controller_id: str | None = None,
        lanes: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self.store = store if isinstance(store, SQLiteStore) else SQLiteStore(store, site_id=site_id or "site-a", controller_id=controller_id or "controller-a")
        self.transport = transport
        self.clock = clock or RealClock()
        self.inventory = dict(inventory or {})
        self.site_id = str(site_id or self.inventory.get("site_id") or getattr(self.store, "site_id", "site-a"))
        self.controller_id = str(controller_id or (self.inventory.get("controller") or {}).get("controller_id") or getattr(self.store, "controller_id", "controller-a"))
        self.policy = self._normalise_policy(policy or {})
        self.pipelines = self._normalise_pipelines(pipelines if pipelines is not None else self.inventory.get("pipelines"))
        mapping = identity_mapping if identity_mapping is not None else self.inventory.get("identity_mapping", ())
        self.peer_authenticator = peer_authenticator or PeerAuthenticator(mapping, whois=whois)
        self.approval_verifier = approval_verifier or ApprovalVerifier(approval_keys or {})
        self._mutex = threading.RLock()
        self._bootstrap_lanes(lanes if lanes is not None else self.inventory.get("lanes"))
        if self.store.available:
            try:
                self.store.recover_pending_reservations()
            except StoreError:
                pass

    # ---- setup and clock -------------------------------------------------

    @staticmethod
    def _normalise_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
        admission = {
            "max_clock_skew_s": 30,
            "heartbeat_s": 60,
            "queue_refresh_s": 60,
            "queue_expiry_s": 600,
            "booking_horizon_s": 1209600,
            "minimum_booking_s": 900,
            "agent_max_s": 14400,
            "operator_max_s": 43200,
        }
        admission.update(dict(policy.get("admission", {})))
        return {
            "schema_version": 1,
            "policy_id": policy.get("policy_id", "default"),
            "revision": int(policy.get("revision", 1)),
            "updated_at": policy.get("updated_at"),
            "purpose_rules": list(policy.get("purpose_rules", ["purpose-required"])),
            "content_rules": list(policy.get("content_rules", [])),
            "admission": admission,
            "quotas": dict(policy.get("quotas", policy.get("quota_limits", {})) or {}),
            "security_hooks": dict(policy.get("security_hooks", {"supported_schemes": ["ssh-sk"], "unsupported_supplied_action": "deny"})),
        }

    @staticmethod
    def _normalise_pipelines(pipelines: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None) -> dict[str, dict[str, Any]]:
        if pipelines is None:
            return {}
        if isinstance(pipelines, Mapping):
            if "pipelines" in pipelines and isinstance(pipelines["pipelines"], list):
                items = pipelines["pipelines"]
            else:
                items = [dict(value, pipeline_id=key) if isinstance(value, Mapping) and "pipeline_id" not in value else value for key, value in pipelines.items()]
        else:
            items = list(pipelines)
        return {str(item["pipeline_id"]): dict(item) for item in items if isinstance(item, Mapping) and item.get("pipeline_id")}

    def _bootstrap_lanes(self, lanes: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]] | None) -> None:
        if not self.store.available:
            return
        if lanes is None:
            return
        items = list(lanes.values()) if isinstance(lanes, Mapping) else list(lanes)
        hosts = {str(item.get("host_id")): item for item in self.inventory.get("hosts", []) if isinstance(item, Mapping)}
        with self.store.transaction() as connection:
            for item in items:
                if not isinstance(item, Mapping):
                    continue
                lane_id = str(item.get("lane_id"))
                if not lane_id or self.store.get_lane(lane_id, connection=connection) is not None:
                    continue
                host_id = str(item.get("host_id", (item.get("lane") or {}).get("host_id", "host-1")))
                host = hosts.get(host_id, {})
                lane_record = {
                    "lane_id": lane_id,
                    "lane": _lane_ref(self.site_id, host_id, lane_id),
                    "host_id": host_id,
                    "endpoint": item.get("endpoint") or host.get("ssh_endpoint") or host_id,
                    "state": str(item.get("state", "free")),
                    "generation": int(item.get("generation", 0)),
                    "reachability": item.get("reachability", host.get("reachability", "unknown")),
                    "enabled": bool(item.get("enabled", True)),
                    "quota_key": item.get("quota_key", host_id),
                    "policy": copy.deepcopy(item.get("policy", {})),
                    "updated_at": _time_text(self._utc()),
                }
                self.store.put_lane(lane_record, connection=connection)

    def _utc(self) -> datetime:
        value = self.clock.utc() if hasattr(self.clock, "utc") else datetime.now(timezone.utc)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _monotonic(self) -> float:
        return float(self.clock.monotonic()) if hasattr(self.clock, "monotonic") else 0.0

    def _boot_id(self) -> str:
        return str(self.clock.boot_id()) if hasattr(self.clock, "boot_id") else "boot-unknown"

    def _deadline(self, seconds: int | float) -> dict[str, Any]:
        return {"boot_id": self._boot_id(), "deadline_s": self._monotonic() + float(seconds), "utc_anchor": _time_text(self._utc()), "monotonic_anchor_s": self._monotonic()}

    def _health_tx(self, connection: sqlite3.Connection) -> None:
        now = self._utc()
        mono = self._monotonic()
        boot = self._boot_id()
        last_utc = self.store.get_meta("last_utc", connection=connection)
        last_mono = self.store.get_meta("last_monotonic", connection=connection)
        last_boot = self.store.get_meta("last_boot_id", connection=connection)
        unhealthy = self.store.recovery_required()
        reason = self.store.get_meta("health_reason", connection=connection)
        if last_utc is not None and last_mono is not None:
            previous = _parse_time(last_utc)
            delta_utc = (now - previous).total_seconds()
            delta_mono = mono - float(last_mono)
            if last_boot != boot or abs(delta_utc - delta_mono) > float(self.policy["admission"]["max_clock_skew_s"]):
                unhealthy = True
                reason = "clock skew or reboot requires reconciliation"
        self.store.set_meta("last_utc", _time_text(now), connection=connection)
        self.store.set_meta("last_monotonic", repr(mono), connection=connection)
        self.store.set_meta("last_boot_id", boot, connection=connection)
        self._expire_bookings_tx(connection, now)
        if unhealthy:
            self.store.set_meta("recovery_required", "1", connection=connection)
            self.store.set_meta("health_reason", reason or "state requires reconciliation", connection=connection)
            self._quarantine_active_tx(connection, reason or "state requires reconciliation")
            # The freeze and exclusion are the recovery record.  Commit them
            # before returning the typed rejection; otherwise the transaction
            # wrapper would roll back the very evidence that must survive a
            # rejected request and a restored wall clock.
            if connection.in_transaction:
                connection.commit()
            raise _Reject(503, "unknown", reason or "controller state requires reconciliation", retryable=True, failure_class="state")

    def _expire_bookings_tx(self, connection: sqlite3.Connection, now: datetime) -> None:
        """Advance calendar state without treating expiry as hardware cleanup."""

        for booking in self.store.all_bookings(connection=connection):
            state = booking.get("state")
            start = _parse_time(booking["start"])
            end = _parse_time(booking["end"])
            changed = False
            if state in {"scheduled", "blocked", "claimed"} and now >= end - timedelta(minutes=10) and self.store.get_meta(f"booking-warning:{booking['booking_id']}", connection=connection) is None:
                self.store.set_meta(f"booking-warning:{booking['booking_id']}", _time_text(now), connection=connection)
                self.store.put_event(
                    self._controller_event(
                        kind="book",
                        state="recovery",
                        request_id=f"booking-warning-{booking['booking_id']}",
                        lane=booking.get("lane"),
                        generation=booking.get("reservation", {}).get("generation"),
                        reason="booking completion warning; finish by booking end",
                        data={"booking_id": booking["booking_id"]},
                    ),
                    connection=connection,
                )
            if state == "scheduled" and now > start + timedelta(minutes=15):
                booking["state"] = "missed"
                booking["recovery"] = {"state": "no-show-reopened", "at": _time_text(now), "reason": "booking had no check-in by start plus fifteen minutes"}
                changed = True
            elif state == "blocked" and now >= end:
                booking["state"] = "missed"
                booking["recovery"] = {"state": "overrun-delayed", "at": _time_text(now), "reason": "booking remained blocked through its end"}
                changed = True
            elif state == "claimed" and now >= end:
                booking["state"] = "completed"
                changed = True
            if changed:
                booking["revision"] = int(booking.get("revision", 1)) + 1
                self.store.put_booking(booking, connection=connection)
                self.store.put_event(
                    self._controller_event(
                        kind="book",
                        state="recovery",
                        request_id=f"booking-recovery-{booking['booking_id']}-{booking['revision']}",
                        lane=booking.get("lane"),
                        generation=booking.get("reservation", {}).get("generation"),
                        reason="booking recovery state advanced",
                        data={"booking_id": booking["booking_id"]},
                    ),
                    connection=connection,
                )

    def _quarantine_active_tx(self, connection: sqlite3.Connection, reason: str) -> None:
        rows = connection.execute("SELECT lease_id,record_json FROM leases WHERE state IN ('starting','running','stopping')").fetchall()
        for row in rows:
            record = json.loads(row[1])
            record["state"] = "quarantined"
            record["reservation"] = dict(record.get("reservation", {}), state="quarantined")
            connection.execute("UPDATE leases SET state='quarantined',reservation_status='uncertain',record_json=?,updated_at=? WHERE lease_id=?", (json.dumps(record, sort_keys=True, separators=(",", ":")), utc_text(), row[0]))
            lane = self.store.get_lane(record["lane"]["lane_id"], connection=connection)
            if lane:
                lane["state"] = "quarantined"
                lane["uncertainty_reason"] = reason
                self.store.put_lane(lane, connection=connection)

    # ---- wire helpers ----------------------------------------------------

    @staticmethod
    def _error(code: str, message: str, *, retryable: bool, failure_class: str, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {"code": code, "message": message, "retryable": retryable, "failure_class": failure_class}
        if details:
            result["details"] = dict(details)
        return result

    def _response(self, request_id: str, status: int, data: Mapping[str, Any] | None = None, error: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return {"schema": 1, "request_id": request_id, "status": status, "data": copy.deepcopy(dict(data)) if data is not None else None, "error": copy.deepcopy(dict(error)) if error is not None else None}

    def _reject_response(self, request_id: str, reject: _Reject) -> dict[str, Any]:
        return self._response(request_id, reject.status, error=self._error(reject.code, reject.message, retryable=reject.retryable, failure_class=reject.failure_class, details=reject.details))

    def _mutation(self, op: str, record_type: str, record_id: str, state: str, lane: Mapping[str, Any] | None, generation: int | None) -> dict[str, Any]:
        return {"kind": "mutation", "operation": op, "record_type": record_type, "record_id": record_id, "state": state, "revision": 1, "reservation": {"lane": dict(lane) if lane else None, "generation": generation, "state": "unassigned" if generation is None else state}}

    def _event(self, *, kind: str, state: str, request_id: str, context: _Context, lane: Mapping[str, Any] | None, generation: int | None, reason: str, data: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "event_id": f"event-{uuid.uuid4().hex}",
            "occurred_at": _time_text(self._utc()),
            "kind": kind,
            "state": state,
            "request_id": request_id,
            "job_id": None,
            "actor": _principal_copy(context.principal),
            "subject": None,
            "site_id": self.site_id,
            "controller_id": self.controller_id,
            "correlation_id": request_id,
            "lane": dict(lane) if lane else None,
            "generation": generation,
            "reason": reason,
            "data": dict(data or {}),
        }

    def _controller_event(self, *, kind: str, state: str, request_id: str, lane: Mapping[str, Any] | None, generation: int | None, reason: str, data: Mapping[str, Any] | None = None) -> dict[str, Any]:
        actor = {"site_id": self.site_id, "tenant_id": "controller", "issuer": self.controller_id, "subject": self.controller_id}
        return {
            "schema_version": 1,
            "event_id": f"event-{uuid.uuid4().hex}",
            "occurred_at": _time_text(self._utc()),
            "kind": kind,
            "state": state,
            "request_id": request_id,
            "job_id": None,
            "actor": actor,
            "subject": None,
            "site_id": self.site_id,
            "controller_id": self.controller_id,
            "correlation_id": request_id,
            "lane": dict(lane) if lane else None,
            "generation": generation,
            "reason": reason,
            "data": dict(data or {}),
        }

    def _remember(self, connection: sqlite3.Connection, request: Mapping[str, Any], context: _Context, fingerprint: str, response: Mapping[str, Any]) -> None:
        self.store.finalize_idempotency(str(request["request_id"]), self.store.principal_key(context.principal), fingerprint, int(response["status"]), response, scope=self._idempotency_scope(request), connection=connection, stored_at=_time_text(self._utc()))

    def _idempotency_scope(self, request: Mapping[str, Any], *, in_progress: bool = False) -> dict[str, Any]:
        supplied = request.get("idempotency_scope")
        if supplied is None:
            scope: dict[str, Any] = {"scope": "authenticated-principal", "controller_id": self.controller_id}
        elif not isinstance(supplied, Mapping):
            raise _Reject(403, "invalid", "idempotency scope is invalid", failure_class="client")
        else:
            scope = copy.deepcopy(dict(supplied))
        if set(scope) != {"scope", "controller_id"} or scope.get("scope") not in {"authenticated-principal", "controller"} or scope.get("controller_id") != self.controller_id:
            raise _Reject(403, "invalid", "idempotency scope does not name this controller", failure_class="client")
        if in_progress:
            scope["in_progress"] = True
        return scope

    def _existing(self, connection: sqlite3.Connection, request: Mapping[str, Any], context: _Context, fingerprint: str) -> dict[str, Any] | None:
        row = self.store.get_idempotency(str(request["request_id"]), connection=connection)
        if row is None:
            return None
        if row["principal_key"] != self.store.principal_key(context.principal) or row["request_fingerprint"] != fingerprint:
            raise _Reject(409, "conflict", "request ID arguments or principal changed", failure_class="conflict")
        stored_scope = dict(row.get("idempotency_scope") or {})
        stored_scope.pop("in_progress", None)
        if stored_scope != self._idempotency_scope(request):
            raise _Reject(409, "conflict", "request idempotency scope changed", failure_class="conflict")
        if isinstance(row.get("idempotency_scope"), Mapping) and row["idempotency_scope"].get("in_progress"):
            raise _Reject(409, "conflict", "request is already in progress", retryable=True, failure_class="conflict")
        return dict(row["response"])

    def _claim_request(self, connection: sqlite3.Connection, request: Mapping[str, Any], context: _Context, fingerprint: str) -> None:
        marker = self._response(
            str(request["request_id"]),
            409,
            error=self._error("conflict", "request is already in progress", retryable=True, failure_class="conflict"),
        )
        claimed = self.store.claim_idempotency(
            str(request["request_id"]),
            self.store.principal_key(context.principal),
            self._idempotency_scope(request, in_progress=True),
            fingerprint,
            marker,
            connection=connection,
            stored_at=_time_text(self._utc()),
        )
        if not claimed:
            row = self.store.get_idempotency(str(request["request_id"]), connection=connection)
            if row is None or row["principal_key"] != self.store.principal_key(context.principal) or row["request_fingerprint"] != fingerprint:
                raise _Reject(409, "conflict", "request ID arguments or principal changed", failure_class="conflict")
            raise _Reject(409, "conflict", "request is already in progress", retryable=True, failure_class="conflict")

    # ---- authentication and admission ----------------------------------

    def _authenticate(self, actual_peer: str | None) -> _Context:
        if actual_peer is None:
            raise _Reject(403, "denied", "authenticated socket peer is required", failure_class="policy")
        try:
            peer = self.peer_authenticator.authenticate(actual_peer)
        except PeerAuthenticationError as exc:
            raise _Reject(403, "denied", str(exc), failure_class="policy") from exc
        quota_key = peer.device_id or peer.external_id
        return _Context(peer=peer, principal=_principal_copy(peer.principal), roles=frozenset(peer.roles), quota_key=quota_key)

    def _effective_class(self, args: Mapping[str, Any], context: _Context, *, booking: Mapping[str, Any] | None = None) -> str:
        requested = str(args.get("class", "batch"))
        if requested not in CLASSES:
            raise _Reject(403, "denied", "unsupported class", failure_class="policy")
        roles = set(context.roles)
        if requested == "operator" and not ({"operator", "john"} & roles):
            raise _Reject(403, "denied", "operator class is not derived from this peer", failure_class="policy")
        if requested == "booked":
            if booking is None or not _same_principal(booking.get("principal"), context.principal):
                raise _Reject(403, "denied", "booked class requires a matching booking", failure_class="policy")
        allowed = set(roles) & set(CLASSES)
        configured = getattr(context.peer, "roles", ())
        if requested in {"service", "resident", "standby"} and allowed and requested not in allowed and "operator" not in roles:
            raise _Reject(403, "denied", "class is not configured for this principal", failure_class="policy")
        return requested

    def _pipeline_admission(self, request: Mapping[str, Any], args: Mapping[str, Any], context: _Context, *, action: str | None = None, lane: Mapping[str, Any] | None = None, generation: int | None = None, booking_id: str | None = None, max_s: int | None = None, max_end: datetime | None = None) -> tuple[dict[str, Any] | None, str | None]:
        pipeline_ref = args.get("pipeline_ref")
        manifest = args.get("signed_manifest")
        if pipeline_ref is None and manifest is None:
            return None, None
        if manifest is not None:
            if not isinstance(manifest, Mapping):
                raise _Reject(403, "denied", "unsupported manifest", failure_class="policy")
            if manifest.get("destination_site") != self.site_id or not _same_principal(manifest.get("principal"), context.principal):
                raise _Reject(403, "denied", "manifest identity or destination mismatch", failure_class="policy")
            if _parse_time(str(manifest.get("expires"))) <= self._utc():
                raise _Reject(403, "denied", "manifest expired", failure_class="policy")
            proof = manifest.get("proof")
            if not isinstance(proof, Mapping) or proof.get("scheme") not in set(self.policy["security_hooks"].get("supported_schemes", ())):
                raise _Reject(403, "denied", "unsupported supplied security hook", failure_class="policy")
            signed_manifest = {key: value for key, value in manifest.items() if key not in {"proof", "canonical_payload_hash"}}
            manifest_digest = hashlib.sha256(canonical_bytes(signed_manifest)).digest()
            if manifest.get("canonical_payload_hash") != hashlib.sha256(canonical_bytes(signed_manifest)).hexdigest():
                raise _Reject(403, "denied", "manifest payload hash mismatch", failure_class="policy")
            try:
                self.approval_verifier.verify(proof, manifest_digest, evidence=None, now=self._utc(), require_evidence=False)
            except AuthError as exc:
                raise _Reject(403, "denied", "manifest proof verification failed", failure_class="policy") from exc
            if pipeline_ref is None:
                pipeline_ref = manifest.get("pipeline_id")
        pipeline = self.pipelines.get(str(pipeline_ref)) if pipeline_ref is not None else None
        if pipeline is None:
            raise _Reject(403, "denied", "pipeline is unavailable", failure_class="policy")
        admission = request.get("admission") if isinstance(request.get("admission"), Mapping) else {}
        supplied_binding = admission.get("pipeline") if isinstance(admission, Mapping) else None
        if pipeline_ref is not None and not isinstance(supplied_binding, Mapping):
            raise _Reject(403, "stale_policy", "current pipeline policy binding is required", failure_class="policy")
        if supplied_binding is not None and (supplied_binding.get("pipeline_id") != pipeline.get("pipeline_id") or supplied_binding.get("revision") != pipeline.get("revision") or supplied_binding.get("policy_hash") != pipeline.get("policy_hash")):
            raise _Reject(403, "stale_policy", "pipeline policy binding is stale", failure_class="policy")
        destination = self.site_id
        availability = pipeline.get("availability")
        if destination in pipeline.get("partner_overrides", {}):
            availability = pipeline["partner_overrides"][destination]
        if availability == "unavailable":
            raise _Reject(403, "denied", "pipeline is unavailable", failure_class="policy")
        purpose = str(args.get("purpose", ""))
        if purpose != str(pipeline.get("purpose", "")):
            raise _Reject(403, "denied", "purpose is not current pipeline purpose", failure_class="policy")
        if manifest is not None and manifest.get("policy_hash") != pipeline.get("policy_hash"):
            raise _Reject(403, "stale_policy", "manifest policy is stale", failure_class="policy")
        approval_id = None
        selected = admission.get("approval") if isinstance(admission, Mapping) else None
        if isinstance(selected, Mapping) and selected.get("required"):
            approval_id = selected.get("approval_id")
        if availability == "approval_required":
            if not approval_id:
                raise _Reject(403, "denied", "approval required", failure_class="policy")
            return pipeline, str(approval_id)
        if approval_id:
            raise _Reject(403, "denied", "unexpected approval selection", failure_class="policy")
        return pipeline, None

    def _validate_delegation(self, request: Mapping[str, Any], context: _Context, op: str, pipeline_id: str | None) -> None:
        admission = request.get("admission")
        delegation = admission.get("delegation") if isinstance(admission, Mapping) else None
        if delegation is None:
            return
        if not isinstance(delegation, Mapping):
            raise _Reject(403, "denied", "invalid delegation", failure_class="policy")
        if not _same_principal(delegation.get("delegate"), context.principal) or delegation.get("destination_site") != self.site_id or self.controller_id != delegation.get("audience"):
            raise _Reject(403, "denied", "delegation audience or principal mismatch", failure_class="policy")
        if op not in set(delegation.get("allowed_operations", ())):
            raise _Reject(403, "denied", "delegation does not allow operation", failure_class="policy")
        if pipeline_id is not None and pipeline_id not in set(delegation.get("allowed_pipelines", ())):
            raise _Reject(403, "denied", "delegation does not allow pipeline", failure_class="policy")
        if _parse_time(str(delegation.get("expires"))) <= self._utc() or int(delegation.get("max_depth", 0)) < 0:
            raise _Reject(403, "denied", "delegation expired or invalid", failure_class="policy")
        binding = delegation.get("binding")
        if not isinstance(binding, Mapping) or binding.get("audience") != delegation.get("audience") or binding.get("nonce") != delegation.get("nonce"):
            raise _Reject(403, "denied", "delegation binding mismatch", failure_class="policy")
        proof = binding.get("proof")
        if not isinstance(proof, Mapping):
            raise _Reject(403, "denied", "supplied delegation has no verifiable proof", failure_class="policy")
        signed_binding = {key: value for key, value in binding.items() if key != "proof"}
        try:
            self.approval_verifier.verify(proof, hashlib.sha256(canonical_bytes(signed_binding)).digest(), evidence=None, now=self._utc(), require_evidence=False)
        except AuthError as exc:
            raise _Reject(403, "denied", "delegation proof verification failed", failure_class="policy") from exc

    def _validate_batch(self, batch: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        arms = list(batch.get("arms", ()))
        if not arms or not batch.get("registered_before_execution") or not batch.get("all_arms_visible"):
            raise _Reject(403, "denied", "batch registration is incomplete", failure_class="policy")
        ids = [str(item.get("arm_id")) for item in arms]
        if len(ids) != len(set(ids)):
            raise _Reject(403, "denied", "batch contains duplicate arms", failure_class="policy")
        known = set(ids)
        edges: dict[str, set[str]] = {item: set() for item in ids}
        for item in arms:
            deps = set(item.get("dependencies", ()))
            if item.get("predecessor") is not None:
                deps.add(item["predecessor"])
            if not deps.issubset(known):
                raise _Reject(403, "denied", "batch contains dangling dependency", failure_class="policy")
            edges[str(item["arm_id"])] = {str(dep) for dep in deps}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise _Reject(403, "denied", "batch dependency cycle", failure_class="policy")
            if node in visited:
                return
            visiting.add(node)
            for dep in edges[node]:
                visit(dep)
            visiting.remove(node)
            visited.add(node)

        for node in ids:
            visit(node)
        return arms

    def _validate_args_shape(self, op: str, args: Mapping[str, Any]) -> None:
        allowed: dict[str, set[str]] = {
            "acquire": {"purpose", "class", "est_s", "max_s", "booking_id", "queue_id", "pipeline_ref", "signed_manifest"},
            "renew": {"token", "instance", "extend_s"},
            "release": {"token", "instance", "extend_s"},
            "claim": {"token", "generation", "generation_source", "instance", "booking_id", "revision"},
            "book": {"start", "end", "purpose"},
            "cancel": {"booking_id", "revision"},
            "approval-request": {"action", "booking_id", "revision", "target_generation", "bounds", "reason", "destination_site", "controller_id", "payload_hash", "manifest_hash", "policy_hash"},
            "approve": {"approval_id", "proof", "evidence"},
            "preempt": {"token", "approval_id", "instance"},
            "chat-load": {"pipeline_ref", "purpose"},
            "chat-unload": {"occupant_id", "generation"},
            "cal": {"at"},
            "free": {"at"},
            "report": {"at"},
            "status": {"at"},
            "queue": {"action", "purpose", "class", "max_wait_s", "queue_id"},
        }
        unexpected = set(args) - allowed.get(op, set())
        if unexpected:
            raise _Reject(403, "denied", "unsupported caller-supplied authority field", failure_class="policy", details={"fields": sorted(unexpected)})

    # ---- executor call and identity fencing -----------------------------

    def _executor_call(self, lane: Mapping[str, Any], lease: Mapping[str, Any], kind: str, *, stop_authority: Mapping[str, Any] | None = None) -> tuple[bool, Mapping[str, Any] | None, str]:
        if self.transport is None:
            return False, None, "executor transport unavailable"
        execution_policy = {
            "class": lease["class"],
            "protected": lease["class"] in PROTECTED_CLASSES,
            "preemptible": lease["class"] not in PROTECTED_CLASSES,
            "grace_s": {"service": 120, "resident": 120, "standby": 300}.get(str(lease["class"]), 0) if kind == "stop" else 0,
            "max_end": lease["max_end"],
            "deadline_kind": "max-end",
        }
        identity = {
            "lane": dict(lease["lane"]),
            "generation": int(lease["generation"]),
            "token": lease["token"],
            "instance": lease["instance"],
            "unit": lease.get("unit"),
            "invocation": lease.get("invocation"),
            "deadline": {
                "kind": "max-end",
                "owner_class": lease["class"],
                "boot_id": lease["deadline"]["boot_id"],
                "deadline_s": lease["deadline"]["deadline_s"],
                "utc_anchor": lease["deadline"]["utc_anchor"],
                "monotonic_anchor_s": lease["deadline"]["monotonic_anchor_s"],
            },
        }
        message: dict[str, Any] = {"schema_version": 1, "kind": kind, "controller_request_id": f"executor-{uuid.uuid4().hex}", "execution_policy": execution_policy, "identity": identity}
        if kind == "stop":
            message["stop_authority"] = stop_authority or {"mode": "controller-match", "approval_id": None}
        endpoint = str(lane.get("endpoint") or lane.get("host_id"))
        try:
            raw = self.transport.request(endpoint, message, 30.0)
        except Exception:
            return False, None, "executor transport failure"
        if not isinstance(raw, Mapping) or raw.get("status") != "ok":
            return False, None, "executor outcome is not a confirmed success"
        reply = raw.get("response")
        if not isinstance(reply, Mapping):
            # A direct typed executor reply is also accepted, but a bare
            # {status: ok} is intentionally not success.
            reply = raw if raw.get("kind") is not None else None
        if reply is None or reply.get("kind") != kind or reply.get("ok") is not True or reply.get("uncertain") is not False:
            return False, None, "executor reply is not a confirmed typed success"
        if reply.get("error") is not None:
            return False, None, "executor success contained an error"
        expected_ack = {"reserve": "reserved", "start": "started", "beat": "beat", "stop": "stopped", "inspect": "inspected"}.get(kind)
        if reply.get("acknowledgement") != expected_ack:
            return False, None, "executor acknowledgement mismatch"
        echoed = reply.get("echoed_identity")
        if not isinstance(echoed, Mapping):
            return False, None, "executor reply omitted echoed identity"
        for field in ("lane", "generation", "token", "instance", "unit", "invocation", "deadline"):
            if field not in echoed or echoed.get(field) != identity.get(field):
                return False, None, f"executor identity mismatch: {field}"
        if not isinstance(reply.get("cgroup_occupants"), list) or not isinstance(reply.get("gpu_tenants"), list):
            return False, None, "executor reply omitted occupancy observations"
        if kind == "reserve":
            if reply.get("observed_state") not in {"starting", "running"} or reply.get("cgroup_occupants") != [] or reply.get("gpu_tenants") != []:
                return False, None, "executor reserve did not prove a non-quarantined empty reservation"
        if kind == "stop":
            if reply.get("observed_state") != "free" or reply.get("cgroup_occupants") != [] or reply.get("gpu_tenants") != []:
                return False, None, "executor stop did not prove empty hardware"
        return True, reply, "ok"

    # ---- acquire and lease operations ----------------------------------

    def _queue_active(self, connection: sqlite3.Connection, lane_id: str) -> list[dict[str, Any]]:
        now = self._monotonic()
        entries = self.store.all_queue(lane_id=lane_id, connection=connection)
        active: list[dict[str, Any]] = []
        by_id = {str(entry["queue_id"]): entry for entry in entries}
        for entry in entries:
            if entry.get("state") in ACTIVE_QUEUE_STATES and now >= float(entry["wait_deadline"]["deadline_s"]):
                entry["state"] = "expired"
                entry["eligible"] = False
                self.store.put_queue(entry, connection=connection)
                self.store.put_event(
                    self._controller_event(
                        kind="queue",
                        state="expired",
                        request_id=f"queue-expiry-{entry['queue_id']}",
                        lane=entry.get("lane"),
                        generation=entry.get("reservation", {}).get("generation"),
                        reason="queue wait deadline expired",
                        data={"queue_id": entry["queue_id"]},
                    ),
                    connection=connection,
                )
                continue
            if entry.get("state") in ACTIVE_QUEUE_STATES:
                dependencies = self.store.queue_dependencies(str(entry["queue_id"]), connection=connection)
                if not dependencies and entry.get("predecessor") is not None:
                    dependencies = [str(entry["predecessor"])]
                dependency_records = [by_id.get(item) for item in dependencies]
                all_complete = bool(dependencies) and all(item is not None and item.get("state") == "claimed" for item in dependency_records)
                if dependencies and not all_complete:
                    entry["eligible"] = False
                    self.store.put_queue(entry, connection=connection)
                elif all_complete:
                    # A queue entry exposes only one predecessor on the
                    # frozen wire record; the private dependency table above
                    # keeps every edge authoritative before that projection is
                    # cleared.
                    entry["predecessor"] = None
                    entry["eligible"] = True
                    self.store.put_queue(entry, connection=connection)
                elif entry.get("predecessor") is None and entry.get("eligible") is not True:
                    entry["eligible"] = True
                    self.store.put_queue(entry, connection=connection)
                active.append(entry)
        return active

    def _queue_eligible(self, active: list[dict[str, Any]], queue_id: str | None, requested_class: str, purpose: str, context: _Context) -> dict[str, Any] | None:
        if not active:
            if queue_id:
                return None
            return None
        ordered = sorted((item for item in active if item.get("eligible") is True and item.get("state") in ACTIVE_QUEUE_STATES), key=lambda item: (CLASS_RANK.get(str(item.get("class")), 99), int(item.get("sequence", 0))))
        if queue_id is None:
            raise _Reject(409, "busy", "queue eligibility must be claimed before raw acquire", retryable=True, failure_class="conflict")
        candidate = next((item for item in active if item["queue_id"] == queue_id), None)
        if candidate is None:
            raise _Reject(409, "busy", "queue entry is not eligible", retryable=True, failure_class="conflict")
        if not _same_principal(candidate.get("principal"), context.principal):
            raise _Reject(403, "denied", "queue entry belongs to another principal", failure_class="policy")
        if candidate.get("class") != requested_class or candidate.get("purpose") != purpose:
            raise _Reject(403, "denied", "queue request does not match the queued admission", failure_class="policy")
        if candidate.get("eligible") is not True or candidate.get("predecessor") is not None or not ordered or ordered[0]["queue_id"] != queue_id:
            raise _Reject(409, "busy", "queue entry is not eligible", retryable=True, failure_class="conflict")
        return candidate

    def _choose_lane(self, connection: sqlite3.Connection, requested: str | None, requested_class: str) -> dict[str, Any]:
        if requested is not None:
            lane = self.store.get_lane(requested, connection=connection)
            if lane is None:
                raise _Reject(403, "denied", "unknown lane", failure_class="policy")
            return lane
        candidates = [lane for lane in self.store.all_lanes(connection=connection) if lane.get("state") == "free" and lane.get("enabled") and lane.get("reachability") == "confirmed"]
        candidates.sort(key=lambda item: (CLASS_RANK.get(requested_class, 99), item["lane_id"]))
        if not candidates:
            raise _Reject(409, "busy", "no eligible lane", retryable=True, failure_class="conflict")
        return candidates[0]

    def _assert_lane_available(self, lane: Mapping[str, Any]) -> None:
        if lane.get("reachability") != "confirmed" or not lane.get("enabled", True):
            raise _Reject(503, "unavailable", "lane reachability is not confirmed", retryable=True, failure_class="transport")
        if lane.get("state") != "free":
            if lane.get("state") in {"quarantined", "unknown"}:
                raise _Reject(503, "unknown", "lane state is unknown", retryable=True, failure_class="state")
            raise _Reject(409, "busy", "lane is occupied", retryable=True, failure_class="conflict")

    def _quota_limit(self, lane: Mapping[str, Any]) -> int | None:
        configured = lane.get("quota_limit")
        if configured is None:
            key = str(lane.get("quota_key", lane.get("host_id", "")))
            configured = self.policy.get("quotas", {}).get(key)
        if configured is None:
            return None
        try:
            limit = int(configured)
        except (TypeError, ValueError) as exc:
            raise _Reject(503, "unknown", "lane quota configuration is invalid", retryable=True, failure_class="state") from exc
        if limit < 1:
            raise _Reject(503, "unknown", "lane quota configuration is invalid", retryable=True, failure_class="state")
        return limit

    def _assert_quota(self, connection: sqlite3.Connection, lane: Mapping[str, Any]) -> None:
        limit = self._quota_limit(lane)
        if limit is None:
            return
        key = str(lane.get("quota_key", lane.get("host_id", "")))
        occupied = 0
        for lease, reservation_status in self.store.leases(connection=connection):
            lease_lane = lease.get("lane", {})
            lane_record = self.store.get_lane(str(lease_lane.get("lane_id")), connection=connection) or {}
            lease_key = str(lane_record.get("quota_key", lane_record.get("host_id", "")))
            if lease_key == key and reservation_status in {"pending", "acknowledged", "uncertain"} and lease.get("state") in ACTIVE_LEASE_STATES:
                occupied += 1
        if occupied >= limit:
            raise _Reject(409, "busy", "shared-device quota is exhausted", retryable=True, failure_class="conflict")

    def _approval_row(self, connection: sqlite3.Connection, approval_id: str, *, action: str, context: _Context, lane: Mapping[str, Any] | None = None, generation: int | None = None, booking_id: str | None = None, revision: int | None = None, payload_hash: str | None = None, manifest_hash: str | None = None, policy_hash: str | None = None, max_s: int | None = None, max_end: datetime | None = None) -> dict[str, Any]:
        approval = self.store.get_approval(approval_id, connection=connection)
        if approval is None or approval.get("state") != "approved":
            raise _Reject(403, "denied", "approval is absent, unapproved, or already consumed", failure_class="policy")
        if _parse_time(str(approval["expires"])) <= self._utc():
            raise _Reject(403, "denied", "approval expired", failure_class="policy")
        checks: list[tuple[str, Any]] = [("action", action), ("destination_site", self.site_id), ("controller_id", self.controller_id), ("requester", context.principal)]
        if lane is not None:
            checks.append(("lane", lane))
        if generation is not None:
            checks.append(("target_generation", generation))
        if booking_id is not None:
            checks.append(("booking_id", booking_id))
        if revision is not None:
            checks.append(("revision", revision))
        if payload_hash is not None:
            checks.append(("payload_hash", payload_hash))
        if manifest_hash is not None:
            checks.append(("manifest_hash", manifest_hash))
        if policy_hash is not None:
            checks.append(("policy_hash", policy_hash))
        for field, expected in checks:
            if approval.get(field) != expected:
                raise _Reject(403, "denied", f"approval binding mismatch: {field}", failure_class="policy")
        if action in {"extension", "forced-preemption"}:
            if generation is None or approval.get("target_generation") != generation:
                raise _Reject(403, "denied", "approval target generation mismatch", failure_class="policy")
        elif approval.get("target_generation") is not None:
            raise _Reject(403, "denied", "approval has an unexpected target generation", failure_class="policy")
        bounds = approval.get("bounds", {})
        if max_s is not None and int(bounds.get("max_s", 0)) < max_s:
            raise _Reject(403, "denied", "approval duration bound is too small", failure_class="policy")
        if max_end is not None and _parse_time(str(bounds.get("max_end"))) < max_end:
            raise _Reject(403, "denied", "approval end bound is too small", failure_class="policy")
        return approval

    def _consume_approval(self, connection: sqlite3.Connection, approval: Mapping[str, Any], context: _Context, request_id: str) -> None:
        updated = dict(approval)
        updated["state"] = "consumed"
        updated["consumed_at"] = _time_text(self._utc())
        self.store.put_approval(updated, connection=connection)
        self.store.put_event(self._event(kind="approval", state="consumed", request_id=request_id, context=context, lane=updated.get("lane"), generation=updated.get("target_generation"), reason="approval consumed", data={"approval_id": updated["id"]}), connection=connection)

    def _make_lease(self, lane: Mapping[str, Any], context: _Context, args: Mapping[str, Any], effective_class: str, generation: int, *, booking_id: str | None = None, approved_max_end: datetime | None = None) -> dict[str, Any]:
        now = self._utc()
        max_s = int(args["max_s"])
        max_end = now + timedelta(seconds=max_s)
        approved_end = approved_max_end or max_end
        if approved_end < max_end:
            max_end = approved_end
        token = secrets.token_urlsafe(32)
        instance = f"instance-{uuid.uuid4().hex}"
        lease_id = f"lease-{uuid.uuid4().hex}"
        generation_deadline = self._deadline(int((max_end - now).total_seconds()))
        return {
            "schema_version": 1,
            "lease_id": lease_id,
            "lane": dict(lane["lane"]),
            "generation": generation,
            "reservation": {"lane": dict(lane["lane"]), "generation": generation, "state": "starting"},
            "token": token,
            "instance": instance,
            "principal": _principal_copy(context.principal),
            "class": effective_class,
            "purpose": str(args["purpose"]),
            "estimated_s": int(args["est_s"]),
            "started_at": _time_text(now),
            "max_end": _time_text(max_end),
            "approved_max_end": _time_text(approved_end),
            "heartbeat_at": _time_text(now),
            "deadline": generation_deadline,
            "booking_id": booking_id,
            "unit": None,
            "invocation": None,
            "state": "starting",
        }

    def _grant_data(self, lease: Mapping[str, Any], operation: str, mode: str = "fresh-acquire") -> dict[str, Any]:
        return {"kind": "grant", "operation": operation, "token": lease["token"], "generation": lease["generation"], "lease": copy.deepcopy(dict(lease)), "reservation": copy.deepcopy(lease["reservation"]), "adoption": {"mode": mode, "principal_bound": True, "generation_bound": True, "token_source": "controller-grant" if mode == "fresh-acquire" else "authenticated-adoption"}}

    def _acquire(self, request: Mapping[str, Any], context: _Context, fingerprint: str, *, operation: str = "acquire", forced_booking_id: str | None = None, idempotency_claimed: bool = False) -> dict[str, Any]:
        args = request.get("args", {})
        if not isinstance(args, Mapping) or not isinstance(args.get("purpose"), str) or not args.get("purpose"):
            raise _Reject(403, "denied", "purpose is required", failure_class="policy")
        if not isinstance(args.get("est_s"), int) or not isinstance(args.get("max_s"), int) or args["est_s"] < 1 or args["max_s"] < 1 or args["est_s"] > args["max_s"]:
            raise _Reject(403, "denied", "invalid estimate or maximum", failure_class="policy")
        pipeline, pipeline_approval_id = self._pipeline_admission(request, args, context)
        self._validate_delegation(request, context, "acquire", pipeline.get("pipeline_id") if pipeline else None)
        batch = request.get("admission", {}).get("batch") if isinstance(request.get("admission"), Mapping) else None
        batch_arms = self._validate_batch(batch) if isinstance(batch, Mapping) else None
        booking_id = forced_booking_id or args.get("booking_id")
        with self.store.transaction() as connection:
            if not idempotency_claimed:
                existing = self._existing(connection, request, context, fingerprint)
                if existing is not None:
                    return existing
            self._health_tx(connection)
            if not idempotency_claimed:
                self._claim_request(connection, request, context, fingerprint)
            booking = self.store.get_booking(str(booking_id), connection=connection) if booking_id else None
            effective_class = self._effective_class(args, context, booking=booking)
            admission = request.get("admission") if isinstance(request.get("admission"), Mapping) else {}
            selected_approval = admission.get("approval") if isinstance(admission, Mapping) else None
            approval_id = str(pipeline_approval_id) if pipeline_approval_id else None
            approval_action = "pipeline"
            if effective_class == "operator":
                if not isinstance(selected_approval, Mapping) or selected_approval.get("required") is not True or not selected_approval.get("approval_id"):
                    raise _Reject(403, "denied", "operator admission requires a signed approval", failure_class="policy")
                if approval_id is not None:
                    raise _Reject(403, "denied", "operator and pipeline approvals cannot be combined", failure_class="policy")
                approval_id = str(selected_approval["approval_id"])
                approval_action = "operator-admission"
            elif isinstance(selected_approval, Mapping) and selected_approval.get("required"):
                if approval_id is None:
                    raise _Reject(403, "denied", "unexpected approval selection", failure_class="policy")
            lane = self._choose_lane(connection, request.get("lane"), effective_class)
            self._assert_lane_available(lane)
            self._assert_quota(connection, lane)
            if booking_id:
                if booking is None or not _same_principal(booking.get("principal"), context.principal) or booking.get("state") not in {"scheduled", "blocked"}:
                    raise _Reject(403, "denied", "booking is not claimable", failure_class="policy")
                if booking.get("lane", {}).get("lane_id") != lane["lane_id"]:
                    raise _Reject(403, "denied", "booking lane mismatch", failure_class="policy")
                if args.get("purpose") != booking.get("purpose"):
                    raise _Reject(403, "denied", "booking purpose mismatch", failure_class="policy")
                now = self._utc()
                start = _parse_time(str(booking["start"]))
                end = _parse_time(str(booking["end"]))
                if now < start - timedelta(minutes=15):
                    raise _Reject(403, "denied", "booking claim window has not opened", failure_class="policy")
                if now >= end:
                    raise _Reject(409, "conflict", "booking has ended", failure_class="conflict")
            active_queue = self._queue_active(connection, lane["lane_id"])
            queue_id = args.get("queue_id")
            queue_entry = self._queue_eligible(active_queue, str(queue_id) if queue_id else None, effective_class, str(args["purpose"]), context)
            if queue_id and queue_entry is None:
                raise _Reject(409, "busy", "queue entry is not eligible", retryable=True, failure_class="conflict")
            future = [item for item in self.store.all_bookings(lane_id=lane["lane_id"], connection=connection) if item.get("state") in {"scheduled", "blocked"} and item.get("booking_id") != booking_id and _parse_time(item["start"]) > self._utc()]
            future.sort(key=lambda item: item["start"])
            if future:
                next_start = _parse_time(future[0]["start"])
                estimate_end = self._utc() + timedelta(seconds=int(args["est_s"]))
                if estimate_end > next_start:
                    raise _Reject(409, "conflict", "estimate crosses next booking", retryable=False, failure_class="conflict", details={"next_booking_id": future[0]["booking_id"], "next_start": future[0]["start"], "fitting_max_s": max(0, int((next_start - self._utc()).total_seconds()))})
            generation = int(lane.get("generation", 0)) + 1
            if approval_id:
                desired_end = self._utc() + timedelta(seconds=int(args["max_s"]))
                selected_approval = self.store.get_approval(approval_id, connection=connection)
                approval_lane = lane["lane"] if selected_approval and selected_approval.get("lane") is not None else None
                approval = self._approval_row(
                    connection,
                    approval_id,
                    action=approval_action,
                    context=context,
                    lane=approval_lane,
                    generation=generation if approval_action in {"extension", "forced-preemption"} else None,
                    booking_id=booking_id,
                    revision=pipeline.get("revision") if approval_action == "pipeline" and pipeline else None,
                    payload_hash=(args.get("signed_manifest") or {}).get("canonical_payload_hash") if isinstance(args.get("signed_manifest"), Mapping) else None,
                    manifest_hash=(args.get("signed_manifest") or {}).get("manifest_hash") if isinstance(args.get("signed_manifest"), Mapping) else None,
                    policy_hash=pipeline.get("policy_hash") if approval_action == "pipeline" and pipeline else None,
                    max_s=int(args["max_s"]),
                    max_end=desired_end,
                )
                approved_max_end = _parse_time(approval["bounds"]["max_end"])
            else:
                approval = None
                approved_max_end = None
            if batch_arms is not None:
                self._persist_batch_queues(connection, batch, batch_arms, lane, context)
            lease = self._make_lease(lane, context, args, effective_class, generation, booking_id=str(booking_id) if booking_id else None, approved_max_end=approved_max_end)
            lane_new = dict(lane)
            lane_new["state"] = "starting"
            lane_new["generation"] = generation
            lane_new["updated_at"] = _time_text(self._utc())
            self.store.put_lane(lane_new, connection=connection)
            self.store.put_lease(lease, reservation_status="pending", connection=connection)
            if approval is not None:
                self._consume_approval(connection, approval, context, str(request["request_id"]))
        ok, _reply, why = self._executor_call(lane, lease, "reserve")
        if not ok:
            with self.store.transaction() as connection:
                current = self.store.get_lease(lease_id=lease["lease_id"], connection=connection)
                if current:
                    record, _status = current
                    record["state"] = "quarantined"
                    record["reservation"]["state"] = "quarantined"
                    self.store.put_lease(record, reservation_status="uncertain", connection=connection)
                    lane_bad = self.store.get_lane(lane["lane_id"], connection=connection) or dict(lane)
                    lane_bad["state"] = "quarantined"
                    lane_bad["uncertainty_reason"] = why
                    self.store.put_lane(lane_bad, connection=connection)
                    response = self._response(str(request["request_id"]), 503, error=self._error("unknown", why, retryable=True, failure_class="state"))
                    self._remember(connection, request, context, fingerprint, response)
                    self.store.put_event(self._event(kind="reconcile", state="quarantined", request_id=str(request["request_id"]), context=context, lane=lease["lane"], generation=lease["generation"], reason=why, data={"lease_id": lease["lease_id"]}), connection=connection)
                    return response
            raise _Reject(503, "unknown", why, retryable=True, failure_class="state")
        with self.store.transaction() as connection:
            current = self.store.get_lease(lease_id=lease["lease_id"], connection=connection)
            if current is None:
                raise _Reject(503, "unknown", "reservation disappeared", retryable=True, failure_class="state")
            final_lease, status = current
            if status != "pending" or final_lease.get("state") == "quarantined":
                raise _Reject(503, "unknown", "reservation requires reconciliation", retryable=True, failure_class="state")
            final_lease["state"] = "starting"
            final_lease["reservation"]["state"] = "starting"
            # Reserve owns the generation; the executor-facing start unit and
            # invocation identities are allocated before the grant becomes
            # usable and then remain stable for release fencing.
            final_lease["unit"] = final_lease.get("unit") or f"unit-{uuid.uuid4().hex}"
            final_lease["invocation"] = final_lease.get("invocation") or f"invoke-{uuid.uuid4().hex}"
            self.store.put_lease(final_lease, reservation_status="acknowledged", connection=connection)
            if queue_entry is not None:
                queue_entry["state"] = "claimed"
                queue_entry["eligible"] = False
                queue_entry["reservation"] = {"lane": lane["lane"], "generation": generation, "state": "reserved"}
                self.store.put_queue(queue_entry, connection=connection)
            if booking_id:
                booking = self.store.get_booking(str(booking_id), connection=connection)
                if booking:
                    booking["state"] = "claimed"
                    booking["checked_in_at"] = booking.get("checked_in_at") or _time_text(self._utc())
                    booking["revision"] = int(booking.get("revision", 1)) + 1
                    booking["reservation"] = {"lane": lane["lane"], "generation": generation, "state": "starting"}
                    self.store.put_booking(booking, connection=connection)
            data = self._grant_data(final_lease, operation)
            response = self._response(str(request["request_id"]), 200, data=data)
            self._remember(connection, request, context, fingerprint, response)
            self.store.put_event(self._event(kind=operation, state="starting", request_id=str(request["request_id"]), context=context, lane=final_lease["lane"], generation=final_lease["generation"], reason="reserve acknowledged", data={"lease_id": final_lease["lease_id"], "token_redacted": True}), connection=connection)
            return response

    def _lookup_token(self, connection: sqlite3.Connection, request: Mapping[str, Any], context: _Context, *, allow_preempt: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
        args = request.get("args", {})
        if "generation" in args or "generation_source" in args:
            raise _Reject(403, "denied", "token operations derive generation from the server", failure_class="policy")
        token = args.get("token")
        row = self.store.get_lease(token=str(token), connection=connection) if isinstance(token, str) else None
        if row is None:
            raise _Reject(403, "denied", "unknown token", failure_class="policy")
        lease, reservation_status = row
        if not allow_preempt and not _same_principal(lease.get("principal"), context.principal):
            raise _Reject(403, "denied", "token principal mismatch", failure_class="policy")
        if request.get("lane") != lease.get("lane", {}).get("lane_id"):
            raise _Reject(403, "denied", "token lane mismatch", failure_class="policy")
        if args.get("instance") is not None and args.get("instance") != lease.get("instance"):
            raise _Reject(403, "denied", "token instance mismatch", failure_class="policy")
        lane = self.store.get_lane(str(request.get("lane")), connection=connection)
        if lane is None:
            raise _Reject(503, "unknown", "lane state unavailable", retryable=True, failure_class="state")
        if reservation_status != "acknowledged" or lease.get("state") not in {"starting", "running"} or lane.get("state") == "quarantined":
            raise _Reject(503, "unknown", "lease requires reconciliation", retryable=True, failure_class="state")
        return lease, lane

    def _release_or_preempt(self, request: Mapping[str, Any], context: _Context, fingerprint: str, *, preempt: bool = False) -> dict[str, Any]:
        with self.store.transaction() as connection:
            existing = self._existing(connection, request, context, fingerprint)
            if existing is not None:
                return existing
            self._health_tx(connection)
            self._claim_request(connection, request, context, fingerprint)
            lease, lane = self._lookup_token(connection, request, context, allow_preempt=preempt)
            approval = None
            approval_action = "forced-preemption"
            target_booking = None
            if preempt:
                caller_class = "operator" if {"operator", "john"} & set(context.roles) else next((item for item in CLASSES if item in context.roles), None)
                if caller_class not in {"operator", "booked", "batch"}:
                    raise _Reject(403, "denied", "this principal cannot preempt work", failure_class="policy")
                target_booking = self.store.get_booking(str(lease.get("booking_id")), connection=connection) if lease.get("booking_id") else None
                target_protected = lease.get("class") in PROTECTED_CLASSES
                if not target_protected:
                    if lease.get("class") not in {"service", "resident", "standby"}:
                        raise _Reject(403, "denied", "eviction matrix protects this target", failure_class="policy")
                else:
                    approval_id = request["args"].get("approval_id")
                    if not approval_id:
                        raise _Reject(403, "denied", "forced preemption approval is required", failure_class="policy")
                    if target_booking is not None:
                        approval_action = "displacement"
                    approval_record = self.store.get_approval(str(approval_id), connection=connection)
                    approval_lane = lease["lane"] if approval_record and approval_record.get("lane") is not None else None
                    approval = self._approval_row(connection, str(approval_id), action=approval_action, context=context, lane=approval_lane, generation=None if approval_action == "displacement" else int(lease["generation"]), booking_id=target_booking["booking_id"] if target_booking is not None else None, revision=int(target_booking["revision"]) if target_booking is not None else None, max_end=_parse_time(lease["max_end"]))
                    self._consume_approval(connection, approval, context, str(request["request_id"]))
            lease["state"] = "stopping"
            lease["reservation"]["state"] = "stopping"
            lane_new = dict(lane)
            lane_new["state"] = "stopping"
            self.store.put_lease(lease, reservation_status="acknowledged", connection=connection)
            self.store.put_lane(lane_new, connection=connection)
        ok, _reply, why = self._executor_call(lane, lease, "stop", stop_authority={"mode": "approved-forced-preemption" if approval is not None else "controller-match", "approval_id": request["args"].get("approval_id") if approval is not None else None})
        with self.store.transaction() as connection:
            current = self.store.get_lease(lease_id=lease["lease_id"], connection=connection)
            if current is None:
                raise _Reject(503, "unknown", "lease disappeared during stop", retryable=True, failure_class="state")
            lease_current, _status = current
            if not ok:
                lease_current["state"] = "quarantined"
                lease_current["reservation"]["state"] = "quarantined"
                self.store.put_lease(lease_current, reservation_status="uncertain", connection=connection)
                lane_bad = dict(lane, state="quarantined", uncertainty_reason=why)
                self.store.put_lane(lane_bad, connection=connection)
                response = self._response(str(request["request_id"]), 503, error=self._error("unknown", why, retryable=True, failure_class="state"))
                self._remember(connection, request, context, fingerprint, response)
                self.store.put_event(self._event(kind="reconcile", state="quarantined", request_id=str(request["request_id"]), context=context, lane=lease["lane"], generation=lease["generation"], reason=why, data={"lease_id": lease["lease_id"]}), connection=connection)
                return response
            lease_current["state"] = "stopping"
            lane_new = dict(lane)
            lane_new["state"] = "free"
            self.store.put_lease(lease_current, reservation_status="acknowledged", connection=connection)
            self.store.put_lane(lane_new, connection=connection)
            if lane_new["state"] == "free":
                lease_current["state"] = "stopping"
            data = self._mutation("preempt" if preempt else "release", "lease", lease_current["lease_id"], "free" if lane_new["state"] == "free" else "quarantined", lease_current["lane"], lease_current["generation"])
            response = self._response(str(request["request_id"]), 200, data=data)
            self._remember(connection, request, context, fingerprint, response)
            self.store.put_event(self._event(kind="preempt" if preempt else "release", state="free", request_id=str(request["request_id"]), context=context, lane=lease_current["lane"], generation=lease_current["generation"], reason="executor confirmed empty", data={"lease_id": lease_current["lease_id"], "token_redacted": True}), connection=connection)
            if target_booking is not None and approval_action == "displacement":
                target_booking["state"] = "displaced"
                target_booking["displacement"] = approval["id"] if approval is not None else None
                target_booking["revision"] = int(target_booking["revision"]) + 1
                self.store.put_booking(target_booking, connection=connection)
                self.store.put_event(self._event(kind="book", state="displaced", request_id=str(request["request_id"]), context=context, lane=target_booking["lane"], generation=lease_current["generation"], reason="approved booking displacement", data={"booking_id": target_booking["booking_id"]}), connection=connection)
            return response

    def _renew(self, request: Mapping[str, Any], context: _Context, fingerprint: str) -> dict[str, Any]:
        with self.store.transaction() as connection:
            existing = self._existing(connection, request, context, fingerprint)
            if existing is not None:
                return existing
            self._health_tx(connection)
            self._claim_request(connection, request, context, fingerprint)
            lease, lane = self._lookup_token(connection, request, context)
            extension = request["args"].get("extend_s")
            if not isinstance(extension, int) or extension < 1:
                raise _Reject(403, "invalid", "renew extension must be positive", failure_class="client")
            proposed = _parse_time(lease["max_end"]) + timedelta(seconds=extension)
            approved = _parse_time(lease["approved_max_end"])
            if proposed > approved:
                raise _Reject(409, "conflict", "approved maximum reached", failure_class="conflict")
            lease["max_end"] = _time_text(proposed)
            lease["heartbeat_at"] = _time_text(self._utc())
            lease["deadline"] = self._deadline(int((proposed - self._utc()).total_seconds()))
            self.store.put_lease(lease, reservation_status="acknowledged", connection=connection)
            data = self._mutation("renew", "lease", lease["lease_id"], lease["state"], lease["lane"], lease["generation"])
            data["revision"] = 2
            response = self._response(str(request["request_id"]), 200, data=data)
            self._remember(connection, request, context, fingerprint, response)
            self.store.put_event(self._event(kind="renew", state=lease["state"], request_id=str(request["request_id"]), context=context, lane=lease["lane"], generation=lease["generation"], reason="lease extended within approved bound", data={"lease_id": lease["lease_id"], "token_redacted": True}), connection=connection)
            return response

    def _claim(self, request: Mapping[str, Any], context: _Context, fingerprint: str) -> dict[str, Any]:
        args = request.get("args", {})
        if "token" in args:
            if args.get("generation_source") != "authenticated-adoption" or not isinstance(args.get("generation"), int):
                raise _Reject(403, "denied", "authenticated adoption requires a generation", failure_class="policy")
            with self.store.transaction() as connection:
                existing = self._existing(connection, request, context, fingerprint)
                if existing is not None:
                    return existing
                self._health_tx(connection)
                self._claim_request(connection, request, context, fingerprint)
                lease, lane = self._lookup_token(connection, {**request, "args": {"token": args.get("token"), "instance": args.get("instance")}}, context)
                if int(args["generation"]) != int(lease["generation"]):
                    raise _Reject(403, "denied", "adoption generation mismatch", failure_class="policy")
                response = self._response(str(request["request_id"]), 200, data=self._grant_data(lease, "claim", "authenticated-adoption"))
                self._remember(connection, request, context, fingerprint, response)
                self.store.put_event(self._event(kind="claim", state=lease["state"], request_id=str(request["request_id"]), context=context, lane=lease["lane"], generation=lease["generation"], reason="authenticated adoption", data={"lease_id": lease["lease_id"], "token_redacted": True}), connection=connection)
                return response
        booking_id = args.get("booking_id")
        revision = args.get("revision")
        if not isinstance(booking_id, str) or not isinstance(revision, int):
            raise _Reject(403, "invalid", "claim requires a booking or authenticated adoption", failure_class="client")
        with self.store.transaction() as connection:
            existing = self._existing(connection, request, context, fingerprint)
            if existing is not None:
                return existing
            self._health_tx(connection)
            self._claim_request(connection, request, context, fingerprint)
            booking = self.store.get_booking(booking_id, connection=connection)
            if booking is None or int(booking.get("revision", 0)) != revision or not _same_principal(booking.get("principal"), context.principal):
                raise _Reject(403, "denied", "booking identity or revision mismatch", failure_class="policy")
            now = self._utc()
            start = _parse_time(booking["start"])
            end = _parse_time(booking["end"])
            if now < start - timedelta(minutes=15):
                raise _Reject(403, "denied", "booking claim window has not opened", failure_class="policy")
            lane = self.store.get_lane(booking["lane"]["lane_id"], connection=connection)
            blocked_check_in = lane is not None and lane.get("state") != "free" and start <= now <= start + timedelta(minutes=15)
            if now >= end and not blocked_check_in:
                booking["state"] = "missed"
                booking["revision"] = revision + 1
                self.store.put_booking(booking, connection=connection)
                raise _Reject(409, "conflict", "booking has ended", failure_class="conflict")
            if lane is None:
                raise _Reject(503, "unknown", "booking lane unavailable", retryable=True, failure_class="state")
            if lane.get("state") != "free":
                if start <= now <= start + timedelta(minutes=15):
                    booking["state"] = "blocked"
                    booking["checked_in_at"] = booking.get("checked_in_at") or _time_text(now)
                    booking["recovery"] = {"state": "blocked-check-in", "at": _time_text(now), "reason": "lane occupied during check-in"}
                    booking["revision"] = revision + 1
                    self.store.put_booking(booking, connection=connection)
                    data = {"kind": "booking", "booking": booking}
                    response = self._response(str(request["request_id"]), 200, data=data)
                    self._remember(connection, request, context, fingerprint, response)
                    self.store.put_event(self._event(kind="claim", state="blocked", request_id=str(request["request_id"]), context=context, lane=booking["lane"], generation=None, reason="blocked check-in recorded", data={"booking_id": booking_id}), connection=connection)
                    return response
                raise _Reject(409, "busy", "booking remains blocked", retryable=True, failure_class="conflict")
        # The actual reservation is performed by the same acquire path after
        # the check-in window has been admitted.  The booking revision is read
        # again there, so it cannot be silently shifted by a concurrent caller.
        acquire_args = {"purpose": booking["purpose"], "class": "booked", "est_s": max(1, int((end - now).total_seconds())), "max_s": max(1, int((end - now).total_seconds())), "booking_id": booking_id}
        acquire_request = dict(request, op="acquire", args=acquire_args)
        return self._acquire(acquire_request, context, fingerprint, operation="claim", forced_booking_id=booking_id, idempotency_claimed=True)

    # ---- queues and batches ---------------------------------------------

    def _persist_batch_queues(self, connection: sqlite3.Connection, batch: Mapping[str, Any], arms: Iterable[Mapping[str, Any]], lane: Mapping[str, Any], context: _Context) -> None:
        batch_record = dict(batch)
        batch_record["batch_id"] = str(batch_record["batch_id"])
        batch_record["arms"] = [dict(item) for item in arms]
        self.store.put_batch(batch_record, connection=connection)
        existing = {item["queue_id"]: item for item in self.store.all_queue(lane_id=lane["lane_id"], connection=connection)}
        sequence = max([int(item.get("sequence", 0)) for item in existing.values()] + [0])
        for arm in arms:
            queue_id = f"{batch_record['batch_id']}-{arm['arm_id']}"
            if queue_id in existing:
                continue
            sequence += 1
            predecessor = arm.get("predecessor")
            dependencies = list(arm.get("dependencies", ()))
            dependency = predecessor or (sorted(str(item) for item in dependencies)[0] if dependencies else None)
            predecessor_id = f"{batch_record['batch_id']}-{dependency}" if dependency else None
            entry = {
                "schema_version": 1,
                "queue_id": queue_id,
                "lane": dict(lane["lane"]),
                "reservation": {"lane": dict(lane["lane"]), "generation": None, "state": "unassigned"},
                "principal": _principal_copy(context.principal),
                "class": "batch",
                "purpose": str(arm.get("purpose", batch_record.get("purpose", "batch"))),
                "sequence": sequence,
                "predecessor": predecessor_id,
                "wait_deadline": self._deadline(600),
                "last_seen": _time_text(self._utc()),
                "state": "queued",
                "eligible": predecessor_id is None,
            }
            self.store.put_queue(entry, connection=connection)
            dependency_names = {str(item) for item in dependencies}
            if predecessor is not None:
                dependency_names.add(str(predecessor))
            dependency_ids = [f"{batch_record['batch_id']}-{item}" for item in sorted(dependency_names)]
            self.store.put_queue_dependencies(queue_id, dependency_ids, connection=connection)

    def register_batch(self, batch: Mapping[str, Any], *, peer: str | None = None, ingress_peer: str | None = None, lane: str | None = None) -> dict[str, Any]:
        """Register every arm atomically, useful to callers before acquire."""

        context = self._authenticate(ingress_peer or peer)
        arms = self._validate_batch(batch)
        with self.store.transaction() as connection:
            self._health_tx(connection)
            selected = self._choose_lane(connection, lane, "batch")
            self._persist_batch_queues(connection, batch, arms, selected, context)
            return {"batch_id": batch["batch_id"], "arm_ids": [item["arm_id"] for item in arms], "lane": selected["lane"], "visible": True}

    def _queue(self, request: Mapping[str, Any], context: _Context, fingerprint: str) -> dict[str, Any]:
        args = request.get("args", {})
        action = args.get("action")
        if action == "add" and (not isinstance(args.get("purpose"), str) or not args.get("purpose")):
            raise _Reject(403, "denied", "purpose is required", failure_class="policy")
        with self.store.transaction() as connection:
            existing = self._existing(connection, request, context, fingerprint)
            if existing is not None:
                return existing
            self._health_tx(connection)
            if action == "list":
                lane_ids = [str(request["lane"])] if request.get("lane") else [str(item["lane_id"]) for item in self.store.all_lanes(connection=connection)]
                for lane_id in lane_ids:
                    self._queue_active(connection, lane_id)
                entries = self.store.all_queue(lane_id=request.get("lane"), connection=connection) if request.get("lane") else self.store.all_queue(connection=connection)
                return self._response(str(request["request_id"]), 200, data={"kind": "queue", "entries": entries})
            self._claim_request(connection, request, context, fingerprint)
            lane_id = request.get("lane")
            if not lane_id:
                raise _Reject(403, "invalid", "queue mutation requires a lane", failure_class="client")
            lane = self.store.get_lane(str(lane_id), connection=connection)
            if lane is None:
                raise _Reject(403, "denied", "unknown lane", failure_class="policy")
            if action == "add":
                effective_class = self._effective_class(args, context)
                active = self._queue_active(connection, str(lane_id))
                sequence = max([int(item.get("sequence", 0)) for item in self.store.all_queue(lane_id=str(lane_id), connection=connection)] + [0]) + 1
                entry = {
                    "schema_version": 1,
                    "queue_id": f"queue-{uuid.uuid4().hex}",
                    "lane": dict(lane["lane"]),
                    "reservation": {"lane": dict(lane["lane"]), "generation": None, "state": "unassigned"},
                    "principal": _principal_copy(context.principal),
                    "class": effective_class,
                    "purpose": str(args.get("purpose", "")),
                    "sequence": sequence,
                    "predecessor": None,
                    "wait_deadline": self._deadline(int(args.get("max_wait_s", 600))),
                    "last_seen": _time_text(self._utc()),
                    "state": "queued",
                    "eligible": not active,
                }
                self.store.put_queue(entry, connection=connection)
                data = self._mutation("queue", "queue", entry["queue_id"], "queued", entry["lane"], None)
                response = self._response(str(request["request_id"]), 200, data=data)
                self._remember(connection, request, context, fingerprint, response)
                self.store.put_event(self._event(kind="queue", state="queued", request_id=str(request["request_id"]), context=context, lane=entry["lane"], generation=None, reason="queue entry added", data={"queue_id": entry["queue_id"]}), connection=connection)
                return response
            queue_id = args.get("queue_id")
            entry = self.store.get_queue(str(queue_id), connection=connection) if queue_id else None
            if entry is None or not _same_principal(entry.get("principal"), context.principal) or entry.get("lane", {}).get("lane_id") != lane_id:
                raise _Reject(403, "denied", "queue identity mismatch", failure_class="policy")
            if action == "refresh":
                entry["last_seen"] = _time_text(self._utc())
                entry["wait_deadline"] = self._deadline(int(args.get("max_wait_s", 600)))
                if entry.get("state") == "expired":
                    raise _Reject(409, "conflict", "expired queue entry cannot refresh", failure_class="conflict")
                self.store.put_queue(entry, connection=connection)
                data = self._mutation("queue", "queue", entry["queue_id"], entry["state"], entry["lane"], None)
                response = self._response(str(request["request_id"]), 200, data=data)
                self._remember(connection, request, context, fingerprint, response)
                self.store.put_event(self._event(kind="queue", state=entry["state"], request_id=str(request["request_id"]), context=context, lane=entry["lane"], generation=None, reason="queue entry refreshed", data={"queue_id": entry["queue_id"]}), connection=connection)
                return response
            if action == "remove":
                entry["state"] = "removed"
                entry["eligible"] = False
                self.store.put_queue(entry, connection=connection)
                data = self._mutation("queue", "queue", entry["queue_id"], "removed", entry["lane"], None)
                response = self._response(str(request["request_id"]), 200, data=data)
                self._remember(connection, request, context, fingerprint, response)
                self.store.put_event(self._event(kind="queue", state="removed", request_id=str(request["request_id"]), context=context, lane=entry["lane"], generation=None, reason="queue entry removed", data={"queue_id": entry["queue_id"]}), connection=connection)
                return response
            raise _Reject(403, "invalid", "unsupported queue action", failure_class="client")

    # ---- approvals -------------------------------------------------------

    def _approval_request(self, request: Mapping[str, Any], context: _Context, fingerprint: str) -> dict[str, Any]:
        args = request.get("args", {})
        if not isinstance(args, Mapping):
            raise _Reject(403, "invalid", "approval request arguments are invalid", failure_class="client")
        allowed = {"action", "booking_id", "revision", "target_generation", "bounds", "reason", "destination_site", "controller_id", "payload_hash", "manifest_hash", "policy_hash"}
        if set(args) - allowed:
            raise _Reject(403, "denied", "challenge identity and expiry are server-issued", failure_class="policy")
        if args.get("destination_site") != self.site_id or args.get("controller_id") != self.controller_id:
            raise _Reject(403, "denied", "approval destination/controller mismatch", failure_class="policy")
        bounds = args.get("bounds")
        if not isinstance(bounds, Mapping) or int(bounds.get("max_s", 0)) < 1:
            raise _Reject(403, "invalid", "approval bounds are invalid", failure_class="client")
        max_end = _parse_time(str(bounds.get("max_end")))
        now = self._utc()
        if max_end <= now:
            raise _Reject(403, "denied", "approval bounds already expired", failure_class="policy")
        lane = None
        if request.get("lane") is not None:
            lane_record = self.store.get_lane(str(request["lane"]))
            if lane_record is None:
                raise _Reject(403, "denied", "unknown approval lane", failure_class="policy")
            lane = lane_record["lane"]
        approval_id = f"approval-{uuid.uuid4().hex}"
        challenge_id = f"challenge-{uuid.uuid4().hex}"
        nonce = f"nonce-{secrets.token_hex(16)}"
        expires = min(max_end, now + timedelta(minutes=5))
        record = {
            "schema_version": 1,
            "id": approval_id,
            "challenge_id": challenge_id,
            "challenge_nonce": nonce,
            "action": args["action"],
            "requester": _principal_copy(context.principal),
            "lane": lane,
            "booking_id": args.get("booking_id"),
            "revision": args.get("revision"),
            "target_generation": args.get("target_generation"),
            "bounds": {"max_s": int(bounds["max_s"]), "max_end": _time_text(max_end)},
            "reason": str(args["reason"]),
            "nonce": nonce,
            "expires": _time_text(expires),
            "approver": None,
            "approved_at": None,
            "proof": None,
            "verified_evidence": None,
            "consumed_at": None,
            "destination_site": self.site_id,
            "controller_id": self.controller_id,
            "payload_hash": args["payload_hash"],
            "manifest_hash": args.get("manifest_hash"),
            "policy_hash": args["policy_hash"],
            "challenge_policy_hash": args["policy_hash"],
            "challenge_manifest_hash": args.get("manifest_hash"),
            "canonicalization": "JCS-RFC8785",
            "canonical_encoding": "UTF-8",
            "hash_algorithm": "SHA-256",
            "domain": "flightctl/approval/v1",
            "signed_fields": list(APPROVAL_SIGNED_FIELDS),
            "state": "issued",
        }
        with self.store.transaction() as connection:
            existing = self._existing(connection, request, context, fingerprint)
            if existing is not None:
                return existing
            self._health_tx(connection)
            self._claim_request(connection, request, context, fingerprint)
            self.store.put_approval(record, connection=connection)
            data = {"kind": "approval", "approval": record}
            response = self._response(str(request["request_id"]), 200, data=data)
            self._remember(connection, request, context, fingerprint, response)
            self.store.put_event(self._event(kind="approval-request", state="issued", request_id=str(request["request_id"]), context=context, lane=lane, generation=record["target_generation"], reason="server-issued challenge", data={"approval_id": approval_id}), connection=connection)
            return response

    def _approve(self, request: Mapping[str, Any], context: _Context, fingerprint: str) -> dict[str, Any]:
        args = request.get("args", {})
        approval_id = args.get("approval_id")
        with self.store.transaction() as connection:
            existing = self._existing(connection, request, context, fingerprint)
            if existing is not None:
                return existing
            self._health_tx(connection)
            self._claim_request(connection, request, context, fingerprint)
            approval = self.store.get_approval(str(approval_id), connection=connection) if approval_id else None
            if approval is None or approval.get("state") != "issued":
                raise _Reject(403, "denied", "approval is absent or already used", failure_class="policy")
            if _parse_time(approval["expires"]) <= self._utc():
                raise _Reject(403, "denied", "approval expired", failure_class="policy")
            try:
                evidence = self.approval_verifier.verify(args.get("proof"), approval_digest(approval), evidence=args.get("evidence"), now=self._utc())
            except AuthError as exc:
                raise _Reject(403, "denied", str(exc), failure_class="policy") from exc
            updated = dict(approval)
            updated["state"] = "approved"
            updated["approver"] = _principal_copy(context.principal)
            updated["approved_at"] = _time_text(self._utc())
            updated["proof"] = copy.deepcopy(args["proof"])
            updated["verified_evidence"] = evidence
            self.store.put_approval(updated, connection=connection)
            data = {"kind": "approval", "approval": updated}
            response = self._response(str(request["request_id"]), 200, data=data)
            self._remember(connection, request, context, fingerprint, response)
            self.store.put_event(self._event(kind="approval", state="approved", request_id=str(request["request_id"]), context=context, lane=updated.get("lane"), generation=updated.get("target_generation"), reason="approval proof verified", data={"approval_id": approval_id}), connection=connection)
            return response

    # ---- booking ---------------------------------------------------------

    def _book(self, request: Mapping[str, Any], context: _Context, fingerprint: str) -> dict[str, Any]:
        args = request.get("args", {})
        if not isinstance(args.get("purpose"), str) or not args.get("purpose"):
            raise _Reject(403, "denied", "purpose is required", failure_class="policy")
        start = _parse_time(str(args.get("start")))
        end = _parse_time(str(args.get("end")))
        now = self._utc()
        duration = (end - start).total_seconds()
        max_s = self.policy["admission"]["operator_max_s"] if {"operator", "john"} & set(context.roles) else self.policy["admission"]["agent_max_s"]
        if end <= start or duration < self.policy["admission"]["minimum_booking_s"] or duration > max_s:
            raise _Reject(403, "denied", "booking duration is outside policy bounds", failure_class="policy")
        if start < now or start > now + timedelta(seconds=self.policy["admission"]["booking_horizon_s"]):
            raise _Reject(403, "denied", "booking is outside the fourteen-day horizon", failure_class="policy")
        lane_id = request.get("lane")
        if not lane_id:
            raise _Reject(403, "invalid", "booking requires a lane", failure_class="client")
        with self.store.transaction() as connection:
            existing = self._existing(connection, request, context, fingerprint)
            if existing is not None:
                return existing
            self._health_tx(connection)
            self._claim_request(connection, request, context, fingerprint)
            lane = self.store.get_lane(str(lane_id), connection=connection)
            if lane is None:
                raise _Reject(403, "denied", "unknown lane", failure_class="policy")
            future_for_principal = [item for item in self.store.all_bookings(connection=connection) if _same_principal(item.get("principal"), context.principal) and item.get("state") in {"scheduled", "blocked", "claimed"} and _parse_time(item["start"]) > now]
            if len(future_for_principal) >= 2 and not ({"operator", "john"} & set(context.roles)):
                raise _Reject(409, "conflict", "agent future booking limit reached", failure_class="conflict")
            for item in self.store.all_bookings(lane_id=str(lane_id), connection=connection):
                if item.get("state") in {"cancelled", "completed", "missed", "displaced"}:
                    continue
                if start < _parse_time(item["end"]) and end > _parse_time(item["start"]):
                    raise _Reject(409, "conflict", "booking overlaps an existing booking", failure_class="conflict")
            booking_id = f"booking-{uuid.uuid4().hex}"
            booking = {
                "schema_version": 1,
                "booking_id": booking_id,
                "revision": 1,
                "lane": dict(lane["lane"]),
                "reservation": {"lane": dict(lane["lane"]), "generation": None, "state": "unassigned"},
                "principal": _principal_copy(context.principal),
                "purpose": str(args["purpose"]),
                "start": _time_text(start),
                "end": _time_text(end),
                "state": "scheduled",
                "checked_in_at": None,
                "created_at": _time_text(now),
                "displacement": None,
                "recovery": {"state": "none", "at": None, "reason": None},
            }
            self.store.put_booking(booking, connection=connection)
            data = {"kind": "booking", "booking": booking}
            response = self._response(str(request["request_id"]), 200, data=data)
            self._remember(connection, request, context, fingerprint, response)
            self.store.put_event(self._event(kind="book", state="scheduled", request_id=str(request["request_id"]), context=context, lane=booking["lane"], generation=None, reason="booking created", data={"booking_id": booking_id}), connection=connection)
            return response

    def _cancel(self, request: Mapping[str, Any], context: _Context, fingerprint: str) -> dict[str, Any]:
        args = request.get("args", {})
        with self.store.transaction() as connection:
            existing = self._existing(connection, request, context, fingerprint)
            if existing is not None:
                return existing
            self._health_tx(connection)
            self._claim_request(connection, request, context, fingerprint)
            booking = self.store.get_booking(str(args.get("booking_id")), connection=connection)
            if booking is None or not _same_principal(booking.get("principal"), context.principal) or (args.get("revision") is not None and int(args["revision"]) != int(booking["revision"])):
                raise _Reject(403, "denied", "booking identity or revision mismatch", failure_class="policy")
            booking["state"] = "cancelled"
            booking["revision"] = int(booking["revision"]) + 1
            self.store.put_booking(booking, connection=connection)
            data = self._mutation("cancel", "booking", booking["booking_id"], "cancelled", booking.get("lane"), booking.get("reservation", {}).get("generation"))
            data["revision"] = booking["revision"]
            response = self._response(str(request["request_id"]), 200, data=data)
            self._remember(connection, request, context, fingerprint, response)
            self.store.put_event(self._event(kind="cancel", state="cancelled", request_id=str(request["request_id"]), context=context, lane=booking.get("lane"), generation=booking.get("reservation", {}).get("generation"), reason="booking cancelled", data={"booking_id": booking["booking_id"]}), connection=connection)
            return response

    # ---- occupants and redacted reads ----------------------------------

    @staticmethod
    def _redact_lease(lease: Mapping[str, Any]) -> dict[str, Any]:
        return {"schema_version": 1, "lease_id": lease["lease_id"], "lane": copy.deepcopy(lease["lane"]), "generation": lease["generation"], "reservation": copy.deepcopy(lease["reservation"]), "instance": lease["instance"], "principal": copy.deepcopy(lease["principal"]), "class": lease["class"], "purpose": lease["purpose"], "state": lease["state"], "token_redacted": True}

    @staticmethod
    def _redact_occupant(occupant: Mapping[str, Any]) -> dict[str, Any]:
        record = copy.deepcopy(dict(occupant))
        record.pop("token", None)
        record["token_redacted"] = True
        return record

    def _chat_load(self, request: Mapping[str, Any], context: _Context, fingerprint: str) -> dict[str, Any]:
        args = request.get("args", {})
        acquire_args = {"purpose": args.get("purpose"), "class": "service", "est_s": 1, "max_s": 600, "pipeline_ref": args.get("pipeline_ref")}
        acquire_request = dict(request, op="acquire", args=acquire_args)
        result = self._acquire(acquire_request, context, fingerprint, operation="chat-load")
        if result.get("status") != 200:
            return result
        grant = result["data"]
        lease = grant["lease"]
        occupant = {
            "schema_version": 1,
            "occupant_id": f"occupant-{uuid.uuid4().hex}",
            "lane": lease["lane"],
            "reservation": lease["reservation"],
            "generation": lease["generation"],
            "token": lease["token"],
            "instance": lease["instance"],
            "principal": lease["principal"],
            "class": "service",
            "pipeline_ref": args["pipeline_ref"],
            "purpose": args["purpose"],
            "loaded_at": lease["started_at"],
            "last_activity": lease["started_at"],
            "request_accounting": {"active_requests": 0, "completed_requests": 0, "last_completed_at": None, "activity_basis": "completed-user-request"},
            "state": "loading",
            "unit": lease.get("unit"),
            "invocation": lease.get("invocation"),
            "deadline": lease["deadline"],
        }
        with self.store.transaction() as connection:
            self.store.put_occupant(occupant, connection=connection)
            data = self._mutation("chat-load", "occupant", occupant["occupant_id"], "loading", occupant["lane"], occupant["generation"])
            result = self._response(str(request["request_id"]), 200, data=data)
            # The acquire path already owns the idempotency record; replace its
            # typed data with the chat mutation while preserving one record.
            self._remember(connection, request, context, fingerprint, result)
            self.store.put_event(self._event(kind="chat-load", state="loading", request_id=str(request["request_id"]), context=context, lane=occupant["lane"], generation=occupant["generation"], reason="service occupant registered", data={"occupant_id": occupant["occupant_id"], "token_redacted": True}), connection=connection)
            return result

    def _chat_unload(self, request: Mapping[str, Any], context: _Context, fingerprint: str) -> dict[str, Any]:
        args = request.get("args", {})
        with self.store.transaction() as connection:
            existing = self._existing(connection, request, context, fingerprint)
            if existing is not None:
                return existing
            self._health_tx(connection)
            self._claim_request(connection, request, context, fingerprint)
            occupant = self.store.get_occupant(str(args.get("occupant_id")), connection=connection)
            if occupant is None or not _same_principal(occupant.get("principal"), context.principal) or int(args.get("generation", 0)) != int(occupant.get("generation", -1)):
                raise _Reject(403, "denied", "occupant identity mismatch", failure_class="policy")
            lease_row = self.store.get_lease(token=occupant.get("token"), connection=connection)
            if lease_row is None:
                raise _Reject(503, "unknown", "occupant lease unavailable", retryable=True, failure_class="state")
            lease, _status = lease_row
            lane = self.store.get_lane(occupant["lane"]["lane_id"], connection=connection)
            occupant["state"] = "draining"
            self.store.put_occupant(occupant, connection=connection)
        ok, _reply, why = self._executor_call(lane or {}, lease, "stop")
        with self.store.transaction() as connection:
            if not ok:
                occupant["state"] = "quarantined"
                self.store.put_occupant(occupant, connection=connection)
                response = self._response(str(request["request_id"]), 503, error=self._error("unknown", why, retryable=True, failure_class="state"))
                self._remember(connection, request, context, fingerprint, response)
                return response
            occupant["state"] = "unloaded"
            self.store.put_occupant(occupant, connection=connection)
            lease["state"] = "stopping"
            lease["reservation"]["state"] = "stopping"
            self.store.put_lease(lease, reservation_status="acknowledged", connection=connection)
            if lane is not None:
                lane["state"] = "free"
                self.store.put_lane(lane, connection=connection)
            data = self._mutation("chat-unload", "occupant", occupant["occupant_id"], "unloaded", occupant["lane"], occupant["generation"])
            response = self._response(str(request["request_id"]), 200, data=data)
            self._remember(connection, request, context, fingerprint, response)
            self.store.put_event(self._event(kind="chat-unload", state="unloaded", request_id=str(request["request_id"]), context=context, lane=occupant["lane"], generation=occupant["generation"], reason="service occupant unloaded", data={"occupant_id": occupant["occupant_id"], "token_redacted": True}), connection=connection)
            return response

    def _read(self, request: Mapping[str, Any], context: _Context, fingerprint: str) -> dict[str, Any]:
        op = str(request["op"])
        with self.store.transaction(immediate=False) as connection:
            self._health_tx(connection) if op != "status" else None
            lane = self.store.get_lane(str(request["lane"]), connection=connection) if request.get("lane") else None
            if op == "status":
                if lane is None and request.get("lane"):
                    raise _Reject(403, "denied", "unknown lane", failure_class="policy")
                confirmed = bool(lane and lane.get("state") not in {"unknown", "quarantined"} and lane.get("reachability") == "confirmed")
                return self._response(str(request["request_id"]), 200, data={"kind": "status", "lane": lane["lane"] if lane else None, "state": lane.get("state", "unknown") if lane else "unknown", "generation": lane.get("generation") if lane else None, "occupancy": {"certainty": "confirmed" if confirmed else "unknown", "reason": None if confirmed else "controller has no confirmed observation"}, "reachability": {"certainty": "confirmed" if lane and lane.get("reachability") == "confirmed" else "unknown", "reason": None if lane and lane.get("reachability") == "confirmed" else "lane reachability is unknown"}})
            if op == "report":
                events = self.store.events(connection=connection)
                return self._response(str(request["request_id"]), 200, data={"kind": "report", "events": events, "next_cursor": None})
            if op == "queue":
                return self._response(str(request["request_id"]), 200, data={"kind": "queue", "entries": self.store.all_queue(lane_id=request.get("lane"), connection=connection) if request.get("lane") else self.store.all_queue(connection=connection)})
            if lane is None:
                raise _Reject(403, "denied", "read operation requires a known lane", failure_class="policy")
            leases = [record for record, _status in self.store.leases(connection=connection) if record.get("lane", {}).get("lane_id") == lane["lane_id"] and record.get("state") in ACTIVE_LEASE_STATES and not (lane.get("state") == "free" and record.get("state") == "stopping")]
            lease = leases[-1] if leases else None
            occupant = next((item for item in self.store.all_occupants(connection=connection) if item.get("lane", {}).get("lane_id") == lane["lane_id"] and item.get("state") not in {"unloaded", "quarantined"}), None)
            if op == "free" or op == "cal":
                windows: list[dict[str, Any]] = []
                known = lane.get("reachability") == "confirmed" and lane.get("state") not in {"unknown", "quarantined"}
                for booking in self.store.all_bookings(lane_id=lane["lane_id"], connection=connection):
                    if booking.get("state") in {"scheduled", "blocked", "claimed"}:
                        windows.append({"start": booking["start"], "end": booking["end"], "state": "booked", "certainty": "estimate", "reason": "future schedule projection"})
                if lease is not None:
                    windows.insert(0, {"start": lease["started_at"], "end": lease["max_end"], "state": "occupied", "certainty": "confirmed" if known else "unknown", "reason": None if known else "lane reachability or occupancy is not confirmed"})
                return self._response(str(request["request_id"]), 200, data={"kind": "projection", "scope": "calendar" if op == "cal" else "free", "lane": lane["lane"], "windows": windows, "observation": {"certainty": "confirmed" if known else "unknown", "reason": None if known else "lane reachability or occupancy is not confirmed"}})
            return self._response(str(request["request_id"]), 200, data={"kind": "occupancy", "lane": lane["lane"], "state": lane.get("state", "unknown"), "generation": lane.get("generation") if lease else None, "lease": self._redact_lease(lease) if lease else None, "occupant": self._redact_occupant(occupant) if occupant else None, "observation": {"certainty": "confirmed" if lane.get("state") != "quarantined" else "unknown", "reason": None if lane.get("state") != "quarantined" else "lane state is quarantined"}})

    def enforce_deadlines(self, *, peer: str | None = None, ingress_peer: str | None = None) -> list[str]:
        """Quarantine expired protected work without inferring hardware emptiness."""

        context = self._authenticate(ingress_peer or peer)
        changed: list[str] = []
        with self.store.transaction() as connection:
            self._health_tx(connection)
            now = self._utc()
            for lease, reservation_status in self.store.leases(connection=connection):
                if reservation_status != "acknowledged" or lease.get("state") not in {"starting", "running"}:
                    continue
                if _parse_time(lease["max_end"]) > now:
                    continue
                booking = self.store.get_booking(str(lease.get("booking_id")), connection=connection) if lease.get("booking_id") else None
                if booking is not None and lease.get("class") in PROTECTED_CLASSES and booking.get("reservation", {}).get("generation") == lease.get("generation"):
                    booking_end = _parse_time(str(booking["end"]))
                    if now >= booking_end and self.store.get_meta(f"booking-overrun:{booking['booking_id']}", connection=connection) is None:
                        self.store.set_meta(f"booking-overrun:{booking['booking_id']}", _time_text(now), connection=connection)
                        self.store.put_event(
                            self._controller_event(
                                kind="book",
                                state="recovery",
                                request_id=f"booking-overrun-{booking['booking_id']}",
                                lane=booking.get("lane"),
                                generation=lease.get("generation"),
                                reason="protected booking overrun; completion requested",
                                data={"booking_id": booking["booking_id"]},
                            ),
                            connection=connection,
                        )
                    if now < booking_end + timedelta(minutes=10):
                        continue
                lease["state"] = "quarantined"
                lease["reservation"]["state"] = "quarantined"
                self.store.put_lease(lease, reservation_status="uncertain", connection=connection)
                lane = self.store.get_lane(lease["lane"]["lane_id"], connection=connection)
                if lane is not None:
                    lane["state"] = "quarantined"
                    lane["uncertainty_reason"] = "lease deadline expired; emptiness is unverified"
                    self.store.put_lane(lane, connection=connection)
                self.store.put_event(self._event(kind="reconcile", state="quarantined", request_id=f"deadline-{lease['lease_id']}", context=context, lane=lease["lane"], generation=lease["generation"], reason="deadline expired without cleanup proof", data={"lease_id": lease["lease_id"]}), connection=connection)
                changed.append(lease["lease_id"])
        return changed

    # ---- public boundary -------------------------------------------------

    def handle(self, request: Mapping[str, Any], peer: str | None = None, *, ingress_peer: str | None = None) -> dict[str, Any]:
        request_id = str(request.get("request_id", "invalid-request")) if isinstance(request, Mapping) else "invalid-request"
        try:
            if not isinstance(request, Mapping):
                raise _Reject(403, "invalid", "RPC request must be an object", failure_class="client")
            if not self.store.available:
                raise _Reject(503, "unknown", "durable state is unavailable", retryable=True, failure_class="state")
            if request.get("schema") != 1 or not isinstance(request.get("op"), str) or not isinstance(request.get("args", {}), Mapping):
                raise _Reject(403, "invalid", "invalid RPC envelope", failure_class="client")
            actual_peer = ingress_peer if ingress_peer is not None else peer
            context = self._authenticate(actual_peer)
            self._validate_args_shape(str(request["op"]), request["args"])
            supplied = request.get("request_fingerprint")
            expected = request_fingerprint(request, principal=context.principal)
            if supplied is None:
                fingerprint = expected
            elif supplied != expected:
                raise _Reject(409, "conflict", "request fingerprint does not match authenticated request", failure_class="conflict")
            else:
                fingerprint = str(supplied)
            self._idempotency_scope(request)
            op = str(request["op"])
            if op in {"acquire", "chat-load"}:
                return self._acquire(request, context, fingerprint) if op == "acquire" else self._chat_load(request, context, fingerprint)
            if op == "renew":
                return self._renew(request, context, fingerprint)
            if op == "release":
                return self._release_or_preempt(request, context, fingerprint)
            if op == "preempt":
                return self._release_or_preempt(request, context, fingerprint, preempt=True)
            if op == "claim":
                return self._claim(request, context, fingerprint)
            if op == "queue":
                return self._queue(request, context, fingerprint)
            if op == "approval-request":
                return self._approval_request(request, context, fingerprint)
            if op == "approve":
                return self._approve(request, context, fingerprint)
            if op == "book":
                return self._book(request, context, fingerprint)
            if op == "cancel":
                return self._cancel(request, context, fingerprint)
            if op == "chat-unload":
                return self._chat_unload(request, context, fingerprint)
            if op in {"cal", "free", "report", "status"}:
                return self._read(request, context, fingerprint)
            raise _Reject(403, "invalid", "unsupported operation", failure_class="client")
        except _Reject as exc:
            return self._reject_response(request_id, exc)
        except (StoreUnavailable, sqlite3.DatabaseError, StoreError) as exc:
            return self._response(request_id, 503, error=self._error("unknown", f"durable state unavailable: {exc}", retryable=True, failure_class="state"))
        except Exception as exc:
            # A controller bug must not become an apparent free lane or a
            # successful mutation.  Keep the wire failure typed and safe.
            return self._response(request_id, 503, error=self._error("unknown", f"controller state unavailable: {exc}", retryable=True, failure_class="state"))

    rpc = handle
    dispatch = handle
    handle_rpc = handle

    def reconcile(self, *, peer: str | None = None, ingress_peer: str | None = None) -> dict[str, Any]:
        """Clear a recovery freeze only after an operator-supplied inspect pass.

        The P1 runtime has no executor inspect orchestration; therefore the
        safe default is to leave the freeze in place.  Callers can use the
        method as a visible boundary and later P2 can supply the real proof.
        """

        self._authenticate(ingress_peer or peer)
        return {"reconciled": False, "reason": "executor reconciliation is not implemented in P1"}


FlightAuthority = Authority
Controller = Authority
