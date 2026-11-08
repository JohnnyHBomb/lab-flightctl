"""A5a4 named acceptance test: the executor_transport twins are registered for the frozen conformance cases."""

import pytest

from flightctl.transport import LocalSubprocessTransport

CARD = "GPU-00000000-0000-0000-0000-000000000002"
IDLE_SMI = f"""#!/bin/sh
case "$1" in --query-gpu=*) echo '{CARD}, 0, 16384, 0, 35, 20.50, 200.00, Not Active, Not Active, 0' ;; esac
"""


@pytest.mark.realtime
def test_executor_transport_twins_registered_for_conformance(tmp_path, monkeypatch) -> None:
    import tests.conformance.impl_executor_transport  # noqa: F401
    from tests.conformance import registry

    found = dict(registry.implementations("executor_transport"))
    assert sorted(found) == ["fake", "real"]

    fake = found["fake"]("host-1")
    assert fake.host_id == "host-1" and fake.controller_boot_id() != fake.host_boot_id()
    assert fake.controller_boot_id().startswith("sim-") and fake.host_boot_id().startswith("sim-")

    smi = tmp_path / "nvidia-smi"
    smi.write_text(IDLE_SMI, encoding="utf-8")
    smi.chmod(0o700)
    monkeypatch.setenv("FLIGHTCTL_CONFORMANCE_NVIDIA_SMI", str(smi))
    monkeypatch.setenv("FLIGHTCTL_CONFORMANCE_GPU_UUIDS", CARD)
    real = found["real"]("host-local")
    assert isinstance(real.transport, LocalSubprocessTransport)
    result = real.transport.call(real.host_id, real.reserve_request(generation=1, expiry_in_s=120), timeout_s=20)
    assert result["status"] == "ok", result
    assert (result["reply"]["ok"], result["reply"]["observed_state"]) == (True, "reserved"), result["reply"]["error"]
    assert real.host_boot_id() == result["reply"]["host_boot"]["boot_id"] and not real.host_boot_id().startswith("sim-")
    assert real.controller_boot_id().startswith("sim-")
    real.cleanup()
    assert not real.directory.exists()
