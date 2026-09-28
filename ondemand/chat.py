"""Controller-gated chat loading, request accounting, and safe unloading.

The module is deliberately transport-neutral.  A chat adapter can report user
request lifecycle events, but it cannot select a lane or grant itself a lane.
The only operation which may start a service is :meth:`ChatController.load`,
with an explicit :class:`ChatSelection` and a successful authority grant.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import inspect as python_inspect
import json
from threading import RLock
from typing import Any, Protocol

from contracts.interfaces import Clock, GPUProbe, Systemd, Transport


ZERO_SHA256 = "0" * 64
DEFAULT_IDLE_S = 600
DEFAULT_DRAIN_GRACE_S = 120
_ELIGIBLE_EVICTORS = frozenset({"operator", "booked", "batch"})
_TRANSPORT_FAILURES = frozenset({"lost", "timeout", "delayed", "unknown", "failure"})
_EXECUTOR_ACKNOWLEDGEMENTS = {
    "reserve": "reserved",
    "start": "started",
    "inspect": "inspected",
    "stop": "stopped",
}
_SECRET_KEYS = frozenset({"token", "access_token", "refresh_token"})


def _copy(value: Any) -> Any:
    """Copy JSON-shaped values without importing a serialization framework."""

    if isinstance(value, Mapping):
        return {str(key): _copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy(item) for item in value]
    if isinstance(value, tuple):
        return [_copy(item) for item in value]
    if isinstance(value, set):
        return sorted(_copy(item) for item in value)
    return value


def _redact_tokens(value: Any) -> Any:
    """Remove secret-bearing token fields from an exported JSON-shaped value."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        redacted = False
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower() in _SECRET_KEYS:
                redacted = True
                continue
            result[key_text] = _redact_tokens(item)
        if redacted:
            result["token_redacted"] = True
        return result
    if isinstance(value, list):
        return [_redact_tokens(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_tokens(item) for item in value]
    if isinstance(value, set):
        return sorted(_redact_tokens(item) for item in value)
    return value


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _as_list(value: Any) -> list[str] | None:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return None
    if any(not isinstance(item, str) for item in value):
        return None
    return list(value)


def _is_identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 128


def _same_lane(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    return all(
        _is_identifier(left.get(field)) and left.get(field) == right.get(field)
        for field in ("site_id", "host_id", "lane_id")
    )


def _failure_message(value: Any, fallback: str) -> str:
    if isinstance(value, Mapping):
        message = value.get("message") or value.get("error") or value.get("code")
        if isinstance(message, str) and message:
            return message
    if isinstance(value, str) and value:
        return value
    return fallback


@dataclass(frozen=True)
class LaneObservation:
    """External lane facts used by selection.

    Compatibility and booking are inputs from external configuration or the
    authority.  This type contains no machine or model preference logic.
    """

    lane_id: str
    host_id: str | None = None
    enabled: bool | None = None
    booked: bool | None = None
    compatible: bool | None = None
    reachability: str | None = None
    state: str | None = None

    @classmethod
    def from_value(cls, lane_id: str, value: Mapping[str, object] | "LaneObservation" | None) -> "LaneObservation":
        if isinstance(value, cls):
            return value
        data = dict(value) if isinstance(value, Mapping) else {}
        enabled = data.get("enabled") if isinstance(data.get("enabled"), bool) else None
        booked = data.get("booked") if isinstance(data.get("booked"), bool) else None
        compatible = data.get("compatible") if isinstance(data.get("compatible"), bool) else None
        reachability = data.get("reachability") if isinstance(data.get("reachability"), str) else None
        state = data.get("state") if isinstance(data.get("state"), str) else None
        return cls(
            lane_id=lane_id,
            host_id=data.get("host_id") if isinstance(data.get("host_id"), str) else None,
            enabled=enabled,
            booked=booked,
            compatible=compatible,
            reachability=reachability,
            state=state,
        )


@dataclass(frozen=True)
class ChatSelection:
    """An explicit, already-authorized selection supplied by a caller."""

    pipeline_ref: str
    purpose: str
    compatible_lanes: frozenset[str] | None = None
    authorized: bool = True
    version: str = "1.0.0"
    revision: int = 1
    policy_hash: str = ZERO_SHA256
    image_digest: str = ZERO_SHA256
    manifest_hash: str | None = None
    unit: str | None = None
    invocation: str | None = None

    @classmethod
    def from_value(
        cls,
        value: "ChatSelection | Mapping[str, object] | str",
        *,
        purpose: str | None = None,
        authorized: bool | None = None,
    ) -> "ChatSelection":
        if isinstance(value, cls):
            if purpose is None and authorized is None:
                return value
            return cls(
                pipeline_ref=value.pipeline_ref,
                purpose=purpose if purpose is not None else value.purpose,
                compatible_lanes=value.compatible_lanes,
                authorized=value.authorized if authorized is None else authorized,
                version=value.version,
                revision=value.revision,
                policy_hash=value.policy_hash,
                image_digest=value.image_digest,
                manifest_hash=value.manifest_hash,
                unit=value.unit,
                invocation=value.invocation,
            )
        if isinstance(value, str):
            return cls(
                pipeline_ref=value,
                purpose=purpose or "interactive inference",
                authorized=True if authorized is None else authorized,
            )
        data = dict(value)
        compatible = data.get("compatible_lanes")
        compatible_set = None
        if compatible is not None:
            values = _as_list(compatible)
            compatible_set = frozenset(values or ())
        return cls(
            pipeline_ref=str(data.get("pipeline_ref", data.get("pipeline_id", ""))),
            purpose=str(data.get("purpose", purpose or "interactive inference")),
            compatible_lanes=compatible_set,
            authorized=bool(data.get("authorized", True if authorized is None else authorized)),
            version=str(data.get("version", "1.0.0")),
            revision=int(data.get("revision", 1)),
            policy_hash=str(data.get("policy_hash", ZERO_SHA256)),
            image_digest=str(data.get("image_digest", ZERO_SHA256)),
            manifest_hash=data.get("manifest_hash") if isinstance(data.get("manifest_hash"), str) else None,
            unit=data.get("unit") if isinstance(data.get("unit"), str) else None,
            invocation=data.get("invocation") if isinstance(data.get("invocation"), str) else None,
        )

    def pipeline_binding(self) -> dict[str, object]:
        return {
            "pipeline_id": self.pipeline_ref,
            "version": self.version,
            "revision": self.revision,
            "purpose": self.purpose,
            "policy_hash": self.policy_hash,
        }


@dataclass
class OperationResult:
    ok: bool
    request_id: str
    state: str
    trace: tuple[str, ...] = ()
    error: str | None = None
    occupant: dict[str, object] | None = None
    response: dict[str, object] | None = None

    def as_dict(self, *, redacted: bool = True) -> dict[str, object]:
        result: dict[str, object] = {
            "ok": self.ok,
            "request_id": self.request_id,
            "state": self.state,
            "trace": list(self.trace),
            "error": self.error,
        }
        if self.occupant is not None:
            result["occupant"] = _copy(self.occupant)
        if self.response is not None:
            result["response"] = _redact_tokens(self.response) if redacted else _copy(self.response)
        if redacted and isinstance(result.get("occupant"), Mapping):
            result["occupant"] = _redact_occupant(result["occupant"])
        return result

    def __getitem__(self, key: str) -> object:
        return self.as_dict()[key]


LoadResult = OperationResult
UnloadResult = OperationResult


class ExecutorHandoff(Protocol):
    """Controller-owned seam for executor-v1 operations."""

    def reserve(self, request: Mapping[str, object]) -> Mapping[str, object]: ...

    def start(self, request: Mapping[str, object]) -> Mapping[str, object]: ...

    def inspect(
        self,
        request: Mapping[str, object],
        *,
        unit: str | None = None,
        invocation: str | None = None,
    ) -> Mapping[str, object]: ...

    def stop(self, request: Mapping[str, object]) -> Mapping[str, object]: ...


class SystemdExecutorHandoff:
    """Map executor-v1 requests to injected P0 systemd/GPU seams.

    No privileged operation is discovered or invoked here.  The controller
    supplies the exact unit and invocation in the executor-v1 identity, and
    every returned success includes the two occupancy observations.
    """

    def __init__(self, systemd: Systemd, gpu_probe: GPUProbe, clock: Clock) -> None:
        self.systemd = systemd
        self.gpu_probe = gpu_probe
        self.clock = clock
        self.calls: list[dict[str, object]] = []

    def reserve(self, request: Mapping[str, object]) -> Mapping[str, object]:
        identity = _mapping(request.get("identity"))
        self.calls.append({"method": "reserve", "request": _copy(request)})
        return _executor_reply("reserve", identity, ok=True, observed_state="starting")

    def start(self, request: Mapping[str, object]) -> Mapping[str, object]:
        identity = _mapping(request.get("identity"))
        unit = identity.get("unit")
        invocation = identity.get("invocation")
        self.calls.append({"method": "start", "request": _copy(request)})
        if not _is_identifier(unit) or not _is_identifier(invocation):
            return _executor_reply("start", identity, ok=False, error="missing start identity")
        try:
            raw = self.systemd.start(unit, invocation)
        except Exception as exc:  # pragma: no cover - defensive seam boundary
            return _executor_reply("start", identity, ok=False, uncertain=True, error=str(exc))
        return self._reply_from_systemd("start", identity, raw, default_state="running")

    def inspect(
        self,
        request: Mapping[str, object],
        *,
        unit: str | None = None,
        invocation: str | None = None,
    ) -> Mapping[str, object]:
        identity = _mapping(request.get("identity"))
        unit = unit or (identity.get("unit") if isinstance(identity.get("unit"), str) else None)
        invocation = invocation or (identity.get("invocation") if isinstance(identity.get("invocation"), str) else None)
        self.calls.append({"method": "inspect", "request": _copy(request), "unit": unit, "invocation": invocation})
        if not _is_identifier(unit) or not _is_identifier(invocation):
            return _executor_reply("inspect", identity, ok=False, error="missing inspect identity")
        try:
            raw = self.systemd.inspect(unit, invocation)
        except Exception as exc:  # pragma: no cover - defensive seam boundary
            return _executor_reply("inspect", identity, ok=False, uncertain=True, error=str(exc))
        host = _mapping(identity.get("lane")).get("host_id")
        probe = self._probe(host)
        reply = self._reply_from_systemd("inspect", {**identity, "unit": unit, "invocation": invocation}, raw, default_state="free")
        if probe["uncertain"]:
            return _executor_reply(
                "inspect",
                {**identity, "unit": unit, "invocation": invocation},
                ok=False,
                uncertain=True,
                cgroup_occupants=reply.get("cgroup_occupants", []),
                gpu_tenants=reply.get("gpu_tenants", []),
                error=str(probe["error"]),
            )
        probe_tenants = probe["gpu_tenants"]
        if probe_tenants is not None:
            combined = sorted(set(reply.get("gpu_tenants", [])) | set(probe_tenants))
            reply = dict(reply)
            reply["gpu_tenants"] = combined
            if combined:
                reply["ok"] = False
                reply["uncertain"] = False
                reply["observed_state"] = "quarantined"
                reply["error"] = "GPU occupancy remains"
        return reply

    def stop(self, request: Mapping[str, object]) -> Mapping[str, object]:
        identity = _mapping(request.get("identity"))
        unit = identity.get("unit")
        invocation = identity.get("invocation")
        self.calls.append({"method": "stop", "request": _copy(request)})
        if not _is_identifier(unit) or not _is_identifier(invocation):
            return _executor_reply("stop", identity, ok=False, error="missing stop identity")
        try:
            raw = self.systemd.stop(unit, invocation)
        except Exception as exc:  # pragma: no cover - defensive seam boundary
            return _executor_reply("stop", identity, ok=False, uncertain=True, error=str(exc))
        if not isinstance(raw, Mapping) or raw.get("ok") is not True:
            reply = self._reply_from_systemd("stop", identity, raw, default_state="free")
        else:
            try:
                post_raw = self.systemd.inspect(unit, invocation)
            except Exception as exc:  # pragma: no cover - defensive seam boundary
                return _executor_reply("stop", identity, ok=False, uncertain=True, error=str(exc))
            post_reply = self._reply_from_systemd("inspect", identity, post_raw, default_state="free")
            cgroup = _as_list(post_reply.get("cgroup_occupants")) or []
            gpu = _as_list(post_reply.get("gpu_tenants")) or []
            if post_reply.get("ok") is not True or post_reply.get("uncertain") is not False:
                reply = _executor_reply(
                    "stop",
                    identity,
                    ok=False,
                    uncertain=post_reply.get("uncertain") is True,
                    cgroup_occupants=cgroup,
                    gpu_tenants=gpu,
                    error=_failure_message(post_reply, "post-stop occupancy was unknown"),
                )
            elif cgroup or gpu:
                reply = _executor_reply(
                    "stop",
                    identity,
                    ok=False,
                    observed_state="quarantined",
                    cgroup_occupants=cgroup,
                    gpu_tenants=gpu,
                    error="occupancy remains after stop",
                )
            else:
                reply = _executor_reply("stop", identity, ok=True, observed_state="free")
        probe = self._probe(_mapping(identity.get("lane")).get("host_id"))
        if probe["uncertain"]:
            return _executor_reply(
                "stop",
                identity,
                ok=False,
                uncertain=True,
                cgroup_occupants=_as_list(reply.get("cgroup_occupants")) or (),
                gpu_tenants=_as_list(reply.get("gpu_tenants")) or (),
                error=str(probe["error"]),
            )
        cgroup = _as_list(reply.get("cgroup_occupants")) or []
        gpu = _as_list(reply.get("gpu_tenants")) or []
        probe_tenants = probe["gpu_tenants"]
        if probe_tenants is not None:
            gpu = sorted(set(gpu) | set(probe_tenants))
        if gpu:
            return _executor_reply(
                "stop",
                identity,
                ok=False,
                observed_state="quarantined",
                cgroup_occupants=cgroup,
                gpu_tenants=gpu,
                error="GPU occupancy remains after stop",
            )
        return reply

    def _probe(self, host: object) -> dict[str, object]:
        if not isinstance(host, str) or not host:
            return {"uncertain": True, "error": "missing GPU probe host", "gpu_tenants": None}
        try:
            raw = self.gpu_probe.inspect(host)
        except Exception as exc:  # pragma: no cover - defensive seam boundary
            return {"uncertain": True, "error": str(exc), "gpu_tenants": None}
        if not isinstance(raw, Mapping):
            return {"uncertain": True, "error": "GPU probe returned a non-mapping", "gpu_tenants": None}
        if raw.get("status") in {"unknown", "lost", "timeout", "delayed"} or raw.get("unknown") is True:
            return {"uncertain": True, "error": _failure_message(raw, "GPU probe outcome is unknown"), "gpu_tenants": None}
        if "gpu_tenants" in raw:
            tenants = raw.get("gpu_tenants")
        elif "gpu_occupants" in raw:
            tenants = raw.get("gpu_occupants")
        else:
            count = raw.get("count")
            if isinstance(count, int) and not isinstance(count, bool) and count == 0:
                return {"uncertain": False, "error": None, "gpu_tenants": []}
            return {"uncertain": True, "error": _failure_message(raw, "GPU occupancy was not observed"), "gpu_tenants": None}
        values = _as_list(tenants)
        if values is None:
            return {"uncertain": True, "error": "GPU occupancy is not a string list", "gpu_tenants": None}
        return {"uncertain": False, "error": None, "gpu_tenants": values}

    def _reply_from_systemd(
        self,
        kind: str,
        identity: Mapping[str, object],
        raw: Mapping[str, object] | object,
        *,
        default_state: str,
    ) -> Mapping[str, object]:
        if not isinstance(raw, Mapping):
            return _executor_reply(kind, identity, ok=False, uncertain=True, error="systemd returned a non-mapping")
        cgroup = raw.get("cgroup_occupants") if "cgroup_occupants" in raw else raw.get("occupants")
        gpu = raw.get("gpu_occupants") if "gpu_occupants" in raw else raw.get("gpu_tenants")
        if ("cgroup_occupants" not in raw and "occupants" not in raw) or ("gpu_occupants" not in raw and "gpu_tenants" not in raw):
            return _executor_reply(kind, identity, ok=False, uncertain=True, error="systemd occupancy was not observed")
        cgroup_values = _as_list(cgroup)
        gpu_values = _as_list(gpu)
        status = str(raw.get("status", "success"))
        if cgroup_values is None or gpu_values is None:
            return _executor_reply(kind, identity, ok=False, uncertain=True, error="systemd occupancy is not a string list")
        if raw.get("unit") != identity.get("unit") or raw.get("invocation") != identity.get("invocation"):
            return _executor_reply(
                kind,
                identity,
                ok=False,
                error="executor identity mismatch",
                cgroup_occupants=cgroup_values,
                gpu_tenants=gpu_values,
            )
        if raw.get("ok") is not True or status in _TRANSPORT_FAILURES | {"invocation_mismatch", "failed_stop"}:
            uncertain = status in _TRANSPORT_FAILURES
            return _executor_reply(
                kind,
                identity,
                ok=False,
                uncertain=uncertain,
                error=_failure_message(raw, f"systemd {kind} failed"),
                cgroup_occupants=cgroup_values,
                gpu_tenants=gpu_values,
            )
        if kind == "stop" and (cgroup_values or gpu_values):
            return _executor_reply(
                kind,
                identity,
                ok=False,
                error="occupancy remains after stop",
                observed_state="quarantined",
                cgroup_occupants=cgroup_values,
                gpu_tenants=gpu_values,
            )
        observed_state = "running" if kind == "inspect" and (cgroup_values or gpu_values) else default_state
        return _executor_reply(
            kind,
            identity,
            ok=True,
            observed_state=observed_state,
            cgroup_occupants=cgroup_values,
            gpu_tenants=gpu_values,
        )


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _executor_reply(
    kind: str,
    identity: Mapping[str, object],
    *,
    ok: bool,
    uncertain: bool = False,
    observed_state: str | None = None,
    error: str | None = None,
    cgroup_occupants: Iterable[str] = (),
    gpu_tenants: Iterable[str] = (),
) -> dict[str, object]:
    if observed_state is None:
        observed_state = "unknown" if uncertain else ("running" if kind == "start" else "quarantined")
    acknowledgement = {
        "reserve": "reserved",
        "start": "started",
        "beat": "beat",
        "stop": "stopped" if ok else "unknown",
        "inspect": "inspected",
    }[kind]
    return {
        "schema_version": 1,
        "kind": kind,
        "echoed_identity": _copy(identity),
        "acknowledgement": acknowledgement,
        "ok": ok,
        "observed_state": observed_state,
        "uncertain": uncertain,
        "cgroup_occupants": list(cgroup_occupants),
        "gpu_tenants": list(gpu_tenants),
        "error": None if ok else (error or "executor operation failed"),
    }


def _redact_occupant(record: Mapping[str, object]) -> dict[str, object]:
    result = _copy(record)
    result.pop("token", None)
    result["token_redacted"] = True
    return result


@dataclass
class _Occupant:
    occupant_id: str
    lane: dict[str, object]
    generation: int
    token: str
    instance: str
    principal: dict[str, object]
    pipeline_ref: str
    purpose: str
    loaded_at: str
    last_activity: str
    unit: str
    invocation: str
    deadline: dict[str, object]
    last_activity_monotonic: float
    state: str = "loading"
    active_request_ids: set[str] = field(default_factory=set)
    completed_request_ids: set[str] = field(default_factory=set)
    completed_requests: int = 0
    last_completed_at: str | None = None
    trace: list[str] = field(default_factory=lambda: ["registered/loading"])

    def reservation_state(self) -> str:
        return {
            "loading": "starting",
            "running": "running",
            "draining": "stopping",
            "stopping": "stopping",
            "quarantined": "quarantined",
            "unloaded": "released",
        }.get(self.state, "unknown")

    def record(self, *, redacted: bool) -> dict[str, object]:
        record: dict[str, object] = {
            "schema_version": 1,
            "occupant_id": self.occupant_id,
            "lane": _copy(self.lane),
            "reservation": {"lane": _copy(self.lane), "generation": self.generation, "state": self.reservation_state()},
            "generation": self.generation,
            "instance": self.instance,
            "principal": _copy(self.principal),
            "class": "service",
            "pipeline_ref": self.pipeline_ref,
            "purpose": self.purpose,
            "loaded_at": self.loaded_at,
            "last_activity": self.last_activity,
            "request_accounting": {
                "active_requests": len(self.active_request_ids),
                "completed_requests": self.completed_requests,
                "last_completed_at": self.last_completed_at,
                "activity_basis": "completed-user-request",
            },
            "state": self.state,
            "unit": self.unit,
            "invocation": self.invocation,
            "deadline": _copy(self.deadline),
        }
        if redacted:
            record["token_redacted"] = True
        else:
            record["token"] = self.token
        return record


@dataclass
class _AuthorityCall:
    ok: bool
    status: int | None
    envelope: dict[str, object]
    error: str | None = None


class ChatController:
    """Own one service-class chat occupant and its lifecycle fences."""

    def __init__(
        self,
        clock: Clock,
        transport: Transport,
        systemd: Systemd,
        gpu_probe: GPUProbe,
        inventory: Mapping[str, object] | None = None,
        *,
        authority_endpoint: str | None = None,
        principal: Mapping[str, object] | None = None,
        controller_id: str | None = None,
        executor: ExecutorHandoff | None = None,
        lane_observations: Mapping[str, Mapping[str, object] | LaneObservation] | None = None,
        idle_timeout_s: int = DEFAULT_IDLE_S,
        drain_grace_s: int = DEFAULT_DRAIN_GRACE_S,
        request_timeout_s: float = 10.0,
        ingress: Mapping[str, object] | None = None,
    ) -> None:
        self.clock = clock
        self.transport = transport
        self.systemd = systemd
        self.gpu_probe = gpu_probe
        self.inventory = _copy(inventory or {})
        controller = _mapping(self.inventory.get("controller"))
        self.authority_endpoint = authority_endpoint or (controller.get("endpoint") if isinstance(controller.get("endpoint"), str) else "authority")
        self.controller_id = controller_id or (controller.get("controller_id") if isinstance(controller.get("controller_id"), str) else "controller")
        self.principal = _copy(principal or {"site_id": "site", "tenant_id": "service", "issuer": "local", "subject": "chat"})
        self._ingress_override = _copy(ingress) if ingress is not None else None
        self.executor: ExecutorHandoff = executor or SystemdExecutorHandoff(systemd, gpu_probe, clock)
        self.idle_timeout_s = int(idle_timeout_s)
        self.drain_grace_s = int(drain_grace_s)
        self.request_timeout_s = float(request_timeout_s)
        self._lock = RLock()
        self._counter = 0
        self._current: _Occupant | None = None
        self._pending_lane: str | None = None
        self._drain_deadline: float | None = None
        self._eviction_reason: str | None = None
        self._excluded_lanes: set[str] = set()
        self._load_results: dict[str, OperationResult] = {}
        self._unload_results: dict[str, OperationResult] = {}
        self._last_trace: tuple[str, ...] = ()
        self._lane_observations = self._build_lane_observations(lane_observations)

    @property
    def state(self) -> str:
        with self._lock:
            return self._current.state if self._current is not None else "unavailable"

    @property
    def state_trace(self) -> tuple[str, ...]:
        with self._lock:
            if self._current is not None:
                return tuple(self._current.trace)
            return self._last_trace

    @property
    def occupant(self) -> dict[str, object] | None:
        with self._lock:
            return self._current.record(redacted=False) if self._current is not None else None

    def occupant_record(self, *, redacted: bool = False) -> dict[str, object] | None:
        """Return the in-process occupant-v1 record for callback seams."""

        with self._lock:
            return self._current.record(redacted=redacted) if self._current is not None else None

    def select_model(self, pipeline_ref: str, purpose: str, **kwargs: object) -> ChatSelection:
        """Return an explicit selection; this method never loads anything."""

        return ChatSelection.from_value({"pipeline_ref": pipeline_ref, "purpose": purpose, **kwargs})

    def set_lane_observations(self, observations: Mapping[str, Mapping[str, object] | LaneObservation]) -> None:
        with self._lock:
            self._lane_observations.update(self._normalise_observations(observations))

    def eligible_lanes(
        self,
        selection: ChatSelection | Mapping[str, object] | str | None = None,
        *,
        lane_order: Iterable[str] | None = None,
        availability: Mapping[str, Mapping[str, object] | LaneObservation] | None = None,
    ) -> list[str]:
        selection_obj = ChatSelection.from_value(selection) if selection is not None else None
        compatible = selection_obj.compatible_lanes if selection_obj is not None else None
        observations = self._lane_observations if availability is None else self._normalise_observations(availability)
        order = list(lane_order) if lane_order is not None else self._configured_lane_order()
        with self._lock:
            if self._current is not None or self._pending_lane is not None:
                return []
            result: list[str] = []
            for lane_id in order:
                if lane_id in self._excluded_lanes:
                    continue
                lane = self._lane_record(lane_id)
                if lane is None:
                    continue
                observation = observations.get(lane_id) if availability is not None else LaneObservation.from_value(lane_id, lane)
                if observation is None:
                    continue
                if observation.enabled is not True or observation.booked is not False or observation.compatible is not True:
                    continue
                if compatible is not None and lane_id not in compatible:
                    continue
                if observation.reachability != "confirmed" or observation.state != "free":
                    continue
                host_id = lane.get("host_id")
                if not _is_identifier(host_id):
                    continue
                if observation.host_id is not None and observation.host_id != host_id:
                    continue
                if self._host_reachability(host_id) != "confirmed":
                    continue
                result.append(lane_id)
            return result

    def select_lane(
        self,
        selection: ChatSelection | Mapping[str, object] | str | None = None,
        *,
        lane_order: Iterable[str] | None = None,
        availability: Mapping[str, Mapping[str, object] | LaneObservation] | None = None,
    ) -> str | None:
        lanes = self.eligible_lanes(selection, lane_order=lane_order, availability=availability)
        return lanes[0] if lanes else None

    def load(
        self,
        selection: ChatSelection | Mapping[str, object] | str | None,
        request_id: str | None = None,
        *,
        purpose: str | None = None,
        authorized: bool | None = None,
        lane_order: Iterable[str] | None = None,
        availability: Mapping[str, Mapping[str, object] | LaneObservation] | None = None,
    ) -> LoadResult:
        """Load only after an explicit selection and an authority grant."""

        request_id = request_id or self._new_id("load")
        if request_id in self._load_results:
            return self._load_results[request_id]
        if selection is None:
            return self._remember_load(OperationResult(False, request_id, "excluded", ("excluded",), "explicit selection is required"))
        try:
            selected = ChatSelection.from_value(selection, purpose=purpose, authorized=authorized)
        except (TypeError, ValueError) as exc:
            return self._remember_load(OperationResult(False, request_id, "excluded", ("excluded",), str(exc)))
        if not selected.authorized:
            return self._remember_load(OperationResult(False, request_id, "excluded", ("excluded",), "selection is not authorized"))
        if not selected.pipeline_ref or not selected.purpose:
            return self._remember_load(OperationResult(False, request_id, "excluded", ("excluded",), "selection is incomplete"))

        with self._lock:
            if self._current is not None:
                return self._remember_load(OperationResult(False, request_id, self._current.state, tuple(self._current.trace), "an occupant already exists"))
            if self._pending_lane is not None:
                return self._remember_load(OperationResult(False, request_id, "unavailable", (), "a load is already in progress"))
            lane_id = self.select_lane(selected, lane_order=lane_order, availability=availability)
            if lane_id is None:
                return self._remember_load(OperationResult(False, request_id, "unavailable", (), "no externally eligible lane"))
            self._pending_lane = lane_id

        try:
            call = self._authority_call(
                "chat-load",
                lane_id,
                {"pipeline_ref": selected.pipeline_ref, "purpose": selected.purpose},
                request_id,
                selected,
            )
            if not call.ok:
                if call.status not in {403, 409, 202}:
                    self._excluded_lanes.add(lane_id)
                result = OperationResult(False, request_id, "excluded" if call.status not in {403, 409, 202} else "unavailable", ("excluded",), call.error, response=call.envelope)
                return self._remember_load(result)
            grant = self._validate_grant(call.envelope, lane_id, selected)
            if grant is None:
                self._excluded_lanes.add(lane_id)
                return self._remember_load(OperationResult(False, request_id, "excluded", ("excluded",), "authority grant identity was not usable", response=call.envelope))

            occupant = self._make_occupant(grant, lane_id, selected)
            with self._lock:
                self._current = occupant
                occupant.trace = ["registered/loading"]

            reserve_reply = self._executor_call("reserve", self._reserve_request(occupant, request_id))
            if not self._valid_executor_reply(reserve_reply, "reserve", occupant, require_empty=True):
                return self._load_failure(request_id, lane_id, occupant, _failure_message(reserve_reply, "executor reservation failed"))
            start_reply = self._executor_call("start", self._start_request(occupant, selected, request_id))
            if not self._valid_executor_reply(start_reply, "start", occupant):
                return self._load_failure(request_id, lane_id, occupant, _failure_message(start_reply, "executor start failed"))

            with self._lock:
                occupant.state = "running"
                occupant.last_activity = _iso(self.clock.utc())
                occupant.last_activity_monotonic = self.clock.monotonic()
                occupant.trace.append("running")
            result = OperationResult(True, request_id, "running", tuple(occupant.trace), occupant=occupant.record(redacted=False), response=call.envelope)
            return self._remember_load(result)
        finally:
            with self._lock:
                self._pending_lane = None

    def _load_failure(self, request_id: str, lane_id: str, occupant: _Occupant, error: str) -> LoadResult:
        with self._lock:
            occupant.state = "quarantined"
            occupant.trace.append("excluded")
            self._excluded_lanes.add(lane_id)
        return self._remember_load(OperationResult(False, request_id, "excluded", tuple(occupant.trace), error, occupant=occupant.record(redacted=False)))

    def begin_request(self, request_id: str, *, generation: int | None = None, occupant_id: str | None = None) -> bool:
        with self._lock:
            occupant = self._current
            if occupant is None or occupant.state != "running":
                return False
            if not self._matches(occupant, generation=generation, occupant_id=occupant_id):
                return False
            if request_id in occupant.active_request_ids or request_id in occupant.completed_request_ids:
                return False
            occupant.active_request_ids.add(request_id)
            return True

    request_started = begin_request
    on_request_start = begin_request

    def complete_request(self, request_id: str, *, generation: int | None = None, occupant_id: str | None = None) -> bool:
        with self._lock:
            occupant = self._current
            if occupant is None or not self._matches(occupant, generation=generation, occupant_id=occupant_id):
                return False
            if request_id not in occupant.active_request_ids or request_id in occupant.completed_request_ids:
                return False
            occupant.active_request_ids.remove(request_id)
            occupant.completed_request_ids.add(request_id)
            occupant.completed_requests += 1
            now = self.clock.utc()
            occupant.last_completed_at = _iso(now)
            occupant.last_activity = occupant.last_completed_at
            occupant.last_activity_monotonic = self.clock.monotonic()
            return True

    request_completed = complete_request
    on_request_complete = complete_request
    request_error = complete_request
    on_request_error = complete_request
    request_disconnected = complete_request
    on_disconnect = complete_request
    stream_started = begin_request
    on_stream_start = begin_request
    stream_completed = complete_request
    on_stream_complete = complete_request

    def apply_request_event(
        self,
        event: str,
        request_id: str,
        *,
        generation: int | None = None,
        occupant_id: str | None = None,
    ) -> dict[str, object] | None:
        """Consume a callback event and produce the current occupant record.

        This is intentionally an in-process callback seam.  It does not map
        to an authority RPC operation.
        """

        if event in {"start", "begin", "stream-start"}:
            changed = self.begin_request(request_id, generation=generation, occupant_id=occupant_id)
        elif event in {"complete", "end", "error", "disconnect", "stream-end"}:
            changed = self.complete_request(request_id, generation=generation, occupant_id=occupant_id)
        else:
            return None
        return self.occupant_record(redacted=False) if changed else None

    def accounting(self, *, generation: int | None = None, occupant_id: str | None = None) -> dict[str, object]:
        with self._lock:
            occupant = self._current
            if occupant is None or not self._matches(occupant, generation=generation, occupant_id=occupant_id):
                return {"active_requests": 0, "completed_requests": 0, "last_completed_at": None, "activity_basis": "completed-user-request"}
            return _copy(occupant.record(redacted=True)["request_accounting"])

    def health_check(self) -> dict[str, object]:
        """Return status without affecting user-request inactivity accounting."""

        return self.public_status()

    health = health_check

    def connection_opened(self, connection_id: str | None = None) -> bool:
        return self.state == "running"

    connection_closed = connection_opened
    on_reconnect = connection_opened

    def unit_restarted(self) -> dict[str, object]:
        with self._lock:
            if self._current is not None and self._current.state == "running":
                self._current.state = "quarantined"
                self._current.trace.append("unavailable")
                self._excluded_lanes.add(str(self._current.lane.get("lane_id", "")))
            return self.public_status()

    on_unit_restart = unit_restarted

    def idle_due(self) -> bool:
        with self._lock:
            occupant = self._current
            if occupant is None or occupant.state != "running" or occupant.active_request_ids:
                return False
            return self.clock.monotonic() - occupant.last_activity_monotonic >= self.idle_timeout_s

    def poll_idle(self, request_id: str | None = None) -> UnloadResult | OperationResult:
        if not self.idle_due():
            return OperationResult(False, request_id or "idle-poll", self.state, (), "idle threshold not reached")
        return self.unload(reason="idle", request_id=request_id)

    poll = poll_idle

    def request_eviction(
        self,
        trigger_class: str,
        *,
        reason: str | None = None,
        grace_s: int | None = None,
    ) -> OperationResult:
        with self._lock:
            occupant = self._current
            if occupant is None:
                return OperationResult(False, "eviction", "unavailable", (), "no occupant")
            if trigger_class not in _ELIGIBLE_EVICTORS or occupant.state not in {"running", "draining"}:
                return OperationResult(False, "eviction", occupant.state, tuple(occupant.trace), "eviction is not authorized")
            occupant.state = "draining"
            occupant.trace.append("draining")
            self._eviction_reason = reason or trigger_class
            self._drain_deadline = self.clock.monotonic() + (self.drain_grace_s if grace_s is None else int(grace_s))
            return OperationResult(False, "eviction", "draining", tuple(occupant.trace), "draining until grace deadline")

    begin_eviction = request_eviction

    def process_eviction(self, request_id: str | None = None) -> UnloadResult | OperationResult:
        with self._lock:
            occupant = self._current
            deadline = self._drain_deadline
            if occupant is None or occupant.state != "draining":
                return OperationResult(False, request_id or "eviction", self.state, (), "eviction is not pending")
            if deadline is None or self.clock.monotonic() < deadline:
                return OperationResult(False, request_id or "eviction", "draining", tuple(occupant.trace), "drain grace is active")
            occupant_id, generation = occupant.occupant_id, occupant.generation
        return self.unload(occupant_id=occupant_id, generation=generation, reason=self._eviction_reason or "eviction", force=True, request_id=request_id)

    drain = process_eviction

    def unload(
        self,
        occupant_id: str | None = None,
        generation: int | None = None,
        *,
        reason: str = "operator",
        force: bool = False,
        request_id: str | None = None,
    ) -> UnloadResult:
        request_id = request_id or self._new_id("unload")
        if request_id in self._unload_results:
            return self._unload_results[request_id]
        with self._lock:
            occupant = self._current
            if occupant is None:
                return self._remember_unload(OperationResult(False, request_id, "unavailable", (), "no occupant"))
            if not self._matches(occupant, generation=generation, occupant_id=occupant_id):
                return self._remember_unload(OperationResult(False, request_id, occupant.state, tuple(occupant.trace), "stale occupant identity"))
            if occupant.active_request_ids and not force:
                return self._remember_unload(OperationResult(False, request_id, occupant.state, tuple(occupant.trace), "active requests are not drained"))
            if occupant.state not in {"running", "draining", "stopping", "quarantined"}:
                return self._remember_unload(OperationResult(False, request_id, occupant.state, tuple(occupant.trace), "occupant is not unloadable"))
            occupant.state = "stopping"
            if not occupant.trace or occupant.trace[-1] != "stopping":
                occupant.trace.append("stopping")
            current = occupant

        selection = ChatSelection(pipeline_ref=current.pipeline_ref, purpose=current.purpose)
        call = self._authority_call(
            "chat-unload",
            str(current.lane.get("lane_id", "")),
            {"occupant_id": current.occupant_id, "generation": current.generation},
            request_id,
            selection,
        )
        if not call.ok or not self._validate_unload_authority(call.envelope, current):
            return self._unload_failure(request_id, current, _failure_message(call.envelope, call.error or "authority unload was not confirmed"), call.envelope)

        inspect_request = self._inspect_request(current, request_id)
        inspection = self._executor_inspect(inspect_request, current.unit, current.invocation)
        if not self._valid_executor_reply(inspection, "inspect", current, require_empty=False, exact_target=True):
            return self._unload_failure(request_id, current, _failure_message(inspection, "cleanup inspection was unknown"), call.envelope)
        cgroup = _as_list(inspection.get("cgroup_occupants"))
        gpu = _as_list(inspection.get("gpu_tenants"))
        if cgroup is None or gpu is None:
            return self._unload_failure(request_id, current, "cleanup occupancy was not observable", call.envelope)

        stop_request = self._stop_request(current, request_id)
        stopped = self._executor_call("stop", stop_request)
        if not self._valid_executor_reply(stopped, "stop", current, require_empty=True, exact_target=True):
            return self._unload_failure(request_id, current, _failure_message(stopped, "cleanup stop was not verified"), call.envelope)

        with self._lock:
            current.state = "unloaded"
            current.trace.append("unloaded")
            self._last_trace = tuple(current.trace)
            self._excluded_lanes.discard(str(current.lane.get("lane_id", "")))
            self._current = None
            self._drain_deadline = None
            self._eviction_reason = None
        return self._remember_unload(OperationResult(True, request_id, "unloaded", tuple(current.trace), response=call.envelope))

    def _unload_failure(
        self,
        request_id: str,
        occupant: _Occupant,
        error: str,
        response: Mapping[str, object] | None,
    ) -> UnloadResult:
        with self._lock:
            occupant.state = "quarantined"
            occupant.trace.append("quarantined")
            self._excluded_lanes.add(str(occupant.lane.get("lane_id", "")))
        result = OperationResult(False, request_id, "quarantined", tuple(occupant.trace), error, occupant=occupant.record(redacted=False), response=_copy(response) if response else None)
        return self._remember_unload(result)

    def public_status(self) -> dict[str, object]:
        with self._lock:
            if self._current is None:
                return {"available": False, "state": "unavailable", "generation": None, "occupant": None, "accounting": {"active_requests": 0, "completed_requests": 0, "last_completed_at": None, "activity_basis": "completed-user-request"}}
            occupant = self._current
            return {
                "available": occupant.state == "running",
                "state": occupant.state,
                "generation": occupant.generation,
                "occupant": occupant.record(redacted=True),
                "accounting": _copy(occupant.record(redacted=True)["request_accounting"]),
            }

    status = public_status

    def public_occupancy(self) -> dict[str, object] | None:
        with self._lock:
            if self._current is None:
                return None
            occupant = self._current
            state = {"loading": "starting", "running": "running", "draining": "stopping", "stopping": "stopping", "quarantined": "quarantined"}.get(occupant.state, "unknown")
            return {
                "kind": "occupancy",
                "lane": _copy(occupant.lane),
                "state": state,
                "generation": occupant.generation,
                "lease": None,
                "occupant": occupant.record(redacted=True),
                "observation": {"certainty": "confirmed", "reason": None},
            }

    occupancy = public_occupancy

    def _build_lane_observations(self, supplied: Mapping[str, Mapping[str, object] | LaneObservation] | None) -> dict[str, LaneObservation]:
        observations: dict[str, LaneObservation] = {}
        for lane_id in self._configured_lane_order():
            lane = self._lane_record(lane_id)
            observations[lane_id] = LaneObservation.from_value(lane_id, lane)
        if supplied:
            observations.update(self._normalise_observations(supplied))
        return observations

    def _normalise_observations(self, values: Mapping[str, Mapping[str, object] | LaneObservation]) -> dict[str, LaneObservation]:
        return {str(lane_id): LaneObservation.from_value(str(lane_id), value) for lane_id, value in values.items()}

    def _configured_lane_order(self) -> list[str]:
        configured = self.inventory.get("chat_lane_order")
        if isinstance(configured, list) and all(isinstance(item, str) for item in configured):
            return list(configured)
        lanes = self.inventory.get("lanes")
        if isinstance(lanes, list):
            return [str(item.get("lane_id")) for item in lanes if isinstance(item, Mapping) and isinstance(item.get("lane_id"), str)]
        return []

    def _lane_record(self, lane_id: str) -> dict[str, object] | None:
        lanes = self.inventory.get("lanes")
        if isinstance(lanes, list):
            for item in lanes:
                if isinstance(item, Mapping) and item.get("lane_id") == lane_id:
                    return dict(item)
        return None

    def _host_reachability(self, host_id: str) -> str:
        hosts = self.inventory.get("hosts")
        if not isinstance(hosts, list):
            return "unknown"
        for host in hosts:
            if isinstance(host, Mapping) and host.get("host_id") == host_id:
                return host.get("reachability") if isinstance(host.get("reachability"), str) else "unknown"
        return "unknown"

    def _new_id(self, prefix: str) -> str:
        with self._lock:
            self._counter += 1
            return f"{prefix}-{self._counter}"

    def _remember_load(self, result: OperationResult) -> OperationResult:
        with self._lock:
            self._load_results.setdefault(result.request_id, result)
            return self._load_results[result.request_id]

    def _remember_unload(self, result: OperationResult) -> OperationResult:
        with self._lock:
            self._unload_results.setdefault(result.request_id, result)
            return self._unload_results[result.request_id]

    def _matches(self, occupant: _Occupant, *, generation: int | None, occupant_id: str | None) -> bool:
        return (generation is None or generation == occupant.generation) and (occupant_id is None or occupant_id == occupant.occupant_id)

    def _authority_call(
        self,
        op: str,
        lane_id: str,
        args: Mapping[str, object],
        request_id: str,
        selection: ChatSelection,
    ) -> _AuthorityCall:
        message = self._rpc_request(op, lane_id, args, request_id, selection)
        try:
            raw = self.transport.request(self.authority_endpoint, message, self.request_timeout_s)
        except Exception as exc:  # fail closed at the transport boundary
            return _AuthorityCall(False, None, {}, str(exc))
        if not isinstance(raw, Mapping):
            return _AuthorityCall(False, None, {}, "authority returned a non-mapping")
        nested = raw.get("response")
        envelope = dict(nested) if isinstance(nested, Mapping) else dict(raw)
        status_value = envelope.get("status")
        status = status_value if isinstance(status_value, int) else None
        if raw.get("status") in _TRANSPORT_FAILURES or status in {202, 403, 409, 503}:
            return _AuthorityCall(False, status, envelope, _failure_message(envelope, _failure_message(raw, "authority did not confirm the operation")))
        if status != 200:
            return _AuthorityCall(False, status, envelope, "authority response was not a complete success")
        if envelope.get("request_id") != request_id:
            return _AuthorityCall(False, status, envelope, "authority response request identity mismatch")
        return _AuthorityCall(True, status, envelope)

    def _rpc_request(
        self,
        op: str,
        lane_id: str,
        args: Mapping[str, object],
        request_id: str,
        selection: ChatSelection,
    ) -> dict[str, object]:
        admission = self._admission(selection)
        body = {"schema": 1, "request_id": request_id, "op": op, "lane": lane_id, "args": _copy(args), "admission": admission}
        body["idempotency_scope"] = {"scope": "authenticated-principal", "controller_id": self.controller_id}
        body["request_fingerprint"] = _json_hash(body)
        return body

    def _admission(self, selection: ChatSelection) -> dict[str, object]:
        if self._ingress_override is not None:
            ingress = _copy(self._ingress_override)
        else:
            ingress = {
                "actor": _copy(self.principal),
                "subject": None,
                "controller_id": self.controller_id,
                "authenticated_peer": str(self.principal.get("subject", "chat")),
                "peer_source": "socket-peer",
                "auth_method": "local",
                "transport_binding": "transport-independent",
                "peer_verified": True,
                "forwarding_headers_ignored": True,
                "operator_elevation": "none",
            }
        return {
            "execution": "atomic",
            "approval": {"approval_id": None, "required": False, "consume_atomically": True},
            "pipeline": selection.pipeline_binding(),
            "delegation": None,
            "ingress": ingress,
            "batch": None,
        }

    def _validate_grant(self, envelope: Mapping[str, object], lane_id: str, selection: ChatSelection) -> dict[str, object] | None:
        data = envelope.get("data")
        if not isinstance(data, Mapping) or data.get("kind") != "grant" or data.get("operation") != "chat-load":
            return None
        token = data.get("token")
        generation = data.get("generation")
        lease = data.get("lease")
        reservation = data.get("reservation")
        adoption = data.get("adoption")
        if not isinstance(token, str) or len(token) < 16 or not isinstance(generation, int) or generation < 1 or not isinstance(lease, Mapping) or not isinstance(reservation, Mapping) or not isinstance(adoption, Mapping):
            return None
        lease_lane = _mapping(lease.get("lane"))
        lease_reservation = _mapping(lease.get("reservation"))
        reservation_lane = _mapping(reservation.get("lane"))
        configured_lane = self._lane_record(lane_id) or {}
        expected_site = self.inventory.get("site_id")
        expected_host = configured_lane.get("host_id")
        expected_lane = {"site_id": expected_site, "host_id": expected_host, "lane_id": lane_id}
        if not _same_lane(lease_lane, expected_lane) or not _same_lane(reservation_lane, lease_lane) or lease.get("generation") != generation or lease_reservation.get("generation") != generation:
            return None
        if lease.get("token") not in {None, token}:
            return None
        if lease.get("class") != "service" or lease.get("state") != "starting" or lease_reservation.get("state") not in {"starting", "reserved"}:
            return None
        if adoption.get("mode") != "fresh-acquire" or adoption.get("principal_bound") is not True or adoption.get("generation_bound") is not True:
            return None
        if not _is_identifier(lease.get("lease_id")) or not _is_identifier(lease.get("instance")) or not _is_identifier(lease.get("unit")) or not _is_identifier(lease.get("invocation")):
            return None
        principal = lease.get("principal")
        if isinstance(principal, Mapping) and principal != self.principal:
            return None
        return {"data": _copy(data), "lease": _copy(lease), "reservation": _copy(reservation), "token": token, "generation": generation}

    def _make_occupant(self, grant: Mapping[str, object], lane_id: str, selection: ChatSelection) -> _Occupant:
        lease = _mapping(grant.get("lease"))
        lane = _mapping(lease.get("lane"))
        now = self.clock.utc()
        monotonic = self.clock.monotonic()
        unit = str(lease["unit"])
        invocation = str(lease["invocation"])
        loaded = _iso(now)
        deadline = {
            "boot_id": self.clock.boot_id(),
            "deadline_s": monotonic + self.idle_timeout_s,
            "utc_anchor": loaded,
            "monotonic_anchor_s": monotonic,
        }
        return _Occupant(
            occupant_id=str(grant["data"].get("occupant_id", lease["lease_id"])),
            lane=lane or {"site_id": str(self.principal.get("site_id", "site")), "host_id": "unknown", "lane_id": lane_id},
            generation=int(grant["generation"]),
            token=str(grant["token"]),
            instance=str(lease["instance"]),
            principal=_copy(lease.get("principal", self.principal)),
            pipeline_ref=selection.pipeline_ref,
            purpose=selection.purpose,
            loaded_at=loaded,
            last_activity=loaded,
            unit=unit,
            invocation=invocation,
            deadline=deadline,
            last_activity_monotonic=monotonic,
        )

    def _executor_call(self, method: str, request: Mapping[str, object]) -> Mapping[str, object]:
        operation = getattr(self.executor, method, None)
        if not callable(operation):
            return _executor_reply(str(request.get("kind", method)), _mapping(request.get("identity")), ok=False, error=f"executor handoff lacks {method}")
        try:
            result = operation(request)
        except Exception as exc:  # fail closed at the injected handoff boundary
            return _executor_reply(str(request.get("kind", method)), _mapping(request.get("identity")), ok=False, uncertain=True, error=str(exc))
        return dict(result) if isinstance(result, Mapping) else _executor_reply(str(request.get("kind", method)), _mapping(request.get("identity")), ok=False, uncertain=True, error="executor returned a non-mapping")

    def _executor_inspect(self, request: Mapping[str, object], unit: str, invocation: str) -> Mapping[str, object]:
        operation = getattr(self.executor, "inspect", None)
        if not callable(operation):
            return _executor_reply("inspect", _mapping(request.get("identity")), ok=False, error="executor handoff lacks inspect")
        try:
            parameters = python_inspect.signature(operation).parameters
            if "unit" in parameters or "invocation" in parameters:
                result = operation(request, unit=unit, invocation=invocation)
            else:
                result = operation(request)
        except Exception as exc:  # fail closed at the injected handoff boundary
            return _executor_reply("inspect", _mapping(request.get("identity")), ok=False, uncertain=True, error=str(exc))
        return dict(result) if isinstance(result, Mapping) else _executor_reply("inspect", _mapping(request.get("identity")), ok=False, uncertain=True, error="executor returned a non-mapping")

    def _valid_executor_reply(
        self,
        reply: Mapping[str, object],
        kind: str,
        occupant: _Occupant,
        *,
        require_empty: bool = False,
        exact_target: bool = False,
    ) -> bool:
        if (
            reply.get("schema_version") != 1
            or reply.get("kind") != kind
            or reply.get("acknowledgement") != _EXECUTOR_ACKNOWLEDGEMENTS[kind]
            or reply.get("ok") is not True
            or reply.get("uncertain") is not False
            or reply.get("error") is not None
        ):
            return False
        identity = _mapping(reply.get("echoed_identity"))
        if not all(field in identity for field in ("lane", "generation", "token", "instance", "unit", "invocation")):
            return False
        lane = _mapping(identity.get("lane"))
        if not _same_lane(lane, occupant.lane) or identity.get("generation") != occupant.generation or identity.get("instance") != occupant.instance:
            return False
        if kind == "inspect" and identity.get("token") is not None:
            return False
        if kind != "inspect" and identity.get("token") != occupant.token:
            return False
        if kind == "reserve" and (identity.get("unit") is not None or identity.get("invocation") is not None):
            return False
        if (exact_target or kind in {"start", "stop", "inspect"}) and (identity.get("unit") != occupant.unit or identity.get("invocation") != occupant.invocation):
            return False
        observed_state = reply.get("observed_state")
        if not isinstance(observed_state, str) or observed_state == "unknown":
            return False
        if kind == "reserve" and reply.get("observed_state") not in {"starting", "running"}:
            return False
        if kind == "start" and reply.get("observed_state") != "running":
            return False
        if kind == "inspect" and reply.get("observed_state") not in {"free", "starting", "running", "stopping", "quarantined"}:
            return False
        if kind == "stop" and reply.get("observed_state") != "free":
            return False
        cgroup = _as_list(reply.get("cgroup_occupants"))
        gpu = _as_list(reply.get("gpu_tenants"))
        if cgroup is None or gpu is None:
            return False
        if require_empty:
            if cgroup or gpu:
                return False
        return True

    def _deadline(self, occupant: _Occupant, kind: str = "heartbeat") -> dict[str, object]:
        deadline = _copy(occupant.deadline)
        deadline["kind"] = kind
        deadline["owner_class"] = "service"
        return deadline

    def _execution_policy(self, occupant: _Occupant) -> dict[str, object]:
        end = self.clock.utc() + timedelta(days=365)
        return {"class": "service", "protected": False, "preemptible": True, "grace_s": self.drain_grace_s, "max_end": _iso(end), "deadline_kind": "heartbeat"}

    def _reserve_request(self, occupant: _Occupant, request_id: str) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "reserve",
            "controller_request_id": request_id,
            "execution_policy": self._execution_policy(occupant),
            "identity": {"lane": _copy(occupant.lane), "generation": occupant.generation, "token": occupant.token, "instance": occupant.instance, "unit": None, "invocation": None, "deadline": self._deadline(occupant)},
        }

    def _start_request(self, occupant: _Occupant, selection: ChatSelection, request_id: str) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "start",
            "controller_request_id": request_id,
            "execution_policy": self._execution_policy(occupant),
            "reservation_acknowledged": True,
            "identity": {"lane": _copy(occupant.lane), "generation": occupant.generation, "token": occupant.token, "instance": occupant.instance, "unit": occupant.unit, "invocation": occupant.invocation, "deadline": self._deadline(occupant)},
            "workload": {"workload_id": occupant.occupant_id, "class": "service", "image_digest": selection.image_digest, "parameters_hash": _json_hash({"pipeline_ref": selection.pipeline_ref, "purpose": selection.purpose}), "manifest_hash": selection.manifest_hash},
        }

    def _inspect_request(self, occupant: _Occupant, request_id: str) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "inspect",
            "controller_request_id": request_id,
            "execution_policy": self._execution_policy(occupant),
            "identity": {"lane": _copy(occupant.lane), "generation": occupant.generation, "token": None, "instance": occupant.instance, "unit": None, "invocation": None, "deadline": None},
        }

    def _stop_request(self, occupant: _Occupant, request_id: str) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "stop",
            "controller_request_id": request_id,
            "execution_policy": self._execution_policy(occupant),
            "identity": {"lane": _copy(occupant.lane), "generation": occupant.generation, "token": occupant.token, "instance": occupant.instance, "unit": occupant.unit, "invocation": occupant.invocation, "deadline": self._deadline(occupant, "grace")},
            "stop_authority": {"mode": "controller-match", "approval_id": None},
        }

    def _validate_unload_authority(self, envelope: Mapping[str, object], occupant: _Occupant) -> bool:
        data = envelope.get("data")
        if not isinstance(data, Mapping) or data.get("kind") != "mutation" or data.get("record_type") != "occupant":
            return False
        if data.get("operation") != "chat-unload" or data.get("record_id") != occupant.occupant_id or data.get("state") not in {"stopping", "unloaded"}:
            return False
        reservation = _mapping(data.get("reservation"))
        reservation_lane = _mapping(reservation.get("lane"))
        if reservation.get("generation") != occupant.generation or reservation.get("state") not in {"stopping", "released"}:
            return False
        if not _same_lane(reservation_lane, occupant.lane):
            return False
        return True


class ChatOccupancy(ChatController):
    """Compatibility name for callers focused on occupancy rather than loading."""


class ChatService(ChatController):
    """Compatibility name for the service-facing controller."""


class ChatAdapter:
    """Small callback adapter used by request/stream handlers.

    Health and connection callbacks deliberately do not call request-accounting
    methods.  A request is counted only when a user request or stream begins,
    and is completed once by the corresponding terminal callback.
    """

    def __init__(self, controller: ChatController, *, generation: int | None = None, occupant_id: str | None = None) -> None:
        self.controller = controller
        self.generation = generation
        self.occupant_id = occupant_id

    def request_started(self, request_id: str) -> bool:
        return self.controller.begin_request(request_id, generation=self.generation, occupant_id=self.occupant_id)

    on_request_start = request_started

    def request_completed(self, request_id: str) -> bool:
        return self.controller.complete_request(request_id, generation=self.generation, occupant_id=self.occupant_id)

    on_request_complete = request_completed

    def request_error(self, request_id: str) -> bool:
        return self.controller.complete_request(request_id, generation=self.generation, occupant_id=self.occupant_id)

    on_request_error = request_error

    def disconnected(self, request_id: str) -> bool:
        return self.controller.complete_request(request_id, generation=self.generation, occupant_id=self.occupant_id)

    on_disconnect = disconnected

    def stream_started(self, request_id: str) -> bool:
        return self.controller.begin_request(request_id, generation=self.generation, occupant_id=self.occupant_id)

    on_stream_start = stream_started

    def stream_completed(self, request_id: str) -> bool:
        return self.controller.complete_request(request_id, generation=self.generation, occupant_id=self.occupant_id)

    on_stream_complete = stream_completed

    def health_check(self) -> dict[str, object]:
        return self.controller.health_check()

    def connected(self) -> bool:
        return self.controller.connection_opened()

    @contextmanager
    def user_request(self, request_id: str) -> Any:
        started = self.request_started(request_id)
        if not started:
            raise RuntimeError("chat occupant is not accepting the request")
        try:
            yield
        except BaseException:
            self.request_error(request_id)
            raise
        else:
            self.request_completed(request_id)


# The module is intentionally explicit about its public names so callers do
# not need to know whether they are using the service or occupancy vocabulary.
__all__ = [
    "ChatAdapter",
    "ChatController",
    "ChatOccupancy",
    "ChatSelection",
    "ChatService",
    "ExecutorHandoff",
    "LaneObservation",
    "LoadResult",
    "OperationResult",
    "SystemdExecutorHandoff",
    "UnloadResult",
]
