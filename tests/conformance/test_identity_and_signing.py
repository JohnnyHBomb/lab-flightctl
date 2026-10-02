"""PeerIdentity and Signer conformance."""

import pytest

from .conftest import port_params



def _impl(kind, factory):
    from . import registry
    return factory(registry.target())


@pytest.mark.parametrize("kind,factory", port_params("peer_identity"))
def test_known_peer_resolves_to_external_ids(kind, factory) -> None:
    ident = _impl(kind, factory)
    result = ident.resolve(ident.test_peer, timeout_s=10)
    assert result["ok"] is True
    kinds = {e["kind"] for e in result["external_ids"]}
    assert {"tailnet-node", "tailnet-login"} <= kinds or "tailnet-tag" in kinds


@pytest.mark.parametrize("kind,factory", port_params("peer_identity"))
def test_unknown_peer_and_timeout_deny(kind, factory) -> None:
    ident = _impl(kind, factory)
    result = ident.resolve("192.0.2.1:9", timeout_s=5)
    assert result["ok"] is False and result["error"]["code"] in {"not_found", "timeout", "unknown"}


@pytest.mark.parametrize("kind,factory", port_params("signer"))
def test_signature_verifies_with_the_v1_verifier(kind, factory) -> None:
    signer = _impl(kind, factory)
    proof = signer.sign(b"flightctl/approval/v2\x00" + b"\x00" * 32, key_ref=signer.test_key, namespace="flightctl/approval/v2", timeout_s=60)
    assert proof["scheme"] in {"ssh-sk", "webauthn"}
    assert signer.verify_with_authority(proof) is True  # binds to flightctl/auth.py's real verifier (slice C3)
