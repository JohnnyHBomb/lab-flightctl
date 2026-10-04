"""A3 named acceptance tests: the OccupancyProbe real twin (flightctl.gpu.NvidiaOccupancyProbe)."""

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.contracts_v2.validation import assert_valid, occupancy_from_capture, occupancy_semantics

CAPTURES = json.loads((Path(__file__).resolve().parent.parent / "contracts_v2" / "captures" / "gpu-occupancy.json").read_text())
U = "GPU-00000000-0000-0000-0000-000000000011"
AT = "2026-10-02T00:00:00Z"
BROWSER = [{"argv0": "/usr/lib/browser/browser", "uid": 1000}]
FMT = "--format=csv,noheader,nounits"


class _Clock:
    def utc(self):
        return datetime(2026, 10, 2, tzinfo=timezone.utc)


class _Scripted:
    """Answers each argv from a dict {tuple(argv): (returncode, text[, delay_s])}; anything unscripted exits 1.
    A failing answer's text is its stderr; returncode None times out. Delays are honest: never beyond timeout_s."""

    def __init__(self, answers):
        self.answers, self.calls, self.timeouts = answers, [], []

    def run(self, argv, *, timeout_s, stdin=None, host_id=None):
        self.calls.append(tuple(argv))
        self.timeouts.append(timeout_s)
        rc, out, delay = (*self.answers.get(tuple(argv), (1, "")), 0)[:3]
        time.sleep(min(delay, timeout_s))
        return {"argv": list(argv), "host_id": host_id, "returncode": rc, "stdout": out, "stderr": out if rc else "",
                "timed_out": rc is None, "duration_s": 0.0, "error": None}


def _answers(gpus, procs, returncode, uuids, types, identities):
    """Scripts (a)-(d): types {(uuid, pid): type}, identities {pid: {"uid", "argv0"}}."""
    from flightctl.gpu import APPS_QUERY, GPU_QUERY
    answers = {("nvidia-smi", GPU_QUERY, FMT): (returncode, gpus), ("nvidia-smi", APPS_QUERY, FMT): (returncode, procs)}
    for uuid in uuids:
        lines = "".join(f"    Process ID : {pid}\n        Type : {t}\n" for (u, pid), t in types.items() if u == uuid)
        answers[("nvidia-smi", "-q", "-d", "PIDS", "-i", uuid)] = (0, f"GPU 00000000:8D:00.0\n{lines}")
    for pid, ident in identities.items():
        answers[("stat", "-c", "%u", f"/proc/{pid}")] = (0, f"{ident['uid']}\n")
        answers[("cat", f"/proc/{pid}/cmdline")] = (0, f"{ident['argv0']}\0--flag\0")
    return answers


def _probe(answers):
    from flightctl.gpu import NvidiaOccupancyProbe
    return NvidiaOccupancyProbe(_Scripted(answers), clock=_Clock(), local_host_id="host-1")


def _without_reason(obs):
    return {k: v for k, v in obs.items() if k != "reason"}


def test_production_parser_agrees_with_oracle_on_all_captures() -> None:
    allow, cap = CAPTURES["noise_allowlist"], CAPTURES["noise_cap_mib"]
    for case in CAPTURES["cases"]:
        uuids = CAPTURES["lane_cards"][case["lane"]]
        idents = {int(pid): i for pid, i in case.get("identities", {}).items()}
        types = {(k.split("|")[0], int(k.split("|")[1])): t for k, t in case.get("context_types", {}).items()}
        probe = _probe(_answers(case["gpus"], case["procs"], case["returncode"], uuids, types, idents))
        obs = probe.occupancy("host-1", "lane-1", uuids, noise_allowlist=allow, noise_cap_mib=cap, lane_noise_mib=1024, timeout_s=10)
        assert_valid(obs, "gpu-probe", "occupancy_observation")
        assert occupancy_semantics(obs) == [], case["name"]
        oracle = occupancy_from_capture(case["gpus"], case["procs"], returncode=case["returncode"], lane_id="lane-1", host_id="host-1",
                                        observed_at=AT, lane_uuids=list(uuids), noise_allowlist=allow, noise_cap_mib=cap,
                                        lane_noise_mib=1024, identities=idents, context_types=types)
        assert _without_reason(obs) == _without_reason(oracle), case["name"]
        assert (obs["reason"] is None) == (oracle["reason"] is None), case["name"]
        assert obs["status"] == case["expect"]["status"], case["name"]


def _one(used, procs, allow=BROWSER, cap=64, status="ok", override=None):
    """procs: [(pid, mib, uid, argv0, type)]; uid None leaves stat and cat unscripted (exit 1); override replaces answers."""
    rows = "".join(f"{U}, {pid}, {argv0 or 'x'} --type=gpu-process, {mib}\n" for pid, mib, _, argv0, _ in procs)
    gpus = f"{U}, {used}, 24576, 0, 40, 30.00, 200.00, Not Active, Not Active, [N/A]\n"
    types = {(U, pid): t for pid, _, _, _, t in procs}
    idents = {pid: {"uid": uid, "argv0": argv0} for pid, _, uid, argv0, _ in procs if uid is not None}
    obs = _probe(_answers(gpus, rows, 0, [U], types, idents) | (override or {})).occupancy(
        "host-1", "lane-1", [U], noise_allowlist=allow, noise_cap_mib=cap, lane_noise_mib=1024, timeout_s=10)
    assert_valid(obs, "gpu-probe", "occupancy_observation")
    assert obs["status"] == status and occupancy_semantics(obs) == []
    return obs


def test_small_unlisted_cuda_process_is_a_tenant() -> None:
    tiny = _one(10, [(4001, 5, 1000, "python3", "C")])
    assert tiny["empty"] is False and [p["pid"] for p in tiny["tenants"]] == [4001]
    assert tiny["tenants"][0]["uid"] == 1000 and tiny["tenants"][0]["argv0"] == "python3"
    stray = _one(312, [(4000, 300, 1000, "python3", "C")])
    assert stray["empty"] is False and [p["pid"] for p in stray["tenants"]] == [4000]


def test_noise_needs_allowlisted_identity_and_cap() -> None:
    b = "/usr/lib/browser/browser"
    desk = _one(10, [(555655, 5, 1000, b, "C+G")])
    assert desk["empty"] is True and [p["pid"] for p in desk["noise"]] == [555655] and desk["tenants"] == []
    allow = BROWSER + [{"argv0": b, "uid": 61200}]
    for label, uid, ctype in [("C context", 1000, "C"), ("other uid", 1001, "C+G"), ("root", 0, "C+G"),
                              ("DynamicUser", 61200, "C+G"), ("unreadable /proc", None, "C+G")]:
        obs = _one(10, [(555655, 5, uid, b, ctype)], allow=allow)
        assert obs["empty"] is False and [p["pid"] for p in obs["tenants"]] == [555655], label
    over = _one(200, [(7000 + i, 40, 1000, b, "G") for i in range(3)], cap=64)
    assert over["tenants"] == [] and len(over["noise"]) == 3 and over["empty"] is False


def test_one_deadline_for_the_whole_call() -> None:
    from flightctl.gpu import APPS_QUERY, GPU_QUERY, NvidiaOccupancyProbe
    gpus = f"{U}, 10, 24576, 0, 40, 30.00, 200.00, Not Active, Not Active, [N/A]\n"
    runner = _Scripted({("nvidia-smi", GPU_QUERY, FMT): (0, gpus, 0.4), ("nvidia-smi", APPS_QUERY, FMT): (0, "", 0.4),
                        ("nvidia-smi", "-q", "-d", "PIDS", "-i", U): (None, "", 30)})
    start = time.monotonic()
    obs = NvidiaOccupancyProbe(runner, clock=_Clock()).occupancy(
        "host-1", "lane-1", [U], noise_allowlist=[], noise_cap_mib=64, lane_noise_mib=1024, timeout_s=1)
    assert time.monotonic() - start < 3 and obs["status"] == "unknown" and "timeout" in obs["reason"], obs
    assert runner.timeouts[1] <= 0.6 and all(t <= 0.2 for t in runner.timeouts[2:]), runner.timeouts


def test_identity_read_timeout_is_unknown() -> None:
    obs = _one(10, [(4001, 5, 1000, "python3", "C")], status="unknown", override={("stat", "-c", "%u", "/proc/4001"): (None, "")})
    assert "timeout" in obs["reason"] and obs["empty"] is False


def test_argv0_is_never_split_on_spaces() -> None:
    one = "/usr/lib/browser/browser --type=gpu-process --enable-features=x"
    obs = _one(10, [(555655, 5, 1000, one, "C+G")], allow=[{"argv0": one, "uid": 1000}],
               override={("cat", "/proc/555655/cmdline"): (0, one + "\0")})  # rewritten into ONE string (GOAL)
    assert [p["argv0"] for p in obs["noise"]] == [one] and obs["empty"] is True


def test_process_memory_above_memory_used_is_unknown() -> None:
    assert _one(10, [(4001, 50, 1000, "python3", "C")], status="unknown")["empty"] is False


def test_allowlisted_identity_with_unknown_memory_is_a_tenant() -> None:
    obs = _one(10, [(555655, "[N/A]", 1000, "/usr/lib/browser/browser", "C+G")])
    assert obs["noise"] == [] and [p["pid"] for p in obs["tenants"]] == [555655] and obs["empty"] is False


def test_pids_query_failure_is_unknown_with_operator_message() -> None:
    obs = _one(10, [], status="unknown", override={("nvidia-smi", "-q", "-d", "PIDS", "-i", U): (6, "No devices were found\n")})
    assert "PIDS query (c)" in obs["reason"] and "exit code 6" in obs["reason"] and "No devices were found" in obs["reason"]


@pytest.mark.realtime
def test_probe_real_process_timeout(tmp_path) -> None:
    from flightctl.clock import RealClock
    from flightctl.commands import LocalCommandRunner
    from flightctl.gpu import NvidiaOccupancyProbe
    child = subprocess.Popen(["sleep", "30"])
    stub, pidfile = tmp_path / "nvidia-smi", tmp_path / "stub.pid"
    try:
        stub.write_text(f"""#!/bin/sh
case "$1" in
  --query-gpu=*) [ -e {tmp_path}/hang ] && echo $$ > {pidfile} && exec sleep 30
    echo "{U}, 10, 24576, 0, 40, 30.00, 200.00, Not Active, Not Active, [N/A]" ;;
  --query-compute-apps=*) echo "{U}, {child.pid}, sleep 30, 5" ;;
  -q) printf 'GPU 00000000:8D:00.0\\n    Process ID : {child.pid}\\n        Type : C\\n' ;;
esac
""")
        stub.chmod(0o755)
        probe = NvidiaOccupancyProbe(LocalCommandRunner(), clock=RealClock(), nvidia_smi=str(stub), local_host_id="host-1")
        kwargs = dict(noise_allowlist=[], noise_cap_mib=64, lane_noise_mib=1024)
        obs = probe.occupancy("host-1", "lane-1", [U], timeout_s=10, **kwargs)
        assert_valid(obs, "gpu-probe", "occupancy_observation")
        assert obs["status"] == "ok" and obs["empty"] is False and len(obs["tenants"]) == 1, obs
        tenant = obs["tenants"][0]
        assert (tenant["pid"], tenant["uid"], tenant["argv0"], tenant["context_type"]) == (child.pid, os.getuid(), "sleep", "C")
        (tmp_path / "hang").touch()
        start = time.monotonic()
        obs = probe.occupancy("host-1", "lane-1", [U], timeout_s=1, **kwargs)
        assert time.monotonic() - start < 4
        assert obs["status"] == "unknown" and obs["empty"] is False and "timeout" in obs["reason"], obs
        with pytest.raises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)
    finally:
        child.kill()
        child.wait()
