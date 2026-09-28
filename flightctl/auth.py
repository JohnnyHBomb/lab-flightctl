"""Authentication and canonical approval-proof helpers for the controller.

The module deliberately has no transport or controller dependency.  It contains
the small amount of canonicalisation needed by the v1 approval contract and a
stdlib-only Ed25519 verifier.  A caller may provide a hardware/security-key
verifier, but a proof is never accepted merely because a request says that it
was verified.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping


DOMAIN = "flightctl/approval/v1"
APPROVAL_SIGNED_FIELDS = (
    "id",
    "action",
    "requester",
    "lane",
    "booking_id",
    "revision",
    "target_generation",
    "bounds",
    "reason",
    "nonce",
    "expires",
    "destination_site",
    "controller_id",
    "payload_hash",
    "manifest_hash",
    "policy_hash",
    "challenge_id",
    "challenge_nonce",
)


class AuthError(ValueError):
    """A supplied identity, proof, or canonical binding is not acceptable."""


class UnsupportedProof(AuthError):
    """The proof uses a scheme or encoding this controller does not support."""


def _utf16_sort_key(value: str) -> bytes:
    """RFC 8785 object ordering is lexicographic UTF-16 code-unit ordering."""

    return value.encode("utf-16-be", "surrogatepass")


def _json_string(value: str) -> str:
    # JSON.stringify/JCS leaves non-control Unicode characters unescaped.  In
    # particular, ensure_ascii=True is not canonical JCS output.
    out = ['"']
    for char in value:
        code = ord(char)
        if char == '"':
            out.append('\\"')
        elif char == "\\":
            out.append("\\\\")
        elif code == 0x08:
            out.append("\\b")
        elif code == 0x09:
            out.append("\\t")
        elif code == 0x0A:
            out.append("\\n")
        elif code == 0x0C:
            out.append("\\f")
        elif code == 0x0D:
            out.append("\\r")
        elif code < 0x20:
            out.append(f"\\u{code:04x}")
        elif 0xD800 <= code <= 0xDFFF:
            raise AuthError("unpaired surrogate is not valid JCS text")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _number(value: int | float) -> str:
    """Render a Python number using the ECMAScript/JCS number spelling.

    JSON inputs used by the controller are I-JSON numbers.  Python's shortest
    round-trip float spelling is the same digit selection as modern ECMAScript;
    this function supplies the different fixed/scientific cut-over and exponent
    spelling required by JSON.stringify.
    """

    if isinstance(value, bool):  # bool is an int subclass
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, float) or not math.isfinite(value):
        raise AuthError("JCS cannot encode non-finite numbers")
    if value == 0.0:
        return "0"
    negative = value < 0
    raw = repr(abs(value)).lower()
    if "e" in raw:
        mantissa, exponent_text = raw.split("e", 1)
        exponent = int(exponent_text)
    else:
        mantissa, exponent = raw, 0
    if "." in mantissa:
        whole, fraction = mantissa.split(".", 1)
    else:
        whole, fraction = mantissa, ""
    combined = whole + fraction
    leading_zeroes = len(combined) - len(combined.lstrip("0"))
    digits = combined.lstrip("0") or "0"
    if exponent == 0 and fraction:
        digits = digits.rstrip("0") or "0"
    decimal_position = len(whole) + exponent - leading_zeroes
    # ECMAScript uses decimal notation for [1e-6, 1e21).
    if -6 <= decimal_position - 1 < 21:
        if decimal_position <= 0:
            rendered = "0." + ("0" * (-decimal_position)) + digits
        elif decimal_position >= len(digits):
            rendered = digits + ("0" * (decimal_position - len(digits)))
        else:
            rendered = digits[:decimal_position] + "." + digits[decimal_position:]
    else:
        exponent_out = decimal_position - 1
        mantissa_out = digits[0]
        if len(digits) > 1:
            mantissa_out += "." + digits[1:]
        rendered = mantissa_out + "e" + ("+" if exponent_out >= 0 else "") + str(exponent_out)
    return ("-" if negative else "") + rendered


def canonical_json(value: Any) -> str:
    """Return deterministic RFC 8785 JSON text for JSON-compatible values."""

    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _json_string(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _number(value)
    if isinstance(value, Mapping):
        keys = list(value)
        if any(not isinstance(key, str) for key in keys):
            raise AuthError("JCS object keys must be strings")
        keys.sort(key=_utf16_sort_key)
        return "{" + ",".join(_json_string(key) + ":" + canonical_json(value[key]) for key in keys) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    raise AuthError(f"unsupported JCS value: {type(value).__name__}")


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def approval_projection(approval: Mapping[str, Any]) -> list[list[str | Any]]:
    """Build the fixed, ordered signed projection, retaining explicit nulls."""

    return [[field, approval.get(field)] for field in APPROVAL_SIGNED_FIELDS]


def approval_canonical_bytes(approval: Mapping[str, Any]) -> bytes:
    return canonical_bytes(approval_projection(approval))


def approval_signing_bytes(approval: Mapping[str, Any]) -> bytes:
    """Bytes hashed before the security-key signature is checked."""

    return DOMAIN.encode("utf-8") + b"\0" + approval_canonical_bytes(approval)


def approval_digest(approval: Mapping[str, Any]) -> bytes:
    return hashlib.sha256(approval_signing_bytes(approval)).digest()


# These aliases make the normative operation easy to discover without making
# callers guess whether they need the projection, domain bytes, or digest.
canonical_approval_bytes = approval_canonical_bytes
approval_message = approval_digest


# ---- Minimal Ed25519 implementation (RFC 8032) -------------------------

_ED_Q = 2**255 - 19
_ED_L = 2**252 + 27742317777372353535851937790883648493
_ED_D = (-121665 * pow(121666, _ED_Q - 2, _ED_Q)) % _ED_Q
_ED_I = pow(2, (_ED_Q - 1) // 4, _ED_Q)


def _ed_xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_ED_D * y * y + 1, _ED_Q - 2, _ED_Q) % _ED_Q
    x = pow(xx, (_ED_Q + 3) // 8, _ED_Q)
    if (x * x - xx) % _ED_Q:
        x = (x * _ED_I) % _ED_Q
    if x & 1:
        x = _ED_Q - x
    return x


_ED_B = (_ed_xrecover(4 * pow(5, _ED_Q - 2, _ED_Q)) , 4 * pow(5, _ED_Q - 2, _ED_Q) % _ED_Q)


def _ed_inv(value: int) -> int:
    return pow(value, _ED_Q - 2, _ED_Q)


def _ed_add(point_a: tuple[int, int], point_b: tuple[int, int]) -> tuple[int, int]:
    x1, y1 = point_a
    x2, y2 = point_b
    denominator_x = _ed_inv(1 + _ED_D * x1 * x2 * y1 * y2)
    denominator_y = _ed_inv(1 - _ED_D * x1 * x2 * y1 * y2)
    return ((x1 * y2 + x2 * y1) * denominator_x % _ED_Q, (y1 * y2 + x1 * x2) * denominator_y % _ED_Q)


def _ed_scalarmult(point: tuple[int, int], scalar: int) -> tuple[int, int]:
    result = (0, 1)
    addend = point
    while scalar:
        if scalar & 1:
            result = _ed_add(result, addend)
        addend = _ed_add(addend, addend)
        scalar >>= 1
    return result


def _ed_encode(point: tuple[int, int]) -> bytes:
    x, y = point
    encoded = bytearray(y.to_bytes(32, "little"))
    encoded[31] |= (x & 1) << 7
    return bytes(encoded)


def _ed_decode(encoded: bytes) -> tuple[int, int]:
    if len(encoded) != 32:
        raise AuthError("invalid Ed25519 public point length")
    value = int.from_bytes(encoded, "little")
    sign = value >> 255
    y = value & ((1 << 255) - 1)
    if y >= _ED_Q:
        raise AuthError("non-canonical Ed25519 public point")
    x = _ed_xrecover(y)
    if (x & 1) != sign:
        x = _ED_Q - x
    # Edwards25519 has a=-1: -x² + y² = 1 + d x²y².
    if (y * y - x * x - 1 - _ED_D * x * x * y * y) % _ED_Q:
        raise AuthError("invalid Ed25519 public point")
    return x, y


def ed25519_public_key(seed: bytes) -> bytes:
    if len(seed) != 32:
        raise AuthError("Ed25519 seed must be 32 bytes")
    digest = hashlib.sha512(seed).digest()
    scalar = int.from_bytes(digest[:32], "little")
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    return _ed_encode(_ed_scalarmult(_ED_B, scalar))


def ed25519_sign(seed: bytes, message: bytes) -> bytes:
    if len(seed) != 32:
        raise AuthError("Ed25519 seed must be 32 bytes")
    expanded = hashlib.sha512(seed).digest()
    scalar = int.from_bytes(expanded[:32], "little")
    scalar &= (1 << 254) - 8
    scalar |= 1 << 254
    nonce = int.from_bytes(hashlib.sha512(expanded[32:] + message).digest(), "little") % _ED_L
    encoded_r = _ed_encode(_ed_scalarmult(_ED_B, nonce))
    public = ed25519_public_key(seed)
    challenge = int.from_bytes(hashlib.sha512(encoded_r + public + message).digest(), "little") % _ED_L
    return encoded_r + ((nonce + challenge * scalar) % _ED_L).to_bytes(32, "little")


def ed25519_verify(public_key: bytes, signature: bytes, message: bytes) -> bool:
    try:
        if len(public_key) != 32 or len(signature) != 64:
            return False
        point_a = _ed_decode(public_key)
        point_r = _ed_decode(signature[:32])
        scalar_s = int.from_bytes(signature[32:], "little")
        if scalar_s >= _ED_L:
            return False
        challenge = int.from_bytes(hashlib.sha512(signature[:32] + public_key + message).digest(), "little") % _ED_L
        return _ed_encode(_ed_scalarmult(_ED_B, scalar_s)) == _ed_encode(_ed_add(point_r, _ed_scalarmult(point_a, challenge)))
    except (AuthError, ValueError, ZeroDivisionError):
        return False


def _b64(value: str) -> bytes:
    if not isinstance(value, str) or not value or not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", value):
        raise AuthError("malformed base64 proof encoding")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise AuthError("malformed base64 proof encoding") from exc


def _ssh_string(data: bytes, offset: int = 0) -> tuple[bytes, int]:
    if offset + 4 > len(data):
        raise AuthError("truncated SSH blob")
    length = struct.unpack(">I", data[offset : offset + 4])[0]
    end = offset + 4 + length
    if end > len(data):
        raise AuthError("truncated SSH blob")
    return data[offset + 4 : end], end


def _application_bytes(value: Any) -> bytes:
    if isinstance(value, str):
        value = value.encode("utf-8")
    if not isinstance(value, (bytes, bytearray)) or not value:
        raise AuthError("missing security-key application")
    return bytes(value)


def _public_key_details(value: Any) -> tuple[bytes, bytes | None]:
    configured_application = None
    if isinstance(value, Mapping):
        configured_application = value.get("application")
        value = value.get("public_key", value.get("key"))
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("ssh-ed25519 ") or stripped.startswith("sk-ssh-ed25519@openssh.com "):
            stripped = stripped.split()[1]
        if stripped.startswith("-----BEGIN"):
            body = "".join(line.strip() for line in stripped.splitlines() if not line.startswith("---"))
            der = base64.b64decode(body, validate=True)
            marker = b"\x03\x21\x00"
            position = der.find(marker)
            if position >= 0 and len(der) >= position + len(marker) + 32:
                key = der[position + len(marker) : position + len(marker) + 32]
                return key, _application_bytes(configured_application) if configured_application is not None else None
        try:
            value = base64.b64decode(stripped, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise AuthError("malformed public key encoding") from exc
    if not isinstance(value, (bytes, bytearray)):
        raise AuthError("missing public key")
    raw = bytes(value)
    if len(raw) == 32:
        return raw, _application_bytes(configured_application) if configured_application is not None else None
    # OpenSSH public key wire format: string algorithm, string key, and for
    # security keys a registered application string.
    try:
        algorithm, offset = _ssh_string(raw)
        key, offset = _ssh_string(raw, offset)
        if algorithm == b"sk-ssh-ed25519@openssh.com" and len(key) == 32:
            application, offset = _ssh_string(raw, offset)
            if offset != len(raw) or not application:
                raise AuthError("invalid security-key application encoding")
            if configured_application is not None and _application_bytes(configured_application) != application:
                raise AuthError("registered security-key application mismatch")
            return key, application
        if algorithm == b"ssh-ed25519" and len(key) == 32 and offset == len(raw):
            return key, _application_bytes(configured_application) if configured_application is not None else None
    except AuthError:
        pass
    raise AuthError("unsupported Ed25519 public key format")


def _public_key_bytes(value: Any) -> bytes:
    return _public_key_details(value)[0]


def _security_key_signature(proof: Mapping[str, Any]) -> tuple[bytes, int, int]:
    """Decode an OpenSSH security-key signature and its signed FIDO evidence.

    The security-key signature is not an ordinary Ed25519 signature over the
    approval digest.  OpenSSH's sk-ssh-ed25519 format carries the authenticator
    flags and counter after the signature string; the authenticator signs the
    application hash, those five bytes, extensions, and SHA-256(message).
    Keeping the flags inside the signed input prevents caller-supplied
    touch/PIN claims from becoming authority.
    """

    encoded = proof.get("signature_b64")
    raw = _b64(encoded)
    try:
        algorithm, offset = _ssh_string(raw)
        signature, offset = _ssh_string(raw, offset)
        if algorithm != b"sk-ssh-ed25519@openssh.com" or len(signature) != 64 or len(raw) != offset + 5:
            raise AuthError("unsupported SSH signature blob")
        flags = raw[offset]
        counter = struct.unpack(">I", raw[offset + 1 : offset + 5])[0]
        if flags & 0x80:
            raise AuthError("security-key proof contains unsupported extensions")
        if not flags & 0x01:
            raise AuthError("security-key proof lacks user-presence evidence")
        if not flags & 0x04:
            raise AuthError("security-key proof lacks user-verification evidence")
        return signature, flags, counter
    except AuthError:
        pass
    raise AuthError("unsupported or malformed SSH security-key signature encoding")


@dataclass(frozen=True)
class KeyRecord:
    public_key: Any
    verifier: Callable[[bytes, bytes], bool] | None = None
    evidence: Mapping[str, Any] | None = None
    application: Any | None = None


@dataclass(frozen=True)
class AuthenticatedPeer:
    peer: str
    external_id: str
    principal: Mapping[str, Any]
    roles: tuple[str, ...] = ()
    device_id: str | None = None


class ApprovalVerifier:
    """Verify a proof against a controller-owned key registry."""

    def __init__(self, keys: Mapping[str, Any] | None = None, *, verifier: Callable[..., Any] | None = None) -> None:
        self.keys: dict[str, KeyRecord] = {}
        for key_id, value in (keys or {}).items():
            if isinstance(value, KeyRecord):
                self.keys[str(key_id)] = value
            elif isinstance(value, Mapping) and ("public_key" in value or "key" in value):
                self.keys[str(key_id)] = KeyRecord(value.get("public_key", value.get("key")), value.get("verifier"), value.get("evidence"), value.get("application"))
            else:
                self.keys[str(key_id)] = KeyRecord(value)
        self.verifier = verifier

    def register(self, key_id: str, public_key: Any, *, evidence: Mapping[str, Any] | None = None, verifier: Callable[[bytes, bytes], bool] | None = None, application: Any | None = None) -> None:
        self.keys[key_id] = KeyRecord(public_key, verifier, evidence, application)

    def verify(self, proof: Mapping[str, Any], message: bytes, *, evidence: Mapping[str, Any] | None = None, now: datetime | None = None, require_evidence: bool = True) -> dict[str, Any]:
        if not isinstance(proof, Mapping):
            raise AuthError("proof must be an object")
        scheme = proof.get("scheme")
        key_id = proof.get("key_id")
        if scheme == "webauthn":
            # The P1 transport-neutral boundary does not yet bind WebAuthn
            # clientData challenge/origin, RP ID hash, and authenticator flags
            # to this approval.  Treating an assertion as an approval before
            # all of those bindings are checked would be an authorization
            # bypass, so the unsupported scheme is explicit and fail-closed.
            raise UnsupportedProof("WebAuthn approval proofs are not enabled")
        if scheme != "ssh-sk":
            raise UnsupportedProof("unsupported proof scheme")
        if not isinstance(key_id, str) or key_id not in self.keys:
            raise AuthError("unknown approval key")
        if proof.get("namespace") != DOMAIN or proof.get("encoding") != "openssh-ssh-sk-signature/base64":
            raise AuthError("invalid SSH security-key proof metadata")
        record = self.keys[key_id]
        signature, flags, counter = _security_key_signature(proof)
        public_key, application = _public_key_details(record.public_key)
        if record.application is not None:
            configured_application = _application_bytes(record.application)
            if application is not None and application != configured_application:
                raise AuthError("registered security-key application mismatch")
            application = configured_application
        if application is None and isinstance(record.evidence, Mapping) and record.evidence.get("application") is not None:
            application = _application_bytes(record.evidence["application"])
        if application is None:
            raise AuthError("registered security-key application is required")
        signed_message = hashlib.sha256(application).digest() + bytes([flags]) + counter.to_bytes(4, "big") + hashlib.sha256(message).digest()
        # The registered public key is the cryptographic authority.  Optional
        # controller callbacks may add policy checks, but a callback result
        # can never replace the real offline signature verification.
        verified = ed25519_verify(public_key, signature, signed_message)
        if not verified:
            raise AuthError("approval signature verification failed")
        if record.verifier is not None:
            if not bool(record.verifier(signed_message, signature)):
                raise AuthError("approval verifier policy rejected proof")
        elif self.verifier is not None:
            try:
                result = self.verifier(proof, signed_message, signature)
            except TypeError:
                result = self.verifier(proof, signed_message)
            if isinstance(result, Mapping):
                verified = bool(result.get("verified"))
            else:
                verified = bool(result)
            if not verified:
                raise AuthError("approval verifier policy rejected proof")

        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        supplied = dict(evidence or {})
        expected = dict(record.evidence or {})
        expected_verifier = str(expected.get("verifier", key_id))
        expected_presence = "verified" if flags & 0x01 else "absent"
        expected_verification = "verified" if flags & 0x04 else "absent"
        # Presence and verification are derived from the signed authenticator
        # flags.  A registered key may constrain them, but neither the
        # approval request nor the caller's evidence can manufacture them.
        if expected.get("user_presence") is not None and str(expected["user_presence"]) != expected_presence:
            raise AuthError("registered key requires different user-presence evidence")
        if expected.get("user_verification") is not None and str(expected["user_verification"]) != expected_verification:
            raise AuthError("registered key requires different user-verification evidence")
        if require_evidence and supplied.get("verifier", expected_verifier) != expected_verifier:
            raise AuthError("verification evidence verifier mismatch")
        if supplied.get("user_presence") is not None and supplied.get("user_presence") != expected_presence:
            raise AuthError("verification evidence user-presence mismatch")
        if supplied.get("user_verification") is not None and supplied.get("user_verification") != expected_verification:
            raise AuthError("verification evidence user-verification mismatch")
        supplied_at = supplied.get("verified_at")
        if supplied_at is not None:
            parsed = _parse_utc(supplied_at)
            if parsed > current + timedelta(seconds=30):
                raise AuthError("verification evidence is from the future")
        return {
            "verifier": expected_verifier,
            "verified_at": _utc_text(current),
            "user_presence": expected_presence,
            "user_verification": expected_verification,
        }


def verify_approval_proof(proof: Mapping[str, Any], approval: Mapping[str, Any], keys: Mapping[str, Any], *, evidence: Mapping[str, Any] | None = None, now: datetime | None = None) -> dict[str, Any]:
    return ApprovalVerifier(keys).verify(proof, approval_digest(approval), evidence=evidence, now=now)


def verify_signature(proof: Mapping[str, Any], message: bytes, keys: Mapping[str, Any], *, evidence: Mapping[str, Any] | None = None, now: datetime | None = None, require_evidence: bool = True) -> dict[str, Any]:
    """Convenience entry point for callers that already have signing bytes."""

    return ApprovalVerifier(keys).verify(proof, message, evidence=evidence, now=now, require_evidence=require_evidence)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str):
        raise AuthError("invalid UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuthError("invalid UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise AuthError("timestamp must be UTC")
    return parsed.astimezone(timezone.utc)


# ---- WebAuthn ES256 verification ----------------------------------------

_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_P_A = _P - 3
_P_B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
_P_G = (
    0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
    0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5,
)
_P_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551


def _p_add(a: tuple[int, int] | None, b: tuple[int, int] | None) -> tuple[int, int] | None:
    if a is None:
        return b
    if b is None:
        return a
    x1, y1 = a
    x2, y2 = b
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    if a == b:
        slope = (3 * x1 * x1 + _P_A) * pow(2 * y1, _P - 2, _P) % _P
    else:
        slope = (y2 - y1) * pow(x2 - x1, _P - 2, _P) % _P
    x3 = (slope * slope - x1 - x2) % _P
    return x3, (slope * (x1 - x3) - y1) % _P


def _p_mul(point: tuple[int, int] | None, scalar: int) -> tuple[int, int] | None:
    result = None
    addend = point
    while scalar:
        if scalar & 1:
            result = _p_add(result, addend)
        addend = _p_add(addend, addend)
        scalar >>= 1
    return result


def _der_signature(raw: bytes) -> tuple[int, int]:
    if len(raw) < 8 or raw[0] != 0x30:
        raise AuthError("invalid ECDSA signature")
    length = raw[1]
    offset = 2
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or offset + count >= len(raw):
            raise AuthError("invalid ECDSA signature length")
        length = int.from_bytes(raw[offset : offset + count], "big")
        offset += count
    if offset + length != len(raw) or raw[offset] != 2:
        raise AuthError("invalid ECDSA signature sequence")
    r_len = raw[offset + 1]
    r_start = offset + 2
    r_end = r_start + r_len
    if r_end + 2 > len(raw) or raw[r_end] != 2:
        raise AuthError("invalid ECDSA r")
    s_len = raw[r_end + 1]
    s_start = r_end + 2
    if s_start + s_len != len(raw):
        raise AuthError("invalid ECDSA s")
    return int.from_bytes(raw[r_start:r_end], "big"), int.from_bytes(raw[s_start : s_start + s_len], "big")


def _p_public_key(value: Any) -> tuple[int, int]:
    raw = _public_key_bytes(value) if not isinstance(value, (bytes, bytearray)) or len(value) != 65 else bytes(value)
    if len(raw) != 65 or raw[0] != 4:
        raise AuthError("WebAuthn public key must be uncompressed P-256")
    point = int.from_bytes(raw[1:33], "big"), int.from_bytes(raw[33:], "big")
    if (point[1] * point[1] - (point[0] ** 3 + _P_A * point[0] + _P_B)) % _P:
        raise AuthError("invalid P-256 public key")
    return point


def _webauthn_verify(public_key: Any, proof: Mapping[str, Any], signature: bytes) -> bool:
    try:
        client = _b64(str(proof.get("client_data_json_b64", "")))
        authenticator = _b64(str(proof.get("authenticator_data_b64", "")))
        r, s = _der_signature(signature)
        if not (1 <= r < _P_N and 1 <= s < _P_N) or len(authenticator) < 37:
            return False
        digest = hashlib.sha256(authenticator + hashlib.sha256(client).digest()).digest()
        z = int.from_bytes(digest, "big")
        inverse = pow(s, _P_N - 2, _P_N)
        point = _p_add(_p_mul(_P_G, (z * inverse) % _P_N), _p_mul(_p_public_key(public_key), (r * inverse) % _P_N))
        return point is not None and point[0] % _P_N == r
    except (AuthError, ValueError, TypeError, binascii.Error):
        return False


class PeerAuthenticationError(AuthError):
    """The actual ingress peer could not be mapped to a configured principal."""


class PeerAuthenticator:
    """Map an actual socket peer through an operator-owned whois result."""

    def __init__(self, identity_mapping: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]] | None = None, *, whois: Callable[[str], Mapping[str, Any]] | Any | None = None) -> None:
        self.whois = whois
        self.mapping: dict[str, Mapping[str, Any]] = {}
        if isinstance(identity_mapping, Mapping):
            items = identity_mapping.items()
            for key, value in items:
                entry = dict(value)
                entry.setdefault("external_id", str(key))
                self.mapping[str(key)] = entry
        else:
            for entry in identity_mapping or ():
                if "external_id" in entry:
                    self.mapping[str(entry["external_id"])] = dict(entry)

    def _whois(self, peer: str) -> Mapping[str, Any]:
        if self.whois is None:
            return {"external_id": peer}
        try:
            if callable(self.whois):
                result = self.whois(peer)
            elif hasattr(self.whois, "lookup"):
                result = self.whois.lookup(peer)
            else:
                raise PeerAuthenticationError("invalid peer lookup service")
        except Exception as exc:
            raise PeerAuthenticationError("peer lookup failed") from exc
        if not isinstance(result, Mapping):
            raise PeerAuthenticationError("peer lookup returned no identity")
        return result

    def authenticate(self, peer: str) -> AuthenticatedPeer:
        if not isinstance(peer, str) or not peer:
            raise PeerAuthenticationError("missing ingress peer")
        result = self._whois(peer)
        external = str(result.get("external_id") or result.get("node") or result.get("user") or result.get("tag") or peer)
        entry = self.mapping.get(external) or self.mapping.get(peer)
        if entry is None and isinstance(result.get("principal"), Mapping):
            # A whois result may carry a configured principal, but only the
            # operator-owned lookup result is trusted; RPC headers never enter.
            entry = dict(result)
        if entry is None or not isinstance(entry.get("principal"), Mapping):
            raise PeerAuthenticationError("unmapped ingress peer")
        principal = dict(entry["principal"])
        roles = tuple(str(item) for item in entry.get("roles", result.get("roles", ())) or ())
        device_id = entry.get("device_id", result.get("device_id"))
        return AuthenticatedPeer(peer=peer, external_id=external, principal=principal, roles=roles, device_id=str(device_id) if device_id is not None else None)


def authenticate_peer(peer: str, identity_mapping: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]], *, whois: Callable[[str], Mapping[str, Any]] | Any | None = None) -> AuthenticatedPeer:
    return PeerAuthenticator(identity_mapping, whois=whois).authenticate(peer)


# Familiar spellings for small embedding callers; the implementation remains
# one canonical operation.
jcs_dumps = canonical_json
jcs_bytes = canonical_bytes
canonicalize = canonical_json
approval_signing_digest = approval_digest
PeerLookup = PeerAuthenticator
Auth = ApprovalVerifier
