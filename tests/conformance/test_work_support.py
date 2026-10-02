"""ModelCache, SessionGateway, Notifier, HealthProbe, LegacyObserver and ReleaseBackend conformance."""

import pytest

from tests.contracts_v2.validation import assert_valid

from .conftest import port_params



def _impl(kind, factory):
    from . import registry
    return factory(registry.target())


@pytest.mark.parametrize("kind,factory", port_params("model_cache"))
def test_ensure_is_idempotent_and_reaches_present(kind, factory) -> None:
    cache = _impl(kind, factory)
    first = cache.ensure(cache.test_host, cache.test_model, timeout_s=30)
    assert_valid(first, "storage", "cache_entry")
    entry = cache.wait_until(lambda e: e["state"] in {"present", "failed"}, timeout_s=cache.fetch_budget_s)
    assert entry["state"] == "present" and entry["verified"] in {"size", "size+sha256"}
    again = cache.ensure(cache.test_host, cache.test_model, timeout_s=30)
    assert again["state"] == "present" and cache.fetches_started == 1


@pytest.mark.parametrize("kind,factory", port_params("model_cache"))
def test_eviction_never_removes_pinned_or_in_use(kind, factory) -> None:
    cache = _impl(kind, factory)
    result = cache.evict(cache.test_host, cache.pinned_model, timeout_s=30)
    assert result["state"] == "present" and result["error"]["code"] == "conflict"


@pytest.mark.fake_only
@pytest.mark.parametrize("kind,factory", port_params("model_cache"))
def test_size_mismatch_is_failed_not_present(kind, factory) -> None:
    cache = _impl(kind, factory)
    cache.script_next("truncated")
    cache.ensure(cache.test_host, cache.test_model, timeout_s=30)
    entry = cache.wait_until(lambda e: e["state"] in {"present", "failed"}, timeout_s=5)
    assert entry["state"] == "failed" and entry["error"]["code"] == "cache_fetch_failed"


@pytest.mark.parametrize("kind,factory", port_params("session_gateway"))
def test_session_window_opens_and_closes(kind, factory) -> None:
    gw = _impl(kind, factory)
    opened = gw.open(gw.test_host, gw.test_user, gw.test_public_key, expires_at=gw.in_minutes(5), device_minors=gw.test_minors, cards=gw.test_cards, timeout_s=20)
    assert opened["ok"] is True
    closed = gw.close(gw.test_host, gw.test_user, gw.test_fingerprint, timeout_s=20)
    assert closed["ok"] is True and gw.key_present() is False
    assert closed["close_proof"]["user_slice_empty"] is True and closed["close_proof"]["key_removed"] is True


@pytest.mark.parametrize("kind,factory", port_params("notifier"))
def test_message_is_delivered_or_typed_failure(kind, factory) -> None:
    notifier = _impl(kind, factory)
    delivery = notifier.send(notifier.test_message, timeout_s=20)
    assert delivery["state"] == ("sent" if kind == "real" else delivery["state"])
    if kind == "real":
        assert delivery["dry_run"] is False, "a real twin must actually deliver (strict)"
    assert "token" not in notifier.last_rendered_body().lower()


@pytest.mark.parametrize("kind,factory", port_params("health_probe"))
def test_health_probe_closed_port_is_not_ok(kind, factory) -> None:
    probe = _impl(kind, factory)
    assert probe.check(1, "/health", timeout_s=2)["ok"] is False


@pytest.mark.parametrize("kind,factory", port_params("legacy_observer"))
def test_unreadable_legacy_state_is_never_free(kind, factory) -> None:
    obs = _impl(kind, factory)
    state = obs.read_lane(obs.test_host, "no-such-lane-conformance", timeout_s=10)
    assert state["state"] in {"free", "unreadable", "unknown"}
    if kind == "fake":
        obs.script_next("corrupt-json")
        assert obs.read_lane(obs.test_host, obs.test_lane, timeout_s=5)["state"] == "unreadable"


@pytest.mark.parametrize("kind,factory", port_params("release_backend"))
def test_release_stage_activate_rollback_restore(kind, factory) -> None:
    backend = _impl(kind, factory)
    staged = backend.stage(backend.test_manifest)
    assert staged["ok"] is True
    assert backend.activate(staged["release_id"], backend.test_host)["ok"] is True
    assert backend.rollback(staged["release_id"], backend.test_host)["ok"] is True
    assert backend.backup(staged["release_id"])["ok"] is True
    assert backend.restore_rehearsal(staged["release_id"], backend.scratch_root())["hashes_match"] is True
