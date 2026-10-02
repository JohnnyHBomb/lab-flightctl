"""Clock and CommandRunner conformance."""

import re
import time

import pytest

from .conftest import port_params



def _impl(kind, factory):
    from . import registry
    return factory(registry.target())


@pytest.mark.parametrize("kind,factory", port_params("clock"))
def test_boot_id_is_the_host_boot_not_the_process(kind, factory) -> None:
    clock = _impl(kind, factory)
    boot = clock.boot_id()
    if kind == "real":
        assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", boot), boot
        assert not boot.startswith("process-"), "v1 RealClock used the pid (G27)"
    assert clock.boot_id() == boot, "stable across calls and across process restarts"


@pytest.mark.parametrize("kind,factory", port_params("clock"))
def test_monotonic_never_goes_backwards_and_utc_is_aware(kind, factory) -> None:
    clock = _impl(kind, factory)
    first = clock.monotonic()
    if kind == "real":
        time.sleep(0.01)
    assert clock.monotonic() >= first
    assert clock.utc().tzinfo is not None


@pytest.mark.parametrize("kind,factory", port_params("command_runner"))
def test_command_timeout_is_typed_and_bounded(kind, factory) -> None:
    runner = _impl(kind, factory)
    started = time.monotonic()
    result = runner.run(["sleep", "5"], timeout_s=0.5)
    assert result["timed_out"] is True and result["returncode"] is None
    assert time.monotonic() - started < 3


@pytest.mark.parametrize("kind,factory", port_params("command_runner"))
def test_argv_is_never_shell_interpreted(kind, factory) -> None:
    runner = _impl(kind, factory)
    result = runner.run(["printf", "%s", "a b; $(echo x) `y`"], timeout_s=5)
    assert result["returncode"] == 0 and result["stdout"] == "a b; $(echo x) `y`"


@pytest.mark.fake_only
@pytest.mark.parametrize("kind,factory", port_params("command_runner"))
def test_unknown_capture_in_replay_is_not_success(kind, factory) -> None:
    runner = _impl(kind, factory)
    result = runner.run(["nvidia-smi", "--never-captured"], timeout_s=5)
    assert result["returncode"] is None and "no capture" in result["stderr"]
