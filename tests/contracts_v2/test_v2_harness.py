"""B7 (Sol 6): the conformance evidence must attribute each case to its own port, and a gauge (strict) run must not
pass on skips. These tests pin the harness rules; they do not need any implementation."""

from pathlib import Path

import pytest

from tests.conformance import conftest as harness
from tests.conformance import registry

CONF = Path(__file__).parents[1] / "conformance"


def test_conformance_modules_declare_applicability_instead_of_skipping() -> None:
    for path in sorted(CONF.glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        assert "pytest.skip(" not in text, f"{path.name}: use @pytest.mark.fake_only / not_dryrun, not pytest.skip()"
        assert "\nPORT = " not in text, f"{path.name}: module-level PORT misattributes evidence; use port_params(port)"


def test_every_param_carries_its_own_port_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_REGISTRY", {})
    registry.register("notifier", "fake", lambda target: object())
    registry.register("model_cache", "fake", lambda target: object())
    for port in ("notifier", "model_cache"):
        (param,) = harness.port_params(port)
        marks = {m.name: m.args for m in param.marks}
        assert marks["port"] == (port,) and param.id == f"{port}-fake"


def test_strict_run_without_real_twin_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_REGISTRY", {})
    registry.register("inhibitor", "fake", lambda target: object())
    monkeypatch.setenv("FLIGHTCTL_CONFORMANCE_STRICT", "1")
    monkeypatch.setenv("FLIGHTCTL_CONFORMANCE_PORTS", "inhibitor")
    fake, missing = harness.port_params("inhibitor")
    assert fake.id == "inhibitor-fake"
    names = [m.name for m in missing.marks]
    assert "strict_missing" in names and "xfail" not in names and missing.id == "inhibitor-missing-real"  # hard fail, never xfail


def test_strict_run_for_unregistered_port_is_not_a_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_REGISTRY", {})
    monkeypatch.setenv("FLIGHTCTL_CONFORMANCE_STRICT", "1")
    monkeypatch.setenv("FLIGHTCTL_CONFORMANCE_PORTS", "waker")
    (param,) = harness.port_params("waker")
    assert not [m for m in param.marks if m.name == "skip"]
