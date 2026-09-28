import json
from pathlib import Path

import pytest

from tests.fakes import FakeClock, FakeGPUProbe, FakeSSH, FakeSystemd
from tests.fakes.gpu import parse_nvidia_smi


FIXTURES = Path(__file__).parents[1] / "fakes" / "fixtures"


def test_fake_interfaces() -> None:
    fixtures = {}
    for path in sorted(FIXTURES.glob("*.json")):
        fixtures[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    probe = FakeGPUProbe({name: fixture for name, fixture in fixtures.items()})
    for name, fixture in fixtures.items():
        result = probe.inspect(name)
        expected = fixture["expected"]
        for key, value in expected.items():
            assert result[key] == value
    assert fixtures["unknown"]["discovery_raw"] == {"tailnet_status": "{\"Peer\":null}", "ssh_probe": "timeout"}
    assert [call["host"] for call in probe.calls] == sorted(fixtures)

    ssh = FakeSSH([{"outcome": "success", "response": {"ok": True}}, {"outcome": "denied"}, {"outcome": "lost"}, {"outcome": "timeout"}, {"outcome": "delayed", "delay_s": 2}, {"outcome": "unknown"}])
    assert ssh.request("host-1", {"kind": "inspect"}, 1)["status"] == "ok"
    assert ssh.request("host-1", {}, 1)["status"] == "denied"
    assert ssh.request("host-1", {}, 1)["status"] == "lost"
    assert ssh.request("host-1", {}, 1)["status"] == "timeout"
    assert ssh.request("host-1", {}, 1)["status"] == "delayed"
    assert ssh.request("host-1", {}, 1)["status"] == "unknown"
    assert len(ssh.calls) == 6

    clock = FakeClock()
    utc_before, mono_before, boot_before = clock.utc(), clock.monotonic(), clock.boot_id()
    clock.advance(utc_s=30)
    assert (clock.utc() - utc_before).total_seconds() == 30
    assert clock.monotonic() == mono_before
    clock.reboot(boot_id="boot-b", utc_s=1, monotonic_s=0)
    assert clock.boot_id() == "boot-b" and boot_before != clock.boot_id()

    systemd = FakeSystemd()
    assert systemd.start("unit-a", "invoke-a")["ok"]
    systemd.queue("invocation_mismatch")
    assert systemd.stop("unit-a", "invoke-b")["status"] == "invocation_mismatch"
    systemd.queue("failed_stop")
    assert systemd.stop("unit-a", "invoke-a")["status"] == "failed_stop"
    systemd.occupants[:] = ["pid-a"]
    systemd.gpu_occupants[:] = ["gpu-tenant-a"]
    inspection = systemd.inspect("unit-a", "invoke-a")
    assert inspection["occupants"] == ["pid-a"]
    assert inspection["cgroup_occupants"] == ["pid-a"]
    assert inspection["gpu_occupants"] == ["gpu-tenant-a"]
    systemd.queue("unknown")
    assert systemd.inspect("unit-a", "invoke-a")["status"] == "unknown"


def test_fake_no_real_side_effects() -> None:
    fake = FakeSSH()
    result = fake.request("host-1", {"kind": "inspect"}, 0.01)
    assert result["status"] == "timeout"
    assert fake.calls[0]["endpoint"] == "host-1"
    assert parse_nvidia_smi("")["count"] is None
