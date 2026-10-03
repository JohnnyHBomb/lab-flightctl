"""A2 named acceptance tests: the CommandRunner seam (local, ssh, record, replay)."""

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from flightctl.commands import SAFE_PATH, LocalCommandRunner, RecordingCommandRunner, SshCommandRunner
from tests.fakes.replay import CaptureError, ReplayCommandRunner

PRINTF = ["printf", "%s", "a b; $(echo x) `y`"]
LISTENER = ("import socket,time\ns=socket.socket()\ns.bind(('127.0.0.1',0))\ns.listen(1)\n"
            "print(s.getsockname()[1],flush=True)\nc=s.accept()\ntime.sleep(60)")
CAPTURE_KEYS = {"argv", "host_id", "returncode", "stdout", "stderr", "timed_out", "duration_s", "error", "stdin_sha256"}


def _cmdline(pid):
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:  # exited between pgrep and the read
        return b""


def _survivors(tag):
    """Live `sleep <tag>` processes: matched by the unique tag with pgrep, then confirmed by their exact cmdline."""
    found = subprocess.run(["pgrep", "-f", f"^sleep {tag}$"], capture_output=True, text=True, timeout=5).stdout.split()
    return [pid for pid in found if _cmdline(pid) == f"sleep\0{tag}\0".encode()]


@pytest.mark.realtime
def test_ssh_unreachable_is_typed_and_bounded():
    listener = subprocess.Popen([sys.executable, "-c", LISTENER], stdout=subprocess.PIPE, text=True)
    try:
        port = int(listener.stdout.readline())
        runner = SshCommandRunner({"host-1": "127.0.0.1"}, connect_timeout_s=1, port=port, ssh_config=os.devnull)
        started = time.monotonic()
        result = runner.run(["true"], timeout_s=20, host_id="host-1")
        assert time.monotonic() - started < 10
        assert result["returncode"] == 255 and result["timed_out"] is False
        assert result["error"]["code"] == "transport_failed" and result["error"]["layer"] == "transport"
    finally:
        listener.kill()
        listener.wait()


@pytest.mark.realtime
def test_local_runner_is_bounded_and_isolated(monkeypatch):
    runner = LocalCommandRunner()
    started = time.monotonic()
    tag = f"30.{time.time_ns()}"  # 30 s, made unique so that a surviving sleep can be found
    result = runner.run(["sh", "-c", f"sleep {tag} & sleep {tag}"], timeout_s=1)
    assert time.monotonic() - started < 4
    assert result["returncode"] is None and result["timed_out"] is True and result["error"]["code"] == "timeout"
    deadline = time.monotonic() + 2  # the whole group was killed: neither the child nor the background sleep survives
    while _survivors(tag) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _survivors(tag) == []
    monkeypatch.setenv("A2_PROBE", "1")
    assert sorted(runner.run(["env"], timeout_s=5)["stdout"].splitlines()) == ["LC_ALL=C", f"PATH={SAFE_PATH}"]
    assert runner.run(["cat"], timeout_s=5, stdin=b"abc")["stdout"] == "abc"


def test_ssh_argv_is_quoted_once_with_batchmode(tmp_path):
    saved, script = tmp_path / "args.json", tmp_path / "fake-ssh"
    script.write_text(f"#!{sys.executable}\nimport json, subprocess, sys\nopen({str(saved)!r}, 'w').write(json.dumps(sys.argv))\n"
                      "sys.exit(subprocess.call(['sh', '-c', ' '.join(sys.argv[sys.argv.index('--') + 1:])]))\n")
    script.chmod(0o755)
    runner = SshCommandRunner({"host-1": "runner@host-1.example"}, connect_timeout_s=7, port=2222,
                              ssh_config=os.devnull, ssh_program=str(script))
    argv = ["printf", "%s|", PRINTF[2], "it's", "", "*"]
    result = runner.run(argv, timeout_s=10, host_id="host-1")
    assert result["returncode"] == 0 and result["stdout"] == f"{PRINTF[2]}|it's||*|" and result["argv"] == argv
    assert json.loads(saved.read_text()) == [str(script), "-F", os.devnull, "-o", "BatchMode=yes", "-o", "ConnectTimeout=7",
                                             "-p", "2222", "runner@host-1.example", "--", shlex.join(argv)]
    saved.unlink()
    assert runner.run(["printf", "local"], timeout_s=5)["stdout"] == "local"
    refused = runner.run(["true"], timeout_s=5, host_id="host-2")
    assert refused["error"]["code"] == "invalid" and refused["returncode"] is None and not saved.exists()
    with pytest.raises(ValueError):
        SshCommandRunner({"host-1": "-oProxyCommand=x"})


def test_record_then_replay_roundtrip(tmp_path):
    path = tmp_path / "captures.jsonl"
    recorder = RecordingCommandRunner(LocalCommandRunner(), str(path))
    calls = [(PRINTF, 5, None), (["sh", "-c", "echo out; echo err >&2; exit 3"], 5, None), (["cat"], 5, b"abc"),
             (["sleep", "5"], 0.3, None), (["sleep", "1"], 5, None)]
    recorded = [recorder.run(argv, timeout_s=t, stdin=s) for argv, t, s in calls]
    assert recorded[1]["returncode"] == 3 and recorded[1]["stderr"] == "err\n" and recorded[3]["timed_out"] is True
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5 and all(set(json.loads(line)) == CAPTURE_KEYS for line in lines)
    replay = ReplayCommandRunner(str(path))
    for (argv, t, s), want in list(zip(calls, recorded))[:3]:
        assert replay.run(argv, timeout_s=t, stdin=s) == want
    timed = replay.run(["sleep", "5"], timeout_s=0.3)
    assert timed["timed_out"] is True and timed["returncode"] is None and timed["error"]["code"] == "timeout"
    late = replay.run(["sleep", "1"], timeout_s=0.5)
    assert late["timed_out"] is True and late["returncode"] is None and late["duration_s"] == 0.5
    for argv, host_id, stdin in ((["nvidia-smi"], None, None), (PRINTF, "host-1", None), (["cat"], None, b"xyz")):
        miss = replay.run(argv, timeout_s=5, stdin=stdin, host_id=host_id)
        assert miss["returncode"] is None and "no capture" in miss["stderr"] and miss["error"]["code"] == "unknown"
    bad = tmp_path / "bad.jsonl"
    duplicate = lines[0][:-1] + ', "argv": ["true"]}'  # valid if the last value silently won
    for second in (b'{"argv": []}', duplicate.encode(), b'{"argv": ["\xff"]}'):
        bad.write_bytes(lines[0].encode() + b"\n" + second + b"\n")
        with pytest.raises(CaptureError, match="line 2"):
            ReplayCommandRunner(str(bad))
