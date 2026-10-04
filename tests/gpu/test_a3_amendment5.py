"""A3 conformance to Amendment 5: one-string command lines in the noise allow-list, and values nvidia-smi does not print
(the oracle maps them to unknown). Each case agrees with the contract oracle occupancy_from_capture."""

import pytest

from tests.contracts_v2.validation import assert_valid, occupancy_from_capture

from .test_a3_occupancy import AT, FMT, U, _answers, _probe

GPUS = f"{U}, 7, 24576, 3, 40, 25.5, 280.0, Not Active, Not Active, 0"
# measured shape (Amendment 5): a GPU process whose command line is ONE string (one NUL) and whose path holds a space
ONE_STRING = "/usr/local/Some App/some-app --type=gpu-process --ozone-platform=wayland --enable-features=X"
ALLOW = [{"argv0": "/usr/local/Some App/some-app", "uid": 1000}]


def _occupancy(answers, allow, cap=64):
    return _probe(answers).occupancy("host-1", "lane-t", [U], noise_allowlist=allow, noise_cap_mib=cap, lane_noise_mib=1024, timeout_s=10)


def _oracle(gpus, procs, allow, identities, types, cap=64):
    return occupancy_from_capture(gpus, procs, returncode=0, lane_id="lane-t", host_id="host-1", observed_at=AT, lane_uuids=[U],
                                  noise_allowlist=allow, noise_cap_mib=cap, lane_noise_mib=1024, identities=identities,
                                  context_types=types)


def test_one_string_gpu_process_cmdline_matches_its_allowlist_entry() -> None:
    procs = f"{U}, 4242, {ONE_STRING}, 7"
    answers = _answers(GPUS, procs, 0, [U], {(U, 4242): "C+G"}, {})
    answers[("stat", "-c", "%u", "/proc/4242")] = (0, "1000\n")
    answers[("cat", "/proc/4242/cmdline")] = (0, ONE_STRING + "\0")  # one string, one NUL (rewritten argv)
    obs = _occupancy(answers, ALLOW)
    assert_valid(obs, "gpu-probe", "occupancy_observation")
    assert obs["status"] == "ok" and obs["empty"] is True and [p["attribution"] for p in obs["processes"]] == ["noise"]
    oracle = _oracle(GPUS, procs, ALLOW, {4242: {"uid": 1000, "argv0": ONE_STRING}}, {(U, 4242): "C+G"})
    assert oracle["empty"] is True and oracle["processes"][0]["attribution"] == "noise"
    # a prefix that does not end at a space, or a C context, is still a tenant
    shorter = [{"argv0": "/usr/local/Some App/some", "uid": 1000}]
    assert _occupancy(answers, shorter)["empty"] is False
    answers_c = dict(answers)
    answers_c[("nvidia-smi", "-q", "-d", "PIDS", "-i", U)] = (0, f"GPU 00000000:8D:00.0\n    Process ID : 4242\n        Type : C\n")
    assert _occupancy(answers_c, ALLOW)["empty"] is False


@pytest.mark.parametrize("label,cells", [
    ("fractional utilization", {3: "3.5"}), ("temperature above 130", {4: "131"}), ("temperature below 0", {4: "-1"}),
    ("memory.total 0", {2: "0"}), ("negative power draw", {5: "-1.5"}), ("negative ECC count", {9: "-2"}),
    ("NaN utilization", {3: "nan"}), ("infinite power limit", {6: "inf"}),
])
def test_out_of_range_gpu_value_is_unknown(label, cells) -> None:
    row = [c.strip() for c in GPUS.split(",")]
    for i, v in cells.items():
        row[i] = v
    gpus = ", ".join(row)
    obs = _occupancy(_answers(gpus, "", 0, [U], {}, {}), ALLOW)
    assert obs["status"] == "unknown" and obs["empty"] is False and obs["reason"], label
    assert_valid(obs, "gpu-probe", "occupancy_observation")
    assert _oracle(gpus, "", ALLOW, {}, {})["status"] == "unknown", label


@pytest.mark.parametrize("label,procs", [("pid 0", f"{U}, 0, python3, 5"), ("negative process memory", f"{U}, 4242, python3, -5")])
def test_pid_zero_or_negative_process_memory_is_unknown(label, procs) -> None:
    answers = _answers(GPUS, procs, 0, [U], {(U, 4242): "C"}, {4242: {"uid": 1000, "argv0": "python3"}})
    obs = _occupancy(answers, ALLOW)
    assert obs["status"] == "unknown" and obs["empty"] is False, label
    assert_valid(obs, "gpu-probe", "occupancy_observation")
    assert _oracle(GPUS, procs, ALLOW, {4242: {"uid": 1000, "argv0": "python3"}}, {(U, 4242): "C"})["status"] == "unknown"


def test_readable_uid_with_unreadable_cmdline_is_a_tenant() -> None:
    """argv0 is null (cat fails) while the uid reads: never noise, and the prefix rule never touches a null argv0."""
    procs = f"{U}, 4242, browser, 7"
    answers = _answers(GPUS, procs, 0, [U], {(U, 4242): "C+G"}, {})
    answers[("stat", "-c", "%u", "/proc/4242")] = (0, "1000\n")  # cat /proc/4242/cmdline is unscripted: it exits 1
    obs = _occupancy(answers, ALLOW)
    assert obs["status"] == "ok" and obs["empty"] is False
    assert obs["processes"][0]["argv0"] is None and obs["processes"][0]["attribution"] == "external"
