"""A4: the executor stdio entry point and its two transports, over real child processes."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from flightctl.clock import RealClock
from flightctl.executor_stdio import MAX_REQUEST_BYTES
from flightctl.transport import LocalSubprocessTransport, SshForcedCommandTransport

ENTRY = str(Path(__file__).resolve().parents[2] / "flightctl" / "executor_stdio.py")
TARGET = os.environ.get("FLIGHTCTL_CONFORMANCE_TARGET", "")


def _reserve(generation: int, token: str) -> dict:
    deadline = {"kind": "max-end", "owner_class": "batch", "boot_id": RealClock().boot_id(), "deadline_s": 3600,
                "utc_anchor": "2026-10-04T00:00:00Z", "monotonic_anchor_s": time.monotonic()}
    identity = {"lane": {"site_id": "site-a", "host_id": "host-1", "lane_id": "lane-gpu0"}, "generation": generation,
                "token": token, "instance": "instance-a", "unit": None, "invocation": None, "deadline": deadline}
    policy = {"class": "batch", "protected": False, "preemptible": True, "grace_s": 0,
              "max_end": "2026-10-04T01:00:00Z", "deadline_kind": "max-end"}
    return {"schema_version": 1, "kind": "reserve", "controller_request_id": f"request-{generation}",
            "execution_policy": policy, "identity": identity}


def _local(*command: str) -> LocalSubprocessTransport:
    return LocalSubprocessTransport(list(command), host_id="host-1")


def _entry(state: Path) -> list[str]:
    return [sys.executable, ENTRY, "--state", str(state), "--controller", "controller-a"]


@pytest.mark.realtime
def test_one_shot_invocations_keep_state(tmp_path):
    first = _local(*_entry(tmp_path / "state.json")).call("host-1", _reserve(1, "token-generation-one"), timeout_s=30)
    assert first["status"] == "ok" and first["reply"]["ok"] is True, first
    second = _local(*_entry(tmp_path / "state.json")).call("host-1", _reserve(2, "token-generation-two"), timeout_s=30)
    assert second["status"] == "ok" and second["reply"]["ok"] is False, second
    fresh = _local(*_entry(tmp_path / "fresh.json")).call("host-1", _reserve(2, "token-generation-two"), timeout_s=30)
    assert fresh["status"] == "ok" and fresh["reply"]["ok"] is True, fresh


def test_garbage_is_unparsable_not_ok(tmp_path):
    state = tmp_path / "state.json"
    garbage = [b"", b"not json", b"[1]", b'{"a": 1, "a": 2}', b'{"a": NaN}', b'{"a": "\xff"}',
               b"{" + b" " * MAX_REQUEST_BYTES + b"}", b"{}" + b" " * (MAX_REQUEST_BYTES - 1)]  # valid, one byte too long
    for payload in garbage:
        done = subprocess.run(_entry(state), input=payload, capture_output=True, timeout=30, cwd="/",
                              env={"PATH": os.environ["PATH"], "LC_ALL": "C"})  # DECIDED 2: any cwd, only PATH and LC_ALL
        assert (done.returncode, done.stdout) == (2, b""), (payload[:20], done)
        assert done.stderr.startswith(b"unparsable:") and not state.exists()
        assert not Path(f"{state}.lock").exists()
    for script in ["print('garbage')", "print('[1]')", "pass"]:
        result = _local(sys.executable, "-c", script).call("host-1", {"kind": "inspect"}, timeout_s=10)
        assert (result["status"], result["reply"], result["error"]["code"]) == ("unparsable", None, "reply_unparsable")


@pytest.mark.realtime
def test_transport_failures_are_typed_and_bounded():
    cases = {
        "timeout": ("import time; time.sleep(30)", "timeout"),
        "failed": ("raise SystemExit(1)", "transport_failed"),
        "unparsable": ("raise SystemExit(2)", "reply_unparsable"),
        "denied": ("import sys; sys.stderr.write('Permission denied (publickey).\\n'); sys.exit(255)", "denied"),
        "lost": ("import sys; sys.stderr.write('Connection closed by remote host\\n'); sys.exit(255)", "reply_lost"),
    }
    for status, (script, code) in cases.items():
        started = time.monotonic()
        result = _local(sys.executable, "-c", script).call("host-1", {"kind": "inspect"}, timeout_s=1)
        assert time.monotonic() - started < 3, status
        assert (result["status"], result["reply"], result["error"]["layer"], result["error"]["code"]) == (
            status, None, "transport", code), result


def test_entry_point_and_argument_edges(tmp_path):
    closed = subprocess.run(["bash", "-c", 'exec 0<&- "$@"', "-", *_entry(tmp_path / "state.json")],
                            capture_output=True, timeout=30)
    assert closed.returncode == 1 and closed.stderr.count(b"\n") == 1, closed
    gone = subprocess.Popen(_entry(tmp_path / "state.json"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    gone.stdout.close()
    _, err = gone.communicate(json.dumps(_reserve(1, "token-generation-one")).encode(), timeout=30)
    assert gone.returncode == 1 and err.count(b"\n") == 1, (gone.returncode, err)
    with pytest.raises(ValueError):
        _local(sys.executable, "-c", "pass", "a\x00b")
    with pytest.raises(ValueError):
        _local(sys.executable, "-c", "pass").call("host-1", {}, timeout_s=10**400)
    nested = _local(sys.executable, "-c", "print('[' * 200000)").call("host-1", {}, timeout_s=10)  # RecursionError
    assert (nested["status"], nested["error"]["code"]) == ("unparsable", "reply_unparsable"), nested
    marker = tmp_path / "ran"
    mismatch = _local(sys.executable, "-c", f"open({str(marker)!r}, 'w')").call("host-2", {}, timeout_s=5)
    assert (mismatch["status"], mismatch["error"]["code"]) == ("failed", "transport_failed") and not marker.exists()


@pytest.mark.onlab
@pytest.mark.skipif(TARGET in ("", "host-local"), reason="FLIGHTCTL_CONFORMANCE_TARGET does not name an ssh host")
def test_wrong_key_denied(tmp_path):
    key = tmp_path / "wrong-key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True, timeout=30)
    user, _, host = TARGET.rpartition("@")
    config = tmp_path / "ssh_config"
    config.write_text(f"Host host-1\n  HostName {host}\n" + (f"  User {user}\n" if user else "")
                      + f"  IdentityFile {key}\n  IdentitiesOnly yes\n  UserKnownHostsFile /dev/null\n"
                      + "  StrictHostKeyChecking no\n")
    port = int(os.environ.get("FLIGHTCTL_CONFORMANCE_SSH_PORT", "22"))
    transport = SshForcedCommandTransport({"host-1": "host-1"}, ssh_config=str(config), port=port)
    result = transport.call("host-1", _reserve(1, "token-generation-one"), timeout_s=30)
    assert (result["status"], result["reply"], result["error"]["code"]) == ("denied", None, "denied"), result
