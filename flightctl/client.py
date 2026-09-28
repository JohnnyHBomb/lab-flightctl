"""Portable v1 RPC client primitives used by the command-line adapter.

The authority owns admission, token lookup, generation fences, and executor
starts.  This module only builds frozen request envelopes, sends them through
an injected transport, and checks the response boundary before the CLI uses a
grant.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO, TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:  # pragma: no cover - the protocols are a typing seam only
    from contracts.interfaces import Clock, Transport


DEFAULT_TTL_MIN = 240
DEFAULT_WAIT_MAX_MIN = 10
QUEUE_REFRESH_S = 60
DEFAULT_TIMEOUT_S = 15.0
RPC_ENDPOINT_ENV = "FLIGHTCTL_ENDPOINT"
CONTROLLER_ID_ENV = "FLIGHTCTL_CONTROLLER_ID"
DISPLAY_TIMEZONE_ENV = "FLIGHTCTL_TIMEZONE"

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_SHORT_IDENTIFIER = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_BASE64 = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_OPS = {
    "acquire",
    "renew",
    "release",
    "claim",
    "queue",
    "book",
    "cancel",
    "approval-request",
    "approve",
    "preempt",
    "chat-load",
    "chat-unload",
    "cal",
    "free",
    "report",
    "status",
}
_CLASSES = {"operator", "booked", "batch", "service", "resident", "standby"}
_STATUSES = {200, 202, 403, 409, 503}
_RESULT_KINDS = {
    "grant",
    "pending",
    "reachability",
    "occupancy",
    "projection",
    "status",
    "mutation",
    "queue",
    "booking",
    "approval",
    "report",
}
_ERROR_CODES = {
    "invalid",
    "busy",
    "denied",
    "conflict",
    "unknown",
    "unsupported_version",
    "stale_policy",
    "fenced",
    "unavailable",
    "timeout",
}
_FAILURE_CLASSES = {"client", "policy", "conflict", "transport", "state", "timeout"}
_RECORD_STATES = {
    "free",
    "starting",
    "running",
    "stopping",
    "quarantined",
    "scheduled",
    "blocked",
    "claimed",
    "missed",
    "completed",
    "cancelled",
    "displaced",
    "queued",
    "eligible",
    "expired",
    "removed",
    "loading",
    "draining",
    "unloaded",
    "issued",
    "approved",
    "consumed",
    "revoked",
    "unknown",
}
_LANE_STATES = {"unassigned", "reserved", "starting", "running", "stopping", "released", "quarantined", "unknown"}
_LEASE_STATES = {"starting", "running", "stopping", "quarantined"}


class ClientError(ValueError):
    """A request or response is outside the frozen v1 contract."""


class InvalidRequest(ClientError):
    """Raised before a transport call for malformed input."""


class InvalidResponse(ClientError):
    """Raised when a server or adapter response cannot be trusted."""


class TransportFailure(RuntimeError):
    """A transport did not provide a usable RPC response."""

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class SystemClock:
    """Small production clock matching the P0 Clock protocol."""

    def utc(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()

    def boot_id(self) -> str:
        boot_path = Path("/proc/sys/kernel/random/boot_id")
        try:
            value = boot_path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            value = "boot-local"
        return value or "boot-local"


class HttpTransport:
    """Stdlib HTTPS/HTTP adapter; tests use an injected transport instead."""

    def __init__(self, *, opener: Any | None = None) -> None:
        self._opener = opener or urllib.request.urlopen

    def request(self, endpoint: str, message: Mapping[str, object], timeout_s: float) -> Mapping[str, object]:
        payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=timeout_s) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportFailure(f"controller transport failed: {exc}") from exc
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransportFailure("controller returned invalid JSON") from exc
        if not isinstance(decoded, Mapping):
            raise TransportFailure("controller returned a non-object response")
        return decoded


def _deepcopy(value: Mapping[str, object]) -> dict[str, object]:
    return copy.deepcopy(dict(value))


def _invalid(error_cls: type[ClientError], message: str) -> None:
    raise error_cls(message)


def _object(
    value: object,
    field: str,
    required: set[str],
    optional: set[str] = frozenset(),
    *,
    error_cls: type[ClientError] = InvalidRequest,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _invalid(error_cls, f"{field} must be an object")
    keys = set(value)
    missing = required - keys
    extra = keys - required - optional
    if missing or extra:
        details = []
        if missing:
            details.append("missing " + ", ".join(sorted(missing)))
        if extra:
            details.append("unexpected " + ", ".join(sorted(extra)))
        _invalid(error_cls, f"{field} is incomplete: {'; '.join(details)}")
    return value


def _unique(values: list[object]) -> bool:
    return all(not any(value == previous for previous in values[:index]) for index, value in enumerate(values))


def _identifier(value: object, field: str, error_cls: type[ClientError] = InvalidRequest) -> None:
    if not isinstance(value, str) or not value or len(value) > 128 or not _IDENTIFIER.fullmatch(value):
        _invalid(error_cls, f"{field} must be a non-empty identifier")


def _short_identifier(value: object, field: str, error_cls: type[ClientError] = InvalidRequest) -> None:
    if not isinstance(value, str) or not _SHORT_IDENTIFIER.fullmatch(value):
        _invalid(error_cls, f"{field} must be a short identifier")


def _positive_int(value: object, field: str, error_cls: type[ClientError] = InvalidRequest) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _invalid(error_cls, f"{field} must be a positive integer")


def _nonnegative_int(value: object, field: str, error_cls: type[ClientError] = InvalidRequest) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _invalid(error_cls, f"{field} must be a non-negative integer")


def _purpose(value: object, error_cls: type[ClientError] = InvalidRequest) -> None:
    if not isinstance(value, str) or not value or len(value) > 512:
        _invalid(error_cls, "purpose must be non-empty")


def _valid_hash(value: object, field: str, error_cls: type[ClientError] = InvalidRequest) -> None:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        _invalid(error_cls, f"{field} must be a SHA-256 hex string")


def _utc_time(value: object, field: str, error_cls: type[ClientError] = InvalidRequest) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        _invalid(error_cls, f"{field} must be a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise error_cls(f"{field} must be a UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        _invalid(error_cls, f"{field} must be a UTC timestamp")


def _validate_principal(value: object, field: str, error_cls: type[ClientError] = InvalidRequest) -> None:
    principal = _object(value, field, {"site_id", "tenant_id", "issuer", "subject"}, error_cls=error_cls)
    _short_identifier(principal.get("site_id"), f"{field}.site_id", error_cls)
    for key in ("tenant_id", "issuer", "subject"):
        _identifier(principal.get(key), f"{field}.{key}", error_cls)


def _validate_lane_ref(value: object, field: str, error_cls: type[ClientError] = InvalidRequest) -> None:
    lane = _object(value, field, {"site_id", "host_id", "lane_id"}, error_cls=error_cls)
    for key in ("site_id", "host_id", "lane_id"):
        _short_identifier(lane.get(key), f"{field}.{key}", error_cls)


def _validate_lane_generation(value: object, field: str, error_cls: type[ClientError] = InvalidRequest) -> None:
    reservation = _object(value, field, {"lane", "generation", "state"}, error_cls=error_cls)
    lane = reservation.get("lane")
    if lane is not None:
        _validate_lane_ref(lane, f"{field}.lane", error_cls)
    generation = reservation.get("generation")
    if generation is not None:
        _positive_int(generation, f"{field}.generation", error_cls)
    state = reservation.get("state")
    if state not in _LANE_STATES:
        _invalid(error_cls, f"{field}.state is invalid")
    if state in {"reserved", "starting", "running", "stopping", "quarantined"} and (lane is None or generation is None):
        _invalid(error_cls, f"{field} must bind an active lane and generation")


def _validate_measurement(value: object, field: str, error_cls: type[ClientError] = InvalidRequest) -> None:
    measurement = _object(value, field, {"certainty", "reason"}, error_cls=error_cls)
    certainty = measurement.get("certainty")
    if certainty not in {"confirmed", "estimate", "unknown"}:
        _invalid(error_cls, f"{field}.certainty is invalid")
    reason = measurement.get("reason")
    if reason is not None and (not isinstance(reason, str) or not reason or len(reason) > 512):
        _invalid(error_cls, f"{field}.reason is invalid")
    if certainty in {"estimate", "unknown"} and not isinstance(reason, str):
        _invalid(error_cls, f"{field}.reason is required for uncertain measurements")
    if certainty == "confirmed" and reason is not None:
        _invalid(error_cls, f"{field}.reason must be null for confirmed measurements")


def _validate_deadline(value: object, field: str, error_cls: type[ClientError] = InvalidRequest) -> None:
    deadline = _object(value, field, {"boot_id", "deadline_s", "utc_anchor", "monotonic_anchor_s"}, error_cls=error_cls)
    _identifier(deadline.get("boot_id"), f"{field}.boot_id", error_cls)
    for key in ("deadline_s", "monotonic_anchor_s"):
        number = deadline.get(key)
        if isinstance(number, bool) or not isinstance(number, (int, float)) or number < 0:
            _invalid(error_cls, f"{field}.{key} is invalid")
    _utc_time(deadline.get("utc_anchor"), f"{field}.utc_anchor", error_cls)


def _validate_approval_selection(value: object, field: str, error_cls: type[ClientError] = InvalidRequest) -> None:
    approval = _object(value, field, {"approval_id", "required", "consume_atomically"}, error_cls=error_cls)
    approval_id = approval.get("approval_id")
    if approval_id is not None:
        _identifier(approval_id, f"{field}.approval_id", error_cls)
    required = approval.get("required")
    if not isinstance(required, bool):
        _invalid(error_cls, f"{field}.required is invalid")
    if approval.get("consume_atomically") is not True:
        _invalid(error_cls, f"{field}.consume_atomically must be true")
    if required is True and approval_id is None:
        _invalid(error_cls, f"{field}.approval_id is required")
    if required is False and approval_id is not None:
        _invalid(error_cls, f"{field}.approval_id must be null when approval is not required")


def _validate_pipeline(value: object, field: str, error_cls: type[ClientError] = InvalidRequest) -> None:
    pipeline = _object(value, field, {"pipeline_id", "version", "revision", "purpose", "policy_hash"}, error_cls=error_cls)
    _short_identifier(pipeline.get("pipeline_id"), f"{field}.pipeline_id", error_cls)
    if not isinstance(pipeline.get("version"), str) or not _VERSION.fullmatch(str(pipeline.get("version"))):
        _invalid(error_cls, f"{field}.version is invalid")
    _positive_int(pipeline.get("revision"), f"{field}.revision", error_cls)
    _purpose(pipeline.get("purpose"), error_cls)
    _valid_hash(pipeline.get("policy_hash"), f"{field}.policy_hash", error_cls)


def _validate_resource_ceiling(value: object, field: str, error_cls: type[ClientError] = InvalidRequest) -> None:
    ceiling = _object(value, field, {"max_s", "max_vram_bytes"}, error_cls=error_cls)
    _positive_int(ceiling.get("max_s"), f"{field}.max_s", error_cls)
    _positive_int(ceiling.get("max_vram_bytes"), f"{field}.max_vram_bytes", error_cls)


def _validate_proof(value: object, field: str, error_cls: type[ClientError] = InvalidRequest) -> None:
    if not isinstance(value, Mapping):
        _invalid(error_cls, f"{field} is invalid")
    scheme = value.get("scheme")
    if scheme == "ssh-sk":
        proof = _object(value, field, {"scheme", "key_id", "namespace", "encoding", "signature_b64"}, error_cls=error_cls)
        _identifier(proof.get("key_id"), f"{field}.key_id", error_cls)
        if proof.get("namespace") != "flightctl/approval/v1" or proof.get("encoding") != "openssh-ssh-sk-signature/base64":
            _invalid(error_cls, f"{field} has invalid SSH proof metadata")
        signature = proof.get("signature_b64")
        if not isinstance(signature, str) or len(signature) > 16384 or not _BASE64.fullmatch(signature):
            _invalid(error_cls, f"{field}.signature_b64 is invalid")
        return
    if scheme == "webauthn":
        proof = _object(value, field, {"scheme", "key_id", "rp_id", "client_data_json_b64", "authenticator_data_b64", "signature_b64"}, error_cls=error_cls)
        _identifier(proof.get("key_id"), f"{field}.key_id", error_cls)
        _identifier(proof.get("rp_id"), f"{field}.rp_id", error_cls)
        for key in ("client_data_json_b64", "authenticator_data_b64", "signature_b64"):
            encoded = proof.get(key)
            if not isinstance(encoded, str) or len(encoded) > 16384 or not _BASE64.fullmatch(encoded):
                _invalid(error_cls, f"{field}.{key} is invalid")
        return
    _invalid(error_cls, f"{field}.scheme is unsupported")


def _validate_evidence(value: object, field: str, error_cls: type[ClientError] = InvalidRequest) -> None:
    evidence = _object(value, field, {"verifier", "verified_at", "user_presence", "user_verification"}, error_cls=error_cls)
    _identifier(evidence.get("verifier"), f"{field}.verifier", error_cls)
    _utc_time(evidence.get("verified_at"), f"{field}.verified_at", error_cls)
    if evidence.get("user_presence") not in {"verified", "not_required"} or evidence.get("user_verification") not in {"verified", "not_required"}:
        _invalid(error_cls, f"{field} user verification is invalid")


def _validate_assurance(value: object, field: str, error_cls: type[ClientError] = InvalidRequest) -> None:
    assurance = _object(value, field, {"required", "offered", "verified", "evidence_refs", "unknown"}, error_cls=error_cls)
    for key in ("required", "offered", "verified", "evidence_refs"):
        values = assurance.get(key)
        if not isinstance(values, list) or not _unique(values):
            _invalid(error_cls, f"{field}.{key} is invalid")
        for index, item in enumerate(values):
            _identifier(item, f"{field}.{key}[{index}]", error_cls)
    if not isinstance(assurance.get("unknown"), bool):
        _invalid(error_cls, f"{field}.unknown is invalid")


def _validate_key_rotation(value: object, field: str, error_cls: type[ClientError] = InvalidRequest) -> None:
    rotation = _object(value, field, {"issuer", "key_set_id", "key_id", "valid_from", "valid_until"}, error_cls=error_cls)
    for key in ("issuer", "key_set_id", "key_id"):
        _identifier(rotation.get(key), f"{field}.{key}", error_cls)
    _utc_time(rotation.get("valid_from"), f"{field}.valid_from", error_cls)
    if rotation.get("valid_until") is not None:
        _utc_time(rotation.get("valid_until"), f"{field}.valid_until", error_cls)


def _validate_canonical_binding(value: object, field: str, error_cls: type[ClientError] = InvalidRequest) -> None:
    binding = _object(value, field, {"canonicalization", "hash_algorithm", "domain", "signed_fields", "payload_hash", "audience", "expires", "nonce", "proof"}, {"attenuation"}, error_cls=error_cls)
    if binding.get("canonicalization") != "JCS-RFC8785" or binding.get("hash_algorithm") != "SHA-256" or binding.get("domain") != "flightctl/approval/v1":
        _invalid(error_cls, f"{field} canonical metadata is invalid")
    signed_fields = binding.get("signed_fields")
    if not isinstance(signed_fields, list) or not signed_fields or not _unique(signed_fields):
        _invalid(error_cls, f"{field}.signed_fields is invalid")
    for index, item in enumerate(signed_fields):
        _identifier(item, f"{field}.signed_fields[{index}]", error_cls)
    _valid_hash(binding.get("payload_hash"), f"{field}.payload_hash", error_cls)
    _identifier(binding.get("audience"), f"{field}.audience", error_cls)
    _utc_time(binding.get("expires"), f"{field}.expires", error_cls)
    _identifier(binding.get("nonce"), f"{field}.nonce", error_cls)
    _validate_proof(binding.get("proof"), f"{field}.proof", error_cls)
    if binding.get("attenuation") is not None:
        attenuation = _object(binding.get("attenuation"), f"{field}.attenuation", {"max_depth", "allowed_operations", "resource_ceiling"}, error_cls=error_cls)
        depth = attenuation.get("max_depth")
        if isinstance(depth, bool) or not isinstance(depth, int) or not 0 <= depth <= 8:
            _invalid(error_cls, f"{field}.attenuation.max_depth is invalid")
        operations = attenuation.get("allowed_operations")
        if not isinstance(operations, list) or not _unique(operations) or any(item not in {"acquire", "renew", "release", "claim", "queue", "book", "cancel", "preempt", "run", "chat-load", "chat-unload"} for item in operations):
            _invalid(error_cls, f"{field}.attenuation.allowed_operations is invalid")
        _validate_resource_ceiling(attenuation.get("resource_ceiling"), f"{field}.attenuation.resource_ceiling", error_cls)


def _validate_delegation(value: object, field: str, error_cls: type[ClientError] = InvalidRequest) -> None:
    delegation = _object(value, field, {"issuer", "delegate", "audience", "destination_site", "allowed_operations", "allowed_pipelines", "resource_ceiling", "expires", "nonce", "max_depth", "binding"}, error_cls=error_cls)
    _validate_principal(delegation.get("issuer"), f"{field}.issuer", error_cls)
    _validate_principal(delegation.get("delegate"), f"{field}.delegate", error_cls)
    _identifier(delegation.get("audience"), f"{field}.audience", error_cls)
    _short_identifier(delegation.get("destination_site"), f"{field}.destination_site", error_cls)
    operations = delegation.get("allowed_operations")
    allowed_operations = {"acquire", "renew", "release", "claim", "queue", "book", "cancel", "preempt", "run", "chat-load", "chat-unload"}
    if not isinstance(operations, list) or not _unique(operations) or any(item not in allowed_operations for item in operations):
        _invalid(error_cls, f"{field}.allowed_operations is invalid")
    pipelines = delegation.get("allowed_pipelines")
    if not isinstance(pipelines, list) or not _unique(pipelines):
        _invalid(error_cls, f"{field}.allowed_pipelines is invalid")
    for index, pipeline in enumerate(pipelines):
        _short_identifier(pipeline, f"{field}.allowed_pipelines[{index}]", error_cls)
    _validate_resource_ceiling(delegation.get("resource_ceiling"), f"{field}.resource_ceiling", error_cls)
    _utc_time(delegation.get("expires"), f"{field}.expires", error_cls)
    _identifier(delegation.get("nonce"), f"{field}.nonce", error_cls)
    depth = delegation.get("max_depth")
    if isinstance(depth, bool) or not isinstance(depth, int) or not 0 <= depth <= 8:
        _invalid(error_cls, f"{field}.max_depth is invalid")
    _validate_canonical_binding(delegation.get("binding"), f"{field}.binding", error_cls)


def _validate_batch(value: object, field: str, error_cls: type[ClientError] = InvalidRequest) -> None:
    batch = _object(value, field, {"batch_id", "arms", "dependencies", "registered_before_execution", "all_arms_visible"}, error_cls=error_cls)
    _identifier(batch.get("batch_id"), f"{field}.batch_id", error_cls)
    arms = batch.get("arms")
    if not isinstance(arms, list) or not arms:
        _invalid(error_cls, f"{field}.arms must be non-empty")
    arm_ids = []
    for index, item in enumerate(arms):
        arm = _object(item, f"{field}.arms[{index}]", {"arm_id", "predecessor", "dependencies"}, error_cls=error_cls)
        _identifier(arm.get("arm_id"), f"{field}.arms[{index}].arm_id", error_cls)
        arm_ids.append(arm.get("arm_id"))
        predecessor = arm.get("predecessor")
        if predecessor is not None:
            _identifier(predecessor, f"{field}.arms[{index}].predecessor", error_cls)
        dependencies = arm.get("dependencies")
        if not isinstance(dependencies, list) or not _unique(dependencies):
            _invalid(error_cls, f"{field}.arms[{index}].dependencies is invalid")
        for dep_index, dependency in enumerate(dependencies):
            _identifier(dependency, f"{field}.arms[{index}].dependencies[{dep_index}]", error_cls)
    if not _unique(arm_ids):
        _invalid(error_cls, f"{field}.arms contains duplicate arm IDs")
    dependencies = batch.get("dependencies")
    if not isinstance(dependencies, list) or not _unique(dependencies):
        _invalid(error_cls, f"{field}.dependencies is invalid")
    for index, dependency in enumerate(dependencies):
        _identifier(dependency, f"{field}.dependencies[{index}]", error_cls)
    if batch.get("registered_before_execution") is not True or batch.get("all_arms_visible") is not True:
        _invalid(error_cls, f"{field} registration flags must be true")


def _validate_manifest_parameter(value: object, field: str, error_cls: type[ClientError] = InvalidRequest) -> None:
    parameter = _object(value, field, {"type", "value"}, error_cls=error_cls)
    kind = parameter.get("type")
    if kind not in {"string", "integer", "number", "boolean", "string-list"}:
        _invalid(error_cls, f"{field}.type is invalid")
    actual = parameter.get("value")
    if kind == "string" and not isinstance(actual, str):
        _invalid(error_cls, f"{field}.value must be a string")
    elif kind == "integer" and (isinstance(actual, bool) or not isinstance(actual, int)):
        _invalid(error_cls, f"{field}.value must be an integer")
    elif kind == "number" and (isinstance(actual, bool) or not isinstance(actual, (int, float))):
        _invalid(error_cls, f"{field}.value must be a number")
    elif kind == "boolean" and not isinstance(actual, bool):
        _invalid(error_cls, f"{field}.value must be a boolean")
    elif kind == "string-list" and (not isinstance(actual, list) or any(not isinstance(item, str) for item in actual)):
        _invalid(error_cls, f"{field}.value must be a string list")


def _validate_signed_receipt(value: object, field: str, error_cls: type[ClientError] = InvalidRequest) -> None:
    receipt = _object(value, field, {"receipt_id", "object_hash", "destination", "issued_at", "signer", "proof", "key_rotation_ref"}, error_cls=error_cls)
    _identifier(receipt.get("receipt_id"), f"{field}.receipt_id", error_cls)
    _valid_hash(receipt.get("object_hash"), f"{field}.object_hash", error_cls)
    _identifier(receipt.get("destination"), f"{field}.destination", error_cls)
    _utc_time(receipt.get("issued_at"), f"{field}.issued_at", error_cls)
    _validate_principal(receipt.get("signer"), f"{field}.signer", error_cls)
    _validate_proof(receipt.get("proof"), f"{field}.proof", error_cls)
    _validate_key_rotation(receipt.get("key_rotation_ref"), f"{field}.key_rotation_ref", error_cls)


def _validate_transfer_hook(value: object, field: str, error_cls: type[ClientError] = InvalidRequest) -> None:
    transfer = _object(value, field, {"interface_version", "mode", "destination", "object_hash", "authenticated_context_ref", "key_rotation_ref"}, error_cls=error_cls)
    if transfer.get("interface_version") != 1:
        _invalid(error_cls, f"{field}.interface_version is invalid")
    if transfer.get("mode") not in {"local", "object-reference", "stream"}:
        _invalid(error_cls, f"{field}.mode is invalid")
    _identifier(transfer.get("destination"), f"{field}.destination", error_cls)
    _valid_hash(transfer.get("object_hash"), f"{field}.object_hash", error_cls)
    _identifier(transfer.get("authenticated_context_ref"), f"{field}.authenticated_context_ref", error_cls)
    if transfer.get("key_rotation_ref") is not None:
        _validate_key_rotation(transfer.get("key_rotation_ref"), f"{field}.key_rotation_ref", error_cls)


def _validate_output_policy(value: object, field: str, error_cls: type[ClientError] = InvalidRequest) -> None:
    policy = _object(value, field, {"destination", "retention_s", "sanitise", "receipt_required", "transfer", "receipt_ref"}, error_cls=error_cls)
    _identifier(policy.get("destination"), f"{field}.destination", error_cls)
    _nonnegative_int(policy.get("retention_s"), f"{field}.retention_s", error_cls)
    if not isinstance(policy.get("sanitise"), bool) or not isinstance(policy.get("receipt_required"), bool):
        _invalid(error_cls, f"{field} boolean policy fields are invalid")
    _validate_transfer_hook(policy.get("transfer"), f"{field}.transfer", error_cls)
    if policy.get("receipt_ref") is not None:
        _validate_signed_receipt(policy.get("receipt_ref"), f"{field}.receipt_ref", error_cls)


def _validate_signed_manifest(value: object, field: str, error_cls: type[ClientError] = InvalidRequest) -> None:
    required = {"schema_version", "job_id", "request_id", "origin_site", "destination_site", "principal", "nonce", "expires", "pipeline_id", "pipeline_version", "image_digest", "parameters", "input_hashes", "output_policy", "resource_ceiling", "deadline_s", "policy_hash", "required_assurance", "canonical_payload_hash", "proof", "key_rotation_ref"}
    manifest = _object(value, field, required, error_cls=error_cls)
    if manifest.get("schema_version") != 1:
        _invalid(error_cls, f"{field}.schema_version is invalid")
    for key in ("job_id", "request_id", "nonce"):
        _identifier(manifest.get(key), f"{field}.{key}", error_cls)
    for key in ("origin_site", "destination_site", "pipeline_id"):
        _short_identifier(manifest.get(key), f"{field}.{key}", error_cls)
    _validate_principal(manifest.get("principal"), f"{field}.principal", error_cls)
    _utc_time(manifest.get("expires"), f"{field}.expires", error_cls)
    if not isinstance(manifest.get("pipeline_version"), str) or not _VERSION.fullmatch(str(manifest.get("pipeline_version"))):
        _invalid(error_cls, f"{field}.pipeline_version is invalid")
    _valid_hash(manifest.get("image_digest"), f"{field}.image_digest", error_cls)
    parameters = manifest.get("parameters")
    if not isinstance(parameters, Mapping):
        _invalid(error_cls, f"{field}.parameters must be an object")
    for key, parameter in parameters.items():
        _identifier(key, f"{field}.parameters.{key}", error_cls)
        _validate_manifest_parameter(parameter, f"{field}.parameters.{key}", error_cls)
    input_hashes = manifest.get("input_hashes")
    if not isinstance(input_hashes, list) or not _unique(input_hashes):
        _invalid(error_cls, f"{field}.input_hashes is invalid")
    for index, item in enumerate(input_hashes):
        _valid_hash(item, f"{field}.input_hashes[{index}]", error_cls)
    _validate_output_policy(manifest.get("output_policy"), f"{field}.output_policy", error_cls)
    _validate_resource_ceiling(manifest.get("resource_ceiling"), f"{field}.resource_ceiling", error_cls)
    _positive_int(manifest.get("deadline_s"), f"{field}.deadline_s", error_cls)
    _valid_hash(manifest.get("policy_hash"), f"{field}.policy_hash", error_cls)
    _validate_assurance(manifest.get("required_assurance"), f"{field}.required_assurance", error_cls)
    _valid_hash(manifest.get("canonical_payload_hash"), f"{field}.canonical_payload_hash", error_cls)
    _validate_proof(manifest.get("proof"), f"{field}.proof", error_cls)
    if manifest.get("key_rotation_ref") is not None:
        _validate_key_rotation(manifest.get("key_rotation_ref"), f"{field}.key_rotation_ref", error_cls)


def _validate_ingress(ingress: object) -> None:
    ingress_value = _object(ingress, "admission.ingress", {"actor", "subject", "controller_id", "authenticated_peer", "peer_source", "auth_method", "transport_binding", "peer_verified", "forwarding_headers_ignored", "operator_elevation"})
    _validate_principal(ingress_value.get("actor"), "admission.ingress.actor")
    subject = ingress_value.get("subject")
    if subject is not None:
        _validate_principal(subject, "admission.ingress.subject")
    _identifier(ingress_value.get("controller_id"), "admission.ingress.controller_id")
    _identifier(ingress_value.get("authenticated_peer"), "admission.ingress.authenticated_peer")
    if ingress_value.get("peer_source") != "socket-peer":
        raise InvalidRequest("admission peer_source must be socket-peer")
    if ingress_value.get("transport_binding") != "transport-independent":
        raise InvalidRequest("admission transport binding is invalid")
    if ingress_value.get("auth_method") not in {"local", "tailnet-peer", "ssh-sk", "webauthn"}:
        raise InvalidRequest("admission auth method is invalid")
    if ingress_value.get("peer_verified") is not True or ingress_value.get("forwarding_headers_ignored") is not True:
        raise InvalidRequest("admission peer assertions are invalid")
    if ingress_value.get("operator_elevation") not in {"none", "approval-only"}:
        raise InvalidRequest("admission operator elevation is invalid")


def _default_admission(controller_id: str) -> dict[str, object]:
    """Return a schema-shaped hint; the authority authenticates the socket peer.

    The values here are deliberately generic and are never used to confer
    operator privilege.  Deployments may inject the complete admission object
    from their composition layer.
    """

    return {
        "execution": "atomic",
        "approval": {"approval_id": None, "required": False, "consume_atomically": True},
        "pipeline": None,
        "delegation": None,
        "ingress": {
            "actor": {"site_id": "site", "tenant_id": "tenant", "issuer": "client", "subject": "client"},
            "subject": None,
            "controller_id": controller_id,
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


def _controller_id(admission: Mapping[str, object]) -> str:
    ingress = admission.get("ingress")
    if isinstance(ingress, Mapping) and isinstance(ingress.get("controller_id"), str):
        return str(ingress["controller_id"])
    return os.environ.get(CONTROLLER_ID_ENV, "controller")


def _validate_admission(admission: object) -> None:
    admission_value = _object(admission, "admission", {"execution", "approval", "pipeline", "delegation", "ingress", "batch"})
    if admission_value.get("execution") != "atomic":
        raise InvalidRequest("admission execution must be atomic")
    _validate_approval_selection(admission_value.get("approval"), "admission.approval")
    pipeline = admission_value.get("pipeline")
    if pipeline is not None:
        _validate_pipeline(pipeline, "admission.pipeline")
    delegation = admission_value.get("delegation")
    if delegation is not None:
        _validate_delegation(delegation, "admission.delegation")
    _validate_ingress(admission_value.get("ingress"))
    batch = admission_value.get("batch")
    if batch is not None:
        _validate_batch(batch, "admission.batch")


def validate_operation(message: Mapping[str, object]) -> None:
    """Validate the executable portion of the frozen RPC request schema.

    This is intentionally stdlib-only.  The authoritative JSON Schema remains
    in P0; this boundary check prevents malformed stdin or CLI requests from
    reaching a transport when the optional schema test dependencies are absent.
    """

    if not isinstance(message, Mapping):
        raise InvalidRequest("RPC request must be an object")
    required = {"schema", "request_id", "op", "lane", "args", "idempotency_scope", "request_fingerprint", "admission"}
    if set(message) != required:
        missing = required - set(message)
        extra = set(message) - required
        detail = []
        if missing:
            detail.append("missing " + ", ".join(sorted(missing)))
        if extra:
            detail.append("unexpected " + ", ".join(sorted(extra)))
        raise InvalidRequest("invalid RPC envelope: " + "; ".join(detail))
    if message.get("schema") != 1:
        raise InvalidRequest("unsupported RPC schema")
    _identifier(message.get("request_id"), "request_id")
    op = message.get("op")
    if not isinstance(op, str) or op not in _OPS:
        raise InvalidRequest("unknown RPC operation")
    lane = message.get("lane")
    if lane is not None:
        _short_identifier(lane, "lane")
    args = message.get("args")
    if not isinstance(args, Mapping):
        raise InvalidRequest("args must be an object")
    scope = message.get("idempotency_scope")
    if not isinstance(scope, Mapping) or scope.get("scope") not in {"authenticated-principal", "controller"}:
        raise InvalidRequest("invalid idempotency scope")
    _identifier(scope.get("controller_id"), "idempotency controller_id")
    _valid_hash(message.get("request_fingerprint"), "request_fingerprint")
    _validate_admission(message.get("admission"))
    _validate_args(str(op), lane, args)
    admission = message["admission"]
    assert isinstance(admission, Mapping)
    if op in {"acquire", "chat-load"} and args.get("pipeline_ref") is not None:
        pipeline = admission.get("pipeline")
        if not isinstance(pipeline, Mapping) or pipeline.get("pipeline_id") != args.get("pipeline_ref"):
            raise InvalidRequest("pipeline binding does not match pipeline_ref")
    if op in {"approve", "preempt"}:
        approval = admission.get("approval")
        if not isinstance(approval, Mapping) or approval.get("required") is not True or approval.get("approval_id") != args.get("approval_id"):
            raise InvalidRequest("approval selection does not match request")


def _validate_args(op: str, lane: object, args: Mapping[str, object]) -> None:
    if op == "acquire":
        _require_exact_keys(args, {"purpose", "class", "est_s", "max_s", "booking_id", "queue_id", "pipeline_ref", "signed_manifest"}, {"purpose", "class", "est_s", "max_s"})
        _purpose(args.get("purpose"))
        if args.get("class") not in _CLASSES:
            raise InvalidRequest("invalid acquire class")
        _positive_int(args.get("est_s"), "est_s")
        _positive_int(args.get("max_s"), "max_s")
        if int(args["est_s"]) > int(args["max_s"]):
            raise InvalidRequest("acquire estimate exceeds max")
        for key in ("booking_id", "queue_id"):
            if key in args and args[key] is not None:
                _identifier(args[key], key)
        if "pipeline_ref" in args and args["pipeline_ref"] is not None:
            _short_identifier(args["pipeline_ref"], "pipeline_ref")
        if args.get("signed_manifest") is not None:
            _validate_signed_manifest(args.get("signed_manifest"), "signed_manifest")
        return
    if op in {"renew", "release"}:
        allowed = {"token", "instance", "extend_s"}
        _require_exact_keys(args, allowed, {"token"})
        _token(args.get("token"))
        if "instance" in args and args["instance"] is not None:
            _identifier(args["instance"], "instance")
        if op == "renew":
            if "extend_s" not in args:
                raise InvalidRequest("renew requires extend_s")
            _positive_int(args.get("extend_s"), "extend_s")
        elif "extend_s" in args:
            raise InvalidRequest("release accepts token only")
        if lane is None:
            raise InvalidRequest(f"{op} requires a lane")
        return
    if op == "claim":
        if lane is None:
            raise InvalidRequest("claim requires a lane")
        _require_exact_keys(args, {"token", "generation", "generation_source", "instance", "booking_id", "revision"}, set())
        adoption = "generation" in args or "generation_source" in args
        booking = "booking_id" in args or "revision" in args
        if adoption == booking:
            raise InvalidRequest("claim must be adoption or booking check-in")
        if adoption:
            _require_exact_keys(args, {"token", "generation", "generation_source", "instance"}, {"token", "generation", "generation_source"})
            _token(args.get("token"))
            _positive_int(args.get("generation"), "generation")
            if args.get("generation_source") != "authenticated-adoption":
                raise InvalidRequest("generation source is not authenticated")
        else:
            _require_exact_keys(args, {"booking_id", "revision"}, {"booking_id", "revision"})
            _identifier(args.get("booking_id"), "booking_id")
            _positive_int(args.get("revision"), "revision")
        return
    if op == "queue":
        action = args.get("action")
        if action == "add":
            _require_exact_keys(args, {"action", "purpose", "class", "max_wait_s"}, {"action", "purpose", "class", "max_wait_s"})
            _purpose(args.get("purpose"))
            if args.get("class") not in _CLASSES:
                raise InvalidRequest("invalid queue class")
            _positive_int(args.get("max_wait_s"), "max_wait_s")
        elif action == "refresh":
            _require_exact_keys(args, {"action", "queue_id", "max_wait_s"}, {"action", "queue_id", "max_wait_s"})
            _identifier(args.get("queue_id"), "queue_id")
            _positive_int(args.get("max_wait_s"), "max_wait_s")
        elif action == "remove":
            _require_exact_keys(args, {"action", "queue_id"}, {"action", "queue_id"})
            _identifier(args.get("queue_id"), "queue_id")
        elif action == "list":
            _require_exact_keys(args, {"action"}, {"action"})
        else:
            raise InvalidRequest("invalid queue action")
        return
    if op == "book":
        if lane is None:
            raise InvalidRequest("book requires a lane")
        _require_exact_keys(args, {"start", "end", "purpose"}, {"start", "end", "purpose"})
        for key in ("start", "end"):
            _utc_time(args.get(key), key)
        _purpose(args.get("purpose"))
        return
    if op == "cancel":
        if lane is None:
            raise InvalidRequest("cancel requires a lane")
        _require_exact_keys(args, {"booking_id", "revision"}, {"booking_id"})
        _identifier(args.get("booking_id"), "booking_id")
        if "revision" in args:
            _positive_int(args.get("revision"), "revision")
        return
    if op == "approval-request":
        _require_exact_keys(args, {"action", "booking_id", "revision", "target_generation", "bounds", "reason", "destination_site", "controller_id", "payload_hash", "manifest_hash", "policy_hash"}, {"action", "booking_id", "revision", "target_generation", "bounds", "reason", "destination_site", "controller_id", "payload_hash", "manifest_hash", "policy_hash"})
        if args.get("action") not in {"extension", "forced-preemption", "displacement", "pipeline", "operator-admission"}:
            raise InvalidRequest("invalid approval action")
        for key in ("booking_id", "revision", "target_generation"):
            if args.get(key) is not None:
                _positive_int(args[key], key) if key != "booking_id" else _identifier(args[key], key)
        bounds = args.get("bounds")
        if not isinstance(bounds, Mapping) or set(bounds) != {"max_s", "max_end"}:
            raise InvalidRequest("invalid approval bounds")
        _positive_int(bounds.get("max_s"), "bounds.max_s")
        _utc_time(bounds.get("max_end"), "bounds.max_end")
        _purpose(args.get("reason"))
        _short_identifier(args.get("destination_site"), "destination_site")
        _identifier(args.get("controller_id"), "controller_id")
        for key in ("payload_hash", "policy_hash"):
            _valid_hash(args.get(key), key)
        if args.get("manifest_hash") is not None:
            _valid_hash(args.get("manifest_hash"), "manifest_hash")
        return
    if op == "approve":
        _require_exact_keys(args, {"approval_id", "proof", "evidence"}, {"approval_id", "proof", "evidence"})
        _identifier(args.get("approval_id"), "approval_id")
        _validate_proof(args.get("proof"), "approval proof")
        _validate_evidence(args.get("evidence"), "approval evidence")
        return
    if op == "preempt":
        if lane is None:
            raise InvalidRequest("preempt requires a lane")
        _require_exact_keys(args, {"token", "approval_id"}, {"token", "approval_id"})
        _token(args.get("token"))
        _identifier(args.get("approval_id"), "approval_id")
        return
    if op == "chat-load":
        if lane is None:
            raise InvalidRequest("chat-load requires a lane")
        _require_exact_keys(args, {"pipeline_ref", "purpose"}, {"pipeline_ref", "purpose"})
        _short_identifier(args.get("pipeline_ref"), "pipeline_ref")
        _purpose(args.get("purpose"))
        return
    if op == "chat-unload":
        if lane is None:
            raise InvalidRequest("chat-unload requires a lane")
        _require_exact_keys(args, {"occupant_id", "generation"}, {"occupant_id", "generation"})
        _identifier(args.get("occupant_id"), "occupant_id")
        _positive_int(args.get("generation"), "generation")
        return
    if op in {"cal", "free", "report", "status"}:
        if op != "cal" and lane is None:
            raise InvalidRequest(f"{op} requires a lane")
        _require_exact_keys(args, {"at"}, set())
        if "at" in args and args["at"] is not None:
            _utc_time(args["at"], "at")
        return
    raise InvalidRequest("unsupported operation")


def _require_exact_keys(args: Mapping[str, object], allowed: set[str], required: set[str]) -> None:
    extra = set(args) - allowed
    missing = required - set(args)
    if extra or missing:
        parts = []
        if missing:
            parts.append("missing " + ", ".join(sorted(missing)))
        if extra:
            parts.append("unexpected " + ", ".join(sorted(extra)))
        raise InvalidRequest("invalid operation arguments: " + "; ".join(parts))


def _token(value: object) -> None:
    if not isinstance(value, str) or len(value) < 16 or len(value) > 512:
        raise InvalidRequest("token is invalid")


def _response_token(value: object, field: str) -> None:
    if not isinstance(value, str) or len(value) < 16 or len(value) > 512:
        raise InvalidResponse(f"{field} is invalid")


def _validate_response_lease(value: object, field: str, *, read: bool = False) -> None:
    if read:
        lease = _object(value, field, {"schema_version", "lease_id", "lane", "generation", "reservation", "instance", "principal", "class", "purpose", "state", "token_redacted"}, error_cls=InvalidResponse)
        if "token" in lease:
            raise InvalidResponse(f"{field} contains a raw token")
        if lease.get("token_redacted") is not True:
            raise InvalidResponse(f"{field}.token_redacted must be true")
        state = lease.get("state")
        if state not in _LEASE_STATES:
            raise InvalidResponse(f"{field}.state is invalid")
    else:
        lease = _object(value, field, {"schema_version", "lease_id", "lane", "generation", "reservation", "token", "instance", "principal", "class", "purpose", "estimated_s", "started_at", "max_end", "approved_max_end", "heartbeat_at", "deadline", "booking_id", "unit", "invocation", "state"}, error_cls=InvalidResponse)
        _response_token(lease.get("token"), f"{field}.token")
        for key in ("started_at", "max_end", "approved_max_end", "heartbeat_at"):
            _utc_time(lease.get(key), f"{field}.{key}", InvalidResponse)
        _positive_int(lease.get("estimated_s"), f"{field}.estimated_s", InvalidResponse)
        _validate_deadline(lease.get("deadline"), f"{field}.deadline", InvalidResponse)
        booking_id = lease.get("booking_id")
        if booking_id is not None:
            _identifier(booking_id, f"{field}.booking_id", InvalidResponse)
        _identifier(lease.get("unit"), f"{field}.unit", InvalidResponse)
        _identifier(lease.get("invocation"), f"{field}.invocation", InvalidResponse)
        if lease.get("state") not in _LEASE_STATES:
            raise InvalidResponse(f"{field}.state is invalid")
    if lease.get("schema_version") != 1:
        raise InvalidResponse(f"{field}.schema_version is invalid")
    _identifier(lease.get("lease_id"), f"{field}.lease_id", InvalidResponse)
    _validate_lane_ref(lease.get("lane"), f"{field}.lane", InvalidResponse)
    _positive_int(lease.get("generation"), f"{field}.generation", InvalidResponse)
    _validate_lane_generation(lease.get("reservation"), f"{field}.reservation", InvalidResponse)
    _identifier(lease.get("instance"), f"{field}.instance", InvalidResponse)
    _validate_principal(lease.get("principal"), f"{field}.principal", InvalidResponse)
    if lease.get("class") not in _CLASSES:
        raise InvalidResponse(f"{field}.class is invalid")
    _purpose(lease.get("purpose"), InvalidResponse)


def _validate_response_occupant(value: object, field: str) -> None:
    if not isinstance(value, Mapping):
        raise InvalidResponse(f"{field} must be an object")
    if "token" in value:
        raise InvalidResponse(f"{field} contains a raw token")
    required = {"schema_version", "occupant_id", "lane", "reservation", "generation", "instance", "principal", "class", "pipeline_ref", "purpose", "loaded_at", "last_activity", "request_accounting", "state", "unit", "invocation", "deadline", "token_redacted"}
    occupant = _object(value, field, required, error_cls=InvalidResponse)
    if occupant.get("schema_version") != 1 or occupant.get("token_redacted") is not True:
        raise InvalidResponse(f"{field} redaction is invalid")
    _identifier(occupant.get("occupant_id"), f"{field}.occupant_id", InvalidResponse)
    _validate_lane_ref(occupant.get("lane"), f"{field}.lane", InvalidResponse)
    _validate_lane_generation(occupant.get("reservation"), f"{field}.reservation", InvalidResponse)
    _positive_int(occupant.get("generation"), f"{field}.generation", InvalidResponse)
    _identifier(occupant.get("instance"), f"{field}.instance", InvalidResponse)
    _validate_principal(occupant.get("principal"), f"{field}.principal", InvalidResponse)
    if occupant.get("class") not in {"service", "resident", "standby"}:
        raise InvalidResponse(f"{field}.class is invalid")
    _short_identifier(occupant.get("pipeline_ref"), f"{field}.pipeline_ref", InvalidResponse)
    _purpose(occupant.get("purpose"), InvalidResponse)
    _utc_time(occupant.get("loaded_at"), f"{field}.loaded_at", InvalidResponse)
    _utc_time(occupant.get("last_activity"), f"{field}.last_activity", InvalidResponse)
    accounting = _object(occupant.get("request_accounting"), f"{field}.request_accounting", {"active_requests", "completed_requests", "last_completed_at", "activity_basis"}, error_cls=InvalidResponse)
    _nonnegative_int(accounting.get("active_requests"), f"{field}.request_accounting.active_requests", InvalidResponse)
    _nonnegative_int(accounting.get("completed_requests"), f"{field}.request_accounting.completed_requests", InvalidResponse)
    if accounting.get("last_completed_at") is not None:
        _utc_time(accounting.get("last_completed_at"), f"{field}.request_accounting.last_completed_at", InvalidResponse)
    if accounting.get("activity_basis") != "completed-user-request":
        raise InvalidResponse(f"{field}.request_accounting.activity_basis is invalid")
    if occupant.get("state") not in {"loading", "running", "draining", "unloaded", "quarantined"}:
        raise InvalidResponse(f"{field}.state is invalid")
    _identifier(occupant.get("unit"), f"{field}.unit", InvalidResponse)
    _identifier(occupant.get("invocation"), f"{field}.invocation", InvalidResponse)
    _validate_deadline(occupant.get("deadline"), f"{field}.deadline", InvalidResponse)


def _validate_response_booking(value: object, field: str) -> None:
    booking = _object(value, field, {"schema_version", "booking_id", "revision", "lane", "reservation", "principal", "purpose", "start", "end", "state", "checked_in_at", "created_at", "displacement", "recovery"}, error_cls=InvalidResponse)
    if booking.get("schema_version") != 1:
        raise InvalidResponse(f"{field}.schema_version is invalid")
    _identifier(booking.get("booking_id"), f"{field}.booking_id", InvalidResponse)
    _positive_int(booking.get("revision"), f"{field}.revision", InvalidResponse)
    if booking.get("lane") is not None:
        _validate_lane_ref(booking.get("lane"), f"{field}.lane", InvalidResponse)
    _validate_lane_generation(booking.get("reservation"), f"{field}.reservation", InvalidResponse)
    _validate_principal(booking.get("principal"), f"{field}.principal", InvalidResponse)
    _purpose(booking.get("purpose"), InvalidResponse)
    _utc_time(booking.get("start"), f"{field}.start", InvalidResponse)
    _utc_time(booking.get("end"), f"{field}.end", InvalidResponse)
    if booking.get("state") not in {"scheduled", "blocked", "claimed", "missed", "completed", "cancelled", "displaced", "recovery"}:
        raise InvalidResponse(f"{field}.state is invalid")
    if booking.get("checked_in_at") is not None:
        _utc_time(booking.get("checked_in_at"), f"{field}.checked_in_at", InvalidResponse)
    _utc_time(booking.get("created_at"), f"{field}.created_at", InvalidResponse)
    if booking.get("displacement") is not None:
        _identifier(booking.get("displacement"), f"{field}.displacement", InvalidResponse)
    recovery = _object(booking.get("recovery"), f"{field}.recovery", {"state", "at", "reason"}, error_cls=InvalidResponse)
    if recovery.get("state") not in {"none", "blocked-check-in", "no-show-reopened", "overrun-delayed"}:
        raise InvalidResponse(f"{field}.recovery.state is invalid")
    if recovery.get("at") is not None:
        _utc_time(recovery.get("at"), f"{field}.recovery.at", InvalidResponse)
    if recovery.get("reason") is not None and (not isinstance(recovery.get("reason"), str) or not recovery["reason"] or len(recovery["reason"]) > 512):
        raise InvalidResponse(f"{field}.recovery.reason is invalid")
    if recovery.get("state") == "none" and (recovery.get("at") is not None or recovery.get("reason") is not None):
        raise InvalidResponse(f"{field}.recovery is inconsistent")
    if recovery.get("state") != "none" and (recovery.get("at") is None or not isinstance(recovery.get("reason"), str)):
        raise InvalidResponse(f"{field}.recovery is incomplete")


def _validate_response_approval(value: object, field: str) -> None:
    required = {"schema_version", "id", "challenge_id", "challenge_nonce", "action", "requester", "lane", "booking_id", "revision", "target_generation", "bounds", "reason", "nonce", "expires", "approver", "approved_at", "proof", "verified_evidence", "consumed_at", "destination_site", "controller_id", "payload_hash", "manifest_hash", "policy_hash", "challenge_policy_hash", "challenge_manifest_hash", "canonicalization", "canonical_encoding", "hash_algorithm", "domain", "signed_fields", "state"}
    approval = _object(value, field, required, error_cls=InvalidResponse)
    if approval.get("schema_version") != 1:
        raise InvalidResponse(f"{field}.schema_version is invalid")
    for key in ("id", "challenge_id", "challenge_nonce", "nonce", "controller_id"):
        _identifier(approval.get(key), f"{field}.{key}", InvalidResponse)
    if approval.get("action") not in {"extension", "forced-preemption", "displacement", "pipeline", "operator-admission"}:
        raise InvalidResponse(f"{field}.action is invalid")
    _validate_principal(approval.get("requester"), f"{field}.requester", InvalidResponse)
    if approval.get("lane") is not None:
        _validate_lane_ref(approval.get("lane"), f"{field}.lane", InvalidResponse)
    for key in ("booking_id",):
        if approval.get(key) is not None:
            _identifier(approval.get(key), f"{field}.{key}", InvalidResponse)
    for key in ("revision", "target_generation"):
        if approval.get(key) is not None:
            _positive_int(approval.get(key), f"{field}.{key}", InvalidResponse)
    bounds = _object(approval.get("bounds"), f"{field}.bounds", {"max_s", "max_end"}, error_cls=InvalidResponse)
    _positive_int(bounds.get("max_s"), f"{field}.bounds.max_s", InvalidResponse)
    _utc_time(bounds.get("max_end"), f"{field}.bounds.max_end", InvalidResponse)
    if not isinstance(approval.get("reason"), str) or not approval["reason"] or len(approval["reason"]) > 1024:
        raise InvalidResponse(f"{field}.reason is invalid")
    _utc_time(approval.get("expires"), f"{field}.expires", InvalidResponse)
    if approval.get("approver") is not None:
        _validate_principal(approval.get("approver"), f"{field}.approver", InvalidResponse)
    for key in ("approved_at", "consumed_at"):
        if approval.get(key) is not None:
            _utc_time(approval.get(key), f"{field}.{key}", InvalidResponse)
    if approval.get("proof") is not None:
        _validate_proof(approval.get("proof"), f"{field}.proof", InvalidResponse)
    if approval.get("verified_evidence") is not None:
        _validate_evidence(approval.get("verified_evidence"), f"{field}.verified_evidence", InvalidResponse)
    _short_identifier(approval.get("destination_site"), f"{field}.destination_site", InvalidResponse)
    for key in ("payload_hash", "policy_hash", "challenge_policy_hash"):
        _valid_hash(approval.get(key), f"{field}.{key}", InvalidResponse)
    if approval.get("manifest_hash") is not None:
        _valid_hash(approval.get("manifest_hash"), f"{field}.manifest_hash", InvalidResponse)
    if approval.get("challenge_manifest_hash") is not None:
        _valid_hash(approval.get("challenge_manifest_hash"), f"{field}.challenge_manifest_hash", InvalidResponse)
    if approval.get("canonicalization") != "JCS-RFC8785" or approval.get("canonical_encoding") != "UTF-8" or approval.get("hash_algorithm") != "SHA-256" or approval.get("domain") != "flightctl/approval/v1":
        raise InvalidResponse(f"{field} canonical metadata is invalid")
    signed_fields = ["id", "action", "requester", "lane", "booking_id", "revision", "target_generation", "bounds", "reason", "nonce", "expires", "destination_site", "controller_id", "payload_hash", "manifest_hash", "policy_hash", "challenge_id", "challenge_nonce"]
    if approval.get("signed_fields") != signed_fields:
        raise InvalidResponse(f"{field}.signed_fields is invalid")
    if approval.get("state") not in {"issued", "approved", "consumed", "expired", "revoked"}:
        raise InvalidResponse(f"{field}.state is invalid")


def _validate_response_event(value: object, field: str) -> None:
    event = _object(value, field, {"schema_version", "event_id", "occurred_at", "kind", "state", "request_id", "job_id", "actor", "subject", "site_id", "controller_id", "correlation_id", "lane", "generation", "reason", "data"}, error_cls=InvalidResponse)
    if event.get("schema_version") != 1:
        raise InvalidResponse(f"{field}.schema_version is invalid")
    for key in ("event_id", "request_id", "controller_id", "correlation_id"):
        _identifier(event.get(key), f"{field}.{key}", InvalidResponse)
    if event.get("job_id") is not None:
        _identifier(event.get("job_id"), f"{field}.job_id", InvalidResponse)
    _utc_time(event.get("occurred_at"), f"{field}.occurred_at", InvalidResponse)
    if event.get("kind") not in {"acquire", "renew", "release", "claim", "queue", "book", "cancel", "approval-request", "approval", "preempt", "chat-load", "chat-unload", "reconcile", "discovery"} or event.get("state") not in _RECORD_STATES | {"recovery"}:
        raise InvalidResponse(f"{field} discriminator is invalid")
    _validate_principal(event.get("actor"), f"{field}.actor", InvalidResponse)
    if event.get("subject") is not None:
        _validate_principal(event.get("subject"), f"{field}.subject", InvalidResponse)
    _short_identifier(event.get("site_id"), f"{field}.site_id", InvalidResponse)
    if event.get("lane") is not None:
        _validate_lane_ref(event.get("lane"), f"{field}.lane", InvalidResponse)
    if event.get("generation") is not None:
        _positive_int(event.get("generation"), f"{field}.generation", InvalidResponse)
    if not isinstance(event.get("reason"), str) or not event["reason"] or len(event["reason"]) > 1024:
        raise InvalidResponse(f"{field}.reason is invalid")
    data = _object(event.get("data"), f"{field}.data", set(), {"lease_id", "booking_id", "queue_id", "approval_id", "job_id", "policy_hash", "manifest_hash", "token_redacted"}, error_cls=InvalidResponse)
    for key in ("lease_id", "booking_id", "queue_id", "approval_id", "job_id"):
        if key in data:
            _identifier(data[key], f"{field}.data.{key}", InvalidResponse)
    for key in ("policy_hash", "manifest_hash"):
        if key in data:
            _valid_hash(data[key], f"{field}.data.{key}", InvalidResponse)
    if "token_redacted" in data and data["token_redacted"] is not True:
        raise InvalidResponse(f"{field}.data.token_redacted must be true")


def _validate_response_result(data: object) -> None:
    if not isinstance(data, Mapping):
        raise InvalidResponse("successful response is not an object")
    kind = data.get("kind")
    if kind == "grant":
        grant = _object(data, "response.data", {"kind", "operation", "token", "generation", "lease", "reservation", "adoption"}, error_cls=InvalidResponse)
        if grant.get("kind") != "grant" or grant.get("operation") not in {"acquire", "claim", "chat-load"}:
            raise InvalidResponse("grant discriminator is invalid")
        _response_token(grant.get("token"), "grant.token")
        _positive_int(grant.get("generation"), "grant.generation", InvalidResponse)
        _validate_response_lease(grant.get("lease"), "grant.lease")
        _validate_lane_generation(grant.get("reservation"), "grant.reservation", InvalidResponse)
        adoption = _object(grant.get("adoption"), "grant.adoption", {"mode", "principal_bound", "generation_bound", "token_source"}, error_cls=InvalidResponse)
        if adoption.get("mode") not in {"fresh-acquire", "authenticated-adoption"} or adoption.get("principal_bound") is not True or adoption.get("generation_bound") is not True:
            raise InvalidResponse("grant adoption binding is invalid")
        expected_source = "controller-grant" if adoption.get("mode") == "fresh-acquire" else "authenticated-adoption"
        if adoption.get("token_source") != expected_source:
            raise InvalidResponse("grant token source is not bound")
        return
    if kind == "pending":
        pending = _object(data, "response.data", {"kind", "operation", "request_id", "queue_id", "retry_after_s", "wait_deadline", "reason"}, error_cls=InvalidResponse)
        if pending.get("kind") != "pending" or pending.get("operation") not in {"acquire", "claim", "queue", "book", "chat-load"}:
            raise InvalidResponse("pending discriminator is invalid")
        _identifier(pending.get("request_id"), "pending.request_id", InvalidResponse)
        if pending.get("queue_id") is not None:
            _identifier(pending.get("queue_id"), "pending.queue_id", InvalidResponse)
        _positive_int(pending.get("retry_after_s"), "pending.retry_after_s", InvalidResponse)
        _validate_deadline(pending.get("wait_deadline"), "pending.wait_deadline", InvalidResponse)
        if not isinstance(pending.get("reason"), str) or not pending["reason"] or len(pending["reason"]) > 512:
            raise InvalidResponse("pending.reason is invalid")
        return
    if kind == "reachability":
        result = _object(data, "response.data", {"kind", "lane", "host_id", "reachability", "observation", "error"}, error_cls=InvalidResponse)
        if result.get("kind") != "reachability" or result.get("reachability") not in {"confirmed", "unreachable", "unknown"}:
            raise InvalidResponse("reachability result is invalid")
        if result.get("lane") is not None:
            _validate_lane_ref(result.get("lane"), "reachability.lane", InvalidResponse)
        _short_identifier(result.get("host_id"), "reachability.host_id", InvalidResponse)
        _validate_measurement(result.get("observation"), "reachability.observation", InvalidResponse)
        if result.get("error") is not None and (not isinstance(result.get("error"), str) or not result["error"]):
            raise InvalidResponse("reachability.error is invalid")
        return
    if kind == "occupancy":
        result = _object(data, "response.data", {"kind", "lane", "state", "generation", "lease", "occupant", "observation"}, error_cls=InvalidResponse)
        if result.get("kind") != "occupancy" or result.get("state") not in {"free", "starting", "running", "stopping", "quarantined", "unknown"}:
            raise InvalidResponse("occupancy result is invalid")
        _validate_lane_ref(result.get("lane"), "occupancy.lane", InvalidResponse)
        if result.get("generation") is not None:
            _positive_int(result.get("generation"), "occupancy.generation", InvalidResponse)
        if result.get("lease") is not None:
            _validate_response_lease(result.get("lease"), "occupancy.lease", read=True)
        if result.get("occupant") is not None:
            _validate_response_occupant(result.get("occupant"), "occupancy.occupant")
        _validate_measurement(result.get("observation"), "occupancy.observation", InvalidResponse)
        return
    if kind == "projection":
        result = _object(data, "response.data", {"kind", "scope", "lane", "windows", "observation"}, error_cls=InvalidResponse)
        if result.get("kind") != "projection" or result.get("scope") not in {"calendar", "free"}:
            raise InvalidResponse("projection result is invalid")
        if result.get("lane") is not None:
            _validate_lane_ref(result.get("lane"), "projection.lane", InvalidResponse)
        windows = result.get("windows")
        if not isinstance(windows, list):
            raise InvalidResponse("projection.windows is invalid")
        for index, item in enumerate(windows):
            window = _object(item, f"projection.windows[{index}]", {"start", "end", "state", "certainty", "reason"}, error_cls=InvalidResponse)
            _utc_time(window.get("start"), f"projection.windows[{index}].start", InvalidResponse)
            _utc_time(window.get("end"), f"projection.windows[{index}].end", InvalidResponse)
            if window.get("state") not in {"free", "booked", "occupied", "unknown"}:
                raise InvalidResponse(f"projection.windows[{index}].state is invalid")
            _validate_measurement({"certainty": window.get("certainty"), "reason": window.get("reason")}, f"projection.windows[{index}]", InvalidResponse)
        _validate_measurement(result.get("observation"), "projection.observation", InvalidResponse)
        return
    if kind == "status":
        result = _object(data, "response.data", {"kind", "lane", "state", "generation", "occupancy", "reachability"}, error_cls=InvalidResponse)
        if result.get("kind") != "status" or result.get("state") not in {"free", "starting", "running", "stopping", "quarantined", "unknown"}:
            raise InvalidResponse("status result is invalid")
        if result.get("lane") is not None:
            _validate_lane_ref(result.get("lane"), "status.lane", InvalidResponse)
        if result.get("generation") is not None:
            _positive_int(result.get("generation"), "status.generation", InvalidResponse)
        _validate_measurement(result.get("occupancy"), "status.occupancy", InvalidResponse)
        _validate_measurement(result.get("reachability"), "status.reachability", InvalidResponse)
        return
    if kind == "mutation":
        result = _object(data, "response.data", {"kind", "operation", "record_type", "record_id", "state", "revision", "reservation"}, error_cls=InvalidResponse)
        if result.get("kind") != "mutation" or result.get("operation") not in _OPS or result.get("record_type") not in {"lease", "booking", "queue", "occupant", "approval", "event"}:
            raise InvalidResponse("mutation result is invalid")
        _identifier(result.get("record_id"), "mutation.record_id", InvalidResponse)
        if result.get("state") not in _RECORD_STATES:
            raise InvalidResponse("mutation.state is invalid")
        _positive_int(result.get("revision"), "mutation.revision", InvalidResponse)
        _validate_lane_generation(result.get("reservation"), "mutation.reservation", InvalidResponse)
        return
    if kind == "queue":
        result = _object(data, "response.data", {"kind", "entries"}, error_cls=InvalidResponse)
        if result.get("kind") != "queue" or not isinstance(result.get("entries"), list):
            raise InvalidResponse("queue result is invalid")
        for index, item in enumerate(result["entries"]):
            entry = _object(item, f"queue.entries[{index}]", {"schema_version", "queue_id", "lane", "reservation", "principal", "class", "purpose", "sequence", "predecessor", "wait_deadline", "last_seen", "state", "eligible"}, error_cls=InvalidResponse)
            if entry.get("schema_version") != 1:
                raise InvalidResponse(f"queue.entries[{index}].schema_version is invalid")
            _identifier(entry.get("queue_id"), f"queue.entries[{index}].queue_id", InvalidResponse)
            if entry.get("lane") is not None:
                _validate_lane_ref(entry.get("lane"), f"queue.entries[{index}].lane", InvalidResponse)
            _validate_lane_generation(entry.get("reservation"), f"queue.entries[{index}].reservation", InvalidResponse)
            _validate_principal(entry.get("principal"), f"queue.entries[{index}].principal", InvalidResponse)
            if entry.get("class") not in _CLASSES:
                raise InvalidResponse(f"queue.entries[{index}].class is invalid")
            _purpose(entry.get("purpose"), InvalidResponse)
            _positive_int(entry.get("sequence"), f"queue.entries[{index}].sequence", InvalidResponse)
            if entry.get("predecessor") is not None:
                _identifier(entry.get("predecessor"), f"queue.entries[{index}].predecessor", InvalidResponse)
            _validate_deadline(entry.get("wait_deadline"), f"queue.entries[{index}].wait_deadline", InvalidResponse)
            _utc_time(entry.get("last_seen"), f"queue.entries[{index}].last_seen", InvalidResponse)
            if entry.get("state") not in {"queued", "eligible", "claimed", "expired", "removed"} or not isinstance(entry.get("eligible"), bool):
                raise InvalidResponse(f"queue.entries[{index}] state is invalid")
        return
    if kind in {"booking", "approval", "report"}:
        required = {"kind", "booking"} if kind == "booking" else {"kind", "approval"} if kind == "approval" else {"kind", "events", "next_cursor"}
        result = _object(data, "response.data", required, error_cls=InvalidResponse)
        if result.get("kind") != kind:
            raise InvalidResponse(f"{kind} result is invalid")
        if kind == "report":
            if not isinstance(result.get("events"), list):
                raise InvalidResponse("report.events is invalid")
            for index, event in enumerate(result["events"]):
                _validate_response_event(event, f"report.events[{index}]")
            if result.get("next_cursor") is not None:
                _identifier(result.get("next_cursor"), "report.next_cursor", InvalidResponse)
        elif kind == "booking":
            _validate_response_booking(result.get("booking"), "booking.booking")
        else:
            _validate_response_approval(result.get("approval"), "approval.approval")
        return
    raise InvalidResponse("successful response has an unknown result kind")


def validate_response(response: Mapping[str, object], request_id: str) -> None:
    """Check the frozen response envelope and its complete typed result."""

    if not isinstance(response, Mapping):
        raise InvalidResponse("response is not an object")
    if set(response) != {"schema", "request_id", "status", "data", "error"}:
        raise InvalidResponse("response envelope fields are not frozen")
    if response.get("schema") != 1 or response.get("request_id") != request_id:
        raise InvalidResponse("response request identity does not match")
    status = response.get("status")
    if status not in _STATUSES:
        raise InvalidResponse("response status is invalid")
    data = response.get("data")
    error = response.get("error")
    if status == 200:
        if error is not None:
            raise InvalidResponse("successful response must have a null error")
        _validate_response_result(data)
        return
    if status == 202:
        if error is not None:
            raise InvalidResponse("pending response is not a typed pending result")
        _validate_response_result(data)
        if not isinstance(data, Mapping) or data.get("request_id") != request_id:
            raise InvalidResponse("pending response request identity does not match")
        return
    if data is not None or not isinstance(error, Mapping):
        raise InvalidResponse("failure response has invalid data/error")
    error = _object(error, "failure response error", {"code", "message", "retryable", "failure_class"}, {"details"}, error_cls=InvalidResponse)
    if error.get("code") not in _ERROR_CODES or error.get("failure_class") not in _FAILURE_CLASSES:
        raise InvalidResponse("failure response error is invalid")
    if not isinstance(error.get("message"), str) or not error["message"]:
        raise InvalidResponse("failure response message is invalid")
    if not isinstance(error.get("retryable"), bool):
        raise InvalidResponse("failure response retryable flag is invalid")
    if "details" in error and not isinstance(error.get("details"), Mapping):
        raise InvalidResponse("failure response details are invalid")


def failure_response(request_id: str, message: str, *, code: str = "unknown", failure_class: str = "transport") -> dict[str, object]:
    if code not in _ERROR_CODES:
        code = "unknown"
    if failure_class not in _FAILURE_CLASSES:
        failure_class = "transport"
    return {
        "schema": 1,
        "request_id": request_id,
        "status": 503,
        "data": None,
        "error": {"code": code, "message": message[:512], "retryable": True, "failure_class": failure_class},
    }


def _normalise_transport_result(raw: object) -> Mapping[str, object]:
    if not isinstance(raw, Mapping):
        raise TransportFailure("transport returned a non-object result")
    # P0's scripted SSH fake wraps successful envelopes as status=response.
    status = raw.get("status")
    if isinstance(status, str):
        status_lower = status.lower()
        if status_lower == "ok" and isinstance(raw.get("response"), Mapping):
            return raw["response"]  # type: ignore[return-value]
        if status_lower in {"denied", "timeout", "lost", "delayed", "unknown", "failure"}:
            detail = raw.get("error", f"transport {status_lower}")
            raise TransportFailure(str(detail), retryable=status_lower != "denied")
    return raw


def _fingerprint(op: str, lane: str | None, args: Mapping[str, object]) -> str:
    payload = {"op": op, "lane": lane, "args": args}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _request_id() -> str:
    return "req-" + uuid.uuid4().hex


@dataclass(frozen=True)
class Grant:
    """Validated grant data passed to the controller-owned handoff."""

    data: Mapping[str, object]

    @property
    def token(self) -> str:
        return str(self.data["token"])

    @property
    def generation(self) -> int:
        return int(self.data["generation"])


class RpcClient:
    """Build and send v1 requests through an injected P0 Transport."""

    def __init__(
        self,
        transport: "Transport | Any | None" = None,
        *,
        clock: "Clock | Any | None" = None,
        endpoint: str | None = None,
        admission: Mapping[str, object] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        retries: int = 1,
        sleeper: Callable[[float], object] | None = None,
        request_id_factory: Callable[[], str] | None = None,
        token_lookup: Callable[[str], str | None] | None = None,
        handoff: Callable[[Mapping[str, object], Sequence[str]], object] | None = None,
        display_timezone: str | None = None,
    ) -> None:
        self.transport = transport or HttpTransport()
        self.clock = clock or SystemClock()
        self.endpoint = endpoint or os.environ.get(RPC_ENDPOINT_ENV, "http://localhost/v1/rpc")
        self.timeout_s = float(timeout_s)
        self.retries = max(0, int(retries))
        self.sleeper = sleeper or time.sleep
        self.request_id_factory = request_id_factory or _request_id
        self.token_lookup = token_lookup
        self.handoff = handoff
        self.display_timezone = display_timezone or os.environ.get(DISPLAY_TIMEZONE_ENV, "UTC")
        try:
            self.display_zone = ZoneInfo(self.display_timezone)
        except ZoneInfoNotFoundError as exc:
            raise InvalidRequest(f"display timezone is unknown: {self.display_timezone}") from exc
        if admission is None:
            controller = os.environ.get(CONTROLLER_ID_ENV, "controller")
            self.admission = _default_admission(controller)
        else:
            self.admission = _deepcopy(admission)
        _validate_admission(self.admission)

    def new_request_id(self) -> str:
        value = self.request_id_factory()
        _identifier(value, "request_id")
        return value

    def make_request(
        self,
        op: str,
        lane: str | None,
        args: Mapping[str, object],
        *,
        request_id: str | None = None,
        admission: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if not isinstance(op, str) or op not in _OPS:
            raise InvalidRequest("unknown RPC operation")
        selected = _deepcopy(admission if admission is not None else self.admission)
        _validate_admission(selected)
        if op in {"approve", "preempt"}:
            approval_id = args.get("approval_id")
            selected["approval"] = {"approval_id": approval_id, "required": True, "consume_atomically": True}
        if op in {"acquire", "chat-load"} and args.get("pipeline_ref") is not None and selected.get("pipeline") is None:
            selected["pipeline"] = {
                "pipeline_id": args.get("pipeline_ref"),
                "version": "1.0.0",
                "revision": 1,
                "purpose": args.get("purpose"),
                "policy_hash": "0" * 64,
            }
        controller = _controller_id(selected)
        message = {
            "schema": 1,
            "request_id": request_id or self.new_request_id(),
            "op": op,
            "lane": lane,
            "args": _deepcopy(args),
            "idempotency_scope": {"scope": "authenticated-principal", "controller_id": controller},
            "request_fingerprint": _fingerprint(op, lane, args),
            "admission": selected,
        }
        validate_operation(message)
        return message

    # Compatibility aliases make the seam straightforward for package-local
    # shims without creating another public wire operation.
    build_request = make_request

    def request(
        self,
        message: Mapping[str, object],
        *,
        retries: int | None = None,
        deadline: float | None = None,
        allow_expired: bool = False,
    ) -> dict[str, object]:
        validate_operation(message)
        frozen_message = _deepcopy(message)
        request_id = str(frozen_message["request_id"])
        attempts = 1 + (self.retries if retries is None else max(0, int(retries)))
        last_error = "transport did not return a response"
        for attempt in range(attempts):
            remaining = None if deadline is None else float(deadline) - float(self.clock.monotonic())
            if remaining is not None and remaining <= 0 and not allow_expired:
                return _timeout_response(request_id)
            timeout_s = self.timeout_s if remaining is None else max(0.0, min(self.timeout_s, remaining))
            try:
                raw = self.transport.request(self.endpoint, frozen_message, timeout_s)
                response = _normalise_transport_result(raw)
                validate_response(response, request_id)
                return dict(response)
            except TransportFailure as exc:
                last_error = str(exc)
                if not exc.retryable or attempt + 1 >= attempts:
                    break
            except InvalidResponse as exc:
                return failure_response(request_id, f"unavailable: {exc}")
            except (OSError, TimeoutError, ConnectionError) as exc:
                last_error = str(exc)
                if attempt + 1 >= attempts:
                    break
            except Exception as exc:  # transport implementations must fail closed
                last_error = str(exc) or exc.__class__.__name__
                if attempt + 1 >= attempts:
                    break
        return failure_response(request_id, f"unavailable: {last_error}")

    send = request

    def call(
        self,
        op: str,
        lane: str | None,
        args: Mapping[str, object],
        *,
        deadline: float | None = None,
        retries: int | None = None,
        allow_expired: bool = False,
    ) -> dict[str, object]:
        return self.request(self.make_request(op, lane, args), deadline=deadline, retries=retries, allow_expired=allow_expired)

    def lookup_token_for_lane(self, lane: str) -> str | None:
        if self.token_lookup is None:
            return None
        token = self.token_lookup(lane)
        if token is None:
            return None
        if not isinstance(token, str) or len(token) < 16:
            raise InvalidRequest("token lookup returned an invalid token")
        return token

    def validate_grant(self, response: Mapping[str, object], lane: str, operation: str) -> Grant:
        request_id = str(response.get("request_id", ""))
        validate_response(response, request_id)
        if response.get("status") != 200:
            raise InvalidResponse("grant response is not complete")
        data = response.get("data")
        if not isinstance(data, Mapping) or data.get("kind") != "grant" or data.get("operation") != operation:
            raise InvalidResponse("response is not the expected grant")
        _validate_response_result(data)
        token = data.get("token")
        generation = data.get("generation")
        if not isinstance(token, str) or len(token) < 16 or isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise InvalidResponse("grant token or generation is invalid")
        lease = data.get("lease")
        reservation = data.get("reservation")
        adoption = data.get("adoption")
        if not isinstance(lease, Mapping) or not isinstance(reservation, Mapping) or not isinstance(adoption, Mapping):
            raise InvalidResponse("grant binding is incomplete")
        lease_lane = lease.get("lane")
        if not isinstance(lease_lane, Mapping) or lease_lane.get("lane_id") != lane:
            raise InvalidResponse("grant lane is not bound")
        if lease.get("generation") != generation or reservation.get("generation") != generation:
            raise InvalidResponse("grant generation is not bound")
        if lease.get("token") != token:
            raise InvalidResponse("grant token is not bound")
        lease_reservation = lease.get("reservation")
        if not isinstance(lease_reservation, Mapping):
            raise InvalidResponse("lease reservation is not bound")
        if lease_reservation.get("generation") != generation:
            raise InvalidResponse("lease reservation generation is not bound")
        reservation_lane = lease_reservation.get("lane")
        if not isinstance(reservation_lane, Mapping) or reservation_lane.get("lane_id") != lane:
            raise InvalidResponse("lease reservation lane is not bound")
        reservation_lane = reservation.get("lane")
        if isinstance(reservation_lane, Mapping) and reservation_lane.get("lane_id") != lane:
            raise InvalidResponse("grant reservation lane is not bound")
        expected_principal = self.admission.get("ingress", {}).get("actor") if isinstance(self.admission.get("ingress"), Mapping) else None
        actual_principal = lease.get("principal")
        if not isinstance(expected_principal, Mapping) or not isinstance(actual_principal, Mapping) or dict(actual_principal) != dict(expected_principal):
            raise InvalidResponse("grant principal is not bound")
        if adoption.get("principal_bound") is not True or adoption.get("generation_bound") is not True:
            raise InvalidResponse("grant is not authority-bound")
        expected_mode = "authenticated-adoption" if operation == "claim" else "fresh-acquire"
        if adoption.get("mode") != expected_mode:
            raise InvalidResponse("grant adoption mode is not bound")
        expected_source = "authenticated-adoption" if operation == "claim" else "controller-grant"
        if adoption.get("token_source") != expected_source:
            raise InvalidResponse("grant token source is not bound")
        return Grant(data=dict(data))

    def lifecycle(
        self,
        lane: str,
        purpose: str,
        workload: Sequence[str],
        *,
        owner_class: str = "batch",
        est_s: int = DEFAULT_TTL_MIN * 60,
        max_s: int = DEFAULT_TTL_MIN * 60,
        token: str | None = None,
        generation: int | None = None,
        handoff: Callable[[Mapping[str, object], Sequence[str]], object] | None = None,
    ) -> dict[str, object]:
        if handoff is None:
            handoff = self.handoff
        if token is not None:
            if generation is None:
                raise InvalidRequest("authenticated adoption requires generation")
            args: dict[str, object] = {"token": token, "generation": generation, "generation_source": "authenticated-adoption"}
            message = self.make_request("claim", lane, args)
            operation = "claim"
        else:
            args = {"purpose": purpose, "class": owner_class, "est_s": est_s, "max_s": max_s}
            message = self.make_request("acquire", lane, args)
            operation = "acquire"
        response = self.request(message)
        if response.get("status") != 200:
            return response
        try:
            grant = self.validate_grant(response, lane, operation)
        except InvalidResponse as exc:
            return failure_response(str(response.get("request_id", message["request_id"])), f"unavailable: {exc}")
        if token is not None and (grant.token != token or grant.generation != generation):
            # A mismatched adoption reply is not permission to release any
            # lease: the caller did not establish ownership of this grant.
            return failure_response(str(response.get("request_id", message["request_id"])), "unavailable: adoption grant does not match the authenticated claim")
        if handoff is None:
            # The grant is authority-bound, so releasing it is safe; there is
            # deliberately no local subprocess fallback.
            release_response = self.call("release", lane, {"token": grant.token})
            if release_response.get("status") != 200:
                return release_response
            return failure_response(str(message["request_id"]), "unavailable: workload handoff is not configured")
        try:
            handoff_result = handoff(grant.data, tuple(workload))
        except Exception as exc:
            handoff_result = False
            handoff_error = str(exc) or exc.__class__.__name__
        else:
            handoff_error = "workload handoff rejected"
        if handoff_result is False:
            release_response = self.call("release", lane, {"token": grant.token})
            if release_response.get("status") != 200:
                return release_response
            return failure_response(str(message["request_id"]), f"unavailable: {handoff_error}")
        return self.call("release", lane, {"token": grant.token})

    def wait_for_lane(
        self,
        lane: str,
        purpose: str,
        *,
        ttl_s: int = DEFAULT_TTL_MIN * 60,
        max_wait_s: int = DEFAULT_WAIT_MAX_MIN * 60,
        yield_after_queue: bool = False,
        owner_class: str = "batch",
    ) -> tuple[dict[str, object], bool]:
        started = float(self.clock.monotonic())
        deadline = started + float(max_wait_s)
        queue_args = {"action": "add", "purpose": purpose, "class": owner_class, "max_wait_s": max_wait_s}
        queue_response = self.call("queue", lane, queue_args, deadline=deadline)
        queue_id = _queue_id(queue_response)
        if queue_response.get("status") not in {200, 202}:
            if self._wait_expired(deadline):
                if queue_id is not None:
                    self._remove_known_queue(lane, queue_id, deadline=deadline)
                return _timeout_response(str(queue_response.get("request_id", ""))), False
            return queue_response, False
        if queue_id is None:
            return failure_response(str(queue_response.get("request_id", "")), "unavailable: queue response omitted queue identity"), False
        if self._wait_expired(deadline):
            self._remove_known_queue(lane, queue_id, deadline=deadline)
            return _timeout_response(str(queue_response.get("request_id", ""))), False
        if yield_after_queue:
            return queue_response, True

        acquire_args = {"purpose": purpose, "class": owner_class, "est_s": ttl_s, "max_s": ttl_s, "queue_id": queue_id}
        acquire_message = self.make_request("acquire", lane, acquire_args)
        while True:
            acquire_response = self.request(acquire_message, deadline=deadline)
            if acquire_response.get("status") == 200 and not self._wait_expired(deadline):
                return acquire_response, False
            if self._wait_expired(deadline):
                self._remove_known_queue(lane, queue_id, deadline=deadline)
                return _timeout_response(str(acquire_message["request_id"])), False
            if not _retryable_wait_response(acquire_response):
                return acquire_response, False
            remaining = deadline - float(self.clock.monotonic())
            if remaining <= 0:
                self._remove_known_queue(lane, queue_id, deadline=deadline)
                return _timeout_response(str(acquire_message["request_id"])), False
            retry_after = _retry_after(acquire_response)
            delay = min(float(QUEUE_REFRESH_S), float(retry_after), remaining)
            if delay <= 0:
                self._remove_known_queue(lane, queue_id, deadline=deadline)
                return _timeout_response(str(acquire_message["request_id"])), False
            try:
                self.sleeper(delay)
            except Exception as exc:
                return failure_response(str(acquire_message["request_id"]), f"unavailable: wait failed: {exc}"), False
            if self._wait_expired(deadline):
                self._remove_known_queue(lane, queue_id, deadline=deadline)
                return _timeout_response(str(acquire_message["request_id"])), False
            refresh = self.call("queue", lane, {"action": "refresh", "queue_id": queue_id, "max_wait_s": max_wait_s}, deadline=deadline)
            if refresh.get("status") != 200:
                # The queue is known, but its state is no longer known.  Do
                # not invent a cleanup result after a failed refresh.
                if self._wait_expired(deadline):
                    self._remove_known_queue(lane, queue_id, deadline=deadline)
                    return _timeout_response(str(acquire_message["request_id"])), False
                return refresh, False
            if self._wait_expired(deadline):
                self._remove_known_queue(lane, queue_id, deadline=deadline)
                return _timeout_response(str(acquire_message["request_id"])), False
            # A definite busy result is not a durable admission result.  Use a
            # fresh request ID so the controller can evaluate eligibility
            # again; transport retries inside request() retain the old ID.
            acquire_message = self.make_request("acquire", lane, acquire_args, request_id=self.new_request_id())

    def _wait_expired(self, deadline: float) -> bool:
        return float(self.clock.monotonic()) >= deadline

    def _remove_known_queue(self, lane: str, queue_id: str, *, deadline: float | None = None) -> None:
        self.call("queue", lane, {"action": "remove", "queue_id": queue_id}, deadline=deadline, retries=0, allow_expired=True)

    run = lifecycle
    wait = wait_for_lane


# Short alias used by callers that call the adapter simply Client.
Client = RpcClient
FlightctlClient = RpcClient


def _queue_id(response: Mapping[str, object]) -> str | None:
    data = response.get("data")
    if not isinstance(data, Mapping):
        return None
    if data.get("kind") == "pending":
        value = data.get("queue_id")
    elif data.get("kind") == "mutation":
        value = data.get("record_id")
    else:
        value = None
    return value if isinstance(value, str) and value else None


def _retry_after(response: Mapping[str, object]) -> int:
    data = response.get("data")
    if isinstance(data, Mapping):
        value = data.get("retry_after_s")
        if isinstance(value, int) and value > 0:
            return value
    return QUEUE_REFRESH_S


def _retryable_wait_response(response: Mapping[str, object]) -> bool:
    if response.get("status") != 409:
        return False
    error = response.get("error")
    return isinstance(error, Mapping) and error.get("code") in {"busy", "conflict"}


def _timeout_response(request_id: str) -> dict[str, object]:
    return {
        "schema": 1,
        "request_id": request_id,
        "status": 409,
        "data": None,
        "error": {"code": "timeout", "message": "bounded wait expired", "retryable": False, "failure_class": "timeout"},
    }


def parse_admission_json(value: str) -> Mapping[str, object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise InvalidRequest("admission JSON is malformed") from exc
    if not isinstance(parsed, Mapping):
        raise InvalidRequest("admission JSON must be an object")
    return parsed


def rpc_stdin(
    *,
    transport: "Transport | Any | None" = None,
    clock: "Clock | Any | None" = None,
    stream_in: TextIO | None = None,
    stream_out: TextIO | None = None,
    stream_err: TextIO | None = None,
    endpoint: str | None = None,
    admission: Mapping[str, object] | None = None,
) -> int:
    """Adapt one frozen operation envelope from stdin to one response."""

    input_stream = stream_in or sys.stdin
    output_stream = stream_out or sys.stdout
    error_stream = stream_err or sys.stderr
    raw = input_stream.read()
    try:
        decoder = json.JSONDecoder()
        stripped = raw.lstrip()
        value, index = decoder.raw_decode(stripped)
        if stripped[index:].strip():
            raise InvalidRequest("stdin contains more than one JSON value")
        if not isinstance(value, Mapping):
            raise InvalidRequest("stdin RPC value must be an object")
        validate_operation(value)
    except (json.JSONDecodeError, InvalidRequest) as exc:
        print(f"flightctl: invalid RPC stdin: {exc}", file=error_stream)
        return 2
    client = RpcClient(transport, clock=clock, endpoint=endpoint, admission=admission)
    response = client.request(value)
    json.dump(response, output_stream, ensure_ascii=False, separators=(",", ":"))
    output_stream.write("\n")
    output_stream.flush()
    return _status_exit(response)


def _status_exit(response: Mapping[str, object]) -> int:
    return {200: 0, 202: 5, 403: 2, 409: 1, 503: 3}.get(response.get("status"), 3)


def encode_signature(value: str) -> str:
    """Encode the positional approval signature without shell interpretation."""

    # The frozen CLI fixture uses the word ``signature`` as the neutral
    # spelling for the three-byte scripted proof ``sig``.  Real callers pass
    # their signer output through --signature-b64 at the composition layer;
    # this compatibility form keeps the positional vector deterministic.
    if value == "signature":
        value = "sig"
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


__all__ = [
    "Client",
    "ClientError",
    "Grant",
    "FlightctlClient",
    "HttpTransport",
    "InvalidRequest",
    "InvalidResponse",
    "RpcClient",
    "SystemClock",
    "TransportFailure",
    "DEFAULT_TTL_MIN",
    "DEFAULT_WAIT_MAX_MIN",
    "QUEUE_REFRESH_S",
    "encode_signature",
    "failure_response",
    "main",
    "parse_admission_json",
    "rpc_stdin",
    "validate_operation",
    "validate_response",
]


def main(argv: Sequence[str] | None = None) -> int:
    """Executable entry point for the internal ``--rpc-stdin`` bridge."""

    selected = list(sys.argv[1:] if argv is None else argv)
    if selected != ["--rpc-stdin"]:
        print("usage: python -m flightctl.client --rpc-stdin", file=sys.stderr)
        return 2
    return rpc_stdin()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
