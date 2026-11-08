"""ExecutorTransport twins for conformance (A5a4): the rigs the frozen cases of test_executor_transport.py call.

fake (any target): lane lane-gpu1 (site-a) of host-1 served by an in-process ExecutorV2 on its own SimClock (boot id
sim-host-1) with a MemoryStateStore and an occupancy probe that reports the lane's card idle, behind the entry point's
wire rules. The controller is a SimClock with another sim- boot id; sleep() advances both clocks.
real, target host-local: flightctl/executor_stdio.py of this checkout as a child process through LocalSubprocessTransport,
as host-1, with --state, a verified site copy (the contract's confirmed inventory example, lane-gpu1 holding exactly the
cards of FLIGHTCTL_CONFORMANCE_GPU_UUIDS, comma-separated, default the example's card) and --nvidia-smi a recording wrapper
(it logs its arguments, then execs FLIGHTCTL_CONFORMANCE_NVIDIA_SMI, default the nvidia-smi on PATH), all in a temporary
directory that cleanup() removes, as does process exit. The controller is a SimClock (sim- boot id) started at the real
UTC; sleep() is real time, and the controller's clock moves with it.
real, any other target: the target's installed entry point through SshForcedCommandTransport (ssh config
FLIGHTCTL_CONFORMANCE_SSH_CONFIG) as host FLIGHTCTL_CONFORMANCE_HOST_ID (default the target) and lane
FLIGHTCTL_CONFORMANCE_LANE (default conformance); the controller is RealClock. The enforcer (FLIGHTCTL_CONFORMANCE_ENFORCER,
shell-split) runs and the wrapper's log (FLIGHTCTL_CONFORMANCE_PROBE_LOG, which must exist) is read on the target through
SshCommandRunner. Building a rig opens no connection.
A real twin's host_boot_id() is the kernel boot id read on the host: here for host-local, else on the target over ssh.
stop_calls() counts the occupancy probe runs (for a real twin: the wrapper's --query-gpu= runs) made during run_enforcer().
cleanup() first stops (owner-release) each lease the rig built a reserve for: an installed executor keeps its fences.
"""

import atexit
import hashlib
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from flightctl.clock import BOOT_ID_PATH, RealClock
from flightctl.commands import LocalCommandRunner, SshCommandRunner
from flightctl.executor import ExecutorV2, MemoryStateStore
from flightctl.transport import LocalSubprocessTransport, SshForcedCommandTransport, _failure, _payload
from tests.contracts_v2.validation import assert_valid, examples, occupancy_from_capture
from tests.sim.rig import SimClock

from . import registry

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = examples("executor")["valid"][0]  # the contract's reserve example: controller, identity and execution policy
CONTROLLER = EXAMPLE["controller_id"]
CARD = "GPU-00000000-0000-0000-0000-000000000002"  # the inventory example's card on host-1
MAX_END_S = 14400  # the reserve example's max-end
UTC = "%Y-%m-%dT%H:%M:%SZ"
IDLE_ROW = "{}, 0, 16384, 0, 35, 20.50, 200.00, Not Active, Not Active, 0\n"  # one idle card as nvidia-smi --query-gpu prints it
WRAPPER = """#!/bin/sh
printf '%s\\n' "$*" >> {log}
exec {smi} "$@"
"""


class _Rig:
    """The members the frozen cases call; a twin adds host_boot_id(), sleep(), _enforce() and _probe_runs()."""

    def __init__(self, transport, *, host_id, lane_id, controller):
        self.transport, self.host_id, self.controller = transport, host_id, controller
        self.lane = {"site_id": "site-a", "host_id": host_id, "lane_id": lane_id}
        self._reserved, self._stops, self._sent = [], 0, 0

    def controller_boot_id(self):
        return self.controller.boot_id()

    def _identity(self, generation):
        unit = f"flightctl-{self.lane['lane_id']}-g{generation}.service"
        return {**EXAMPLE["identity"], "lane": dict(self.lane), "generation": generation, "unit": unit}

    def _request(self, kind, identity, deadlines=None, **fields):
        """A v2 request stamped now on the controller's clock (as the rig's controller unless fields name another one); it must
        be valid against executor.schema.json#/$defs/request."""
        stamp = self.controller.utc().strftime(UTC)
        self._sent += 1
        request = {"schema_version": 2, "kind": kind, "controller_request_id": f"creq-{self._sent}", "controller_id": CONTROLLER,
                   "sent_at": stamp, "identity": identity, **fields}
        if deadlines is not None:
            request["deadlines"] = [{"kind": name, "in_s": in_s, "sender_utc": stamp} for name, in_s in deadlines.items() if in_s is not None]
        assert_valid(request, "executor", "request")
        return request

    def reserve_request(self, *, generation, expiry_in_s, stale_in_s=None, protected=False):
        policy = {**EXAMPLE["execution_policy"], "protected": protected, "preemptible": not protected}
        deadlines = {"expiry": expiry_in_s, "max-end": MAX_END_S, "heartbeat-stale": stale_in_s}
        request = self._request("reserve", self._identity(generation), deadlines, execution_policy=policy, awake={"hold_inhibitor": False})
        self._reserved.append(request)
        return request

    def beat_request(self, *, generation, expiry_in_s, stale_in_s=None):
        return self._request("beat", self._identity(generation), {"expiry": expiry_in_s, "heartbeat-stale": stale_in_s})

    def stop_request_from(self, req, *, mode):
        authority = {"mode": mode, "approval_id": None, "reason": "conformance stop"}
        return self._request("stop", req["identity"], execution_policy=req["execution_policy"], stop_authority=authority)

    def _inspect(self, generation, **fields):
        return self._request("inspect", self._identity(generation), scope="lease", **fields)

    def inspect_lease(self, generation):
        result = self.transport.call(self.host_id, self._inspect(generation), timeout_s=60)
        assert result["status"] == "ok", result
        return result["reply"]

    def call_with_wrong_identity(self):
        return self.transport.call(self.host_id, self._inspect(1, controller_id="controller-b"), timeout_s=20)

    def call_with_garbage(self):
        return self.transport.call(self.host_id, {**self._inspect(1), "schema_version": "2"}, timeout_s=20)

    def run_enforcer(self):
        before = self._probe_runs()
        acted = self._enforce()
        self._stops += self._probe_runs() - before
        return acted

    def stop_calls(self):
        return self._stops

    def cleanup(self):
        for request in self._reserved:
            self.transport.call(self.host_id, self.stop_request_from(request, mode="owner-release"), timeout_s=60)
        self._reserved = []


class _IdleProbe:
    """OccupancyProbe fake: the contract oracle's observation of an idle capture of the lane's cards; counts its calls."""

    def __init__(self, clock):
        self._clock, self.calls = clock, 0

    def occupancy(self, host_id, lane_id, uuids, *, noise_allowlist, noise_cap_mib, lane_noise_mib, timeout_s):
        self.calls += 1
        return occupancy_from_capture("".join(IDLE_ROW.format(uuid) for uuid in uuids), "", returncode=0, lane_id=lane_id,
                                      host_id=host_id, observed_at=self._clock.utc().strftime(UTC), lane_uuids=list(uuids),
                                      noise_allowlist=noise_allowlist, noise_cap_mib=noise_cap_mib, lane_noise_mib=lane_noise_mib)


class _WireRules:
    """The entry point's wire rules in front of an in-process ExecutorV2; the request and the reply cross as JSON."""

    def __init__(self, executor):
        self._executor = executor

    def call(self, host_id, request, *, timeout_s):
        request = json.loads(_payload(host_id, request, timeout_s))
        if type(request.get("schema_version")) is not int or request["schema_version"] != 2:
            return _failure("unparsable", "unparsable: schema_version must be the integer 2")
        if request.get("controller_id") != CONTROLLER:
            return _failure("denied", "Permission denied: the request's controller_id is not this entry point's controller")
        return {"status": "ok", "reply": json.loads(json.dumps(self._executor.handle(request), allow_nan=False)), "error": None}


class _FakeRig(_Rig):
    def __init__(self):
        self.host = SimClock(boot_id="sim-host-1")
        self.probe = _IdleProbe(self.host)
        self.executor = ExecutorV2(self.host, host_id="host-1", store=MemoryStateStore(), lane_cards={"lane-gpu1": [CARD]},
                                   occupancy=self.probe)
        super().__init__(_WireRules(self.executor), host_id="host-1", lane_id="lane-gpu1", controller=SimClock(boot_id="sim-controller"))

    def host_boot_id(self):
        return self.host.boot_id()

    def sleep(self, seconds):
        self.controller.advance(seconds)
        self.host.advance(seconds)

    def _enforce(self):
        return self.executor.enforce_deadlines()

    def _probe_runs(self):
        return self.probe.calls


class _RealRig(_Rig):
    def __init__(self, transport, *, runner, on_host, enforcer, probe_log, directory=None, **where):
        super().__init__(transport, **where)
        self._runner, self._on_host, self._enforcer, self._probe_log, self.directory = runner, on_host, enforcer, probe_log, directory

    def _run(self, argv):
        """stdout of argv run on the host (here for host-local, else over ssh); it must exit 0."""
        result = self._runner.run(argv, timeout_s=60, host_id=self._on_host)
        assert result["returncode"] == 0, result
        return result["stdout"]

    def host_boot_id(self):
        return self._run(["cat", BOOT_ID_PATH]).strip()

    def sleep(self, seconds):
        time.sleep(seconds)
        if isinstance(self.controller, SimClock):  # host-local: the simulated controller clock keeps to real time
            self.controller.advance(seconds)

    def _enforce(self):
        acted = json.loads(self._run(self._enforcer))
        assert isinstance(acted, list), acted
        return acted

    def _probe_runs(self):
        return sum(line.startswith("--query-gpu=") for line in self._run(["cat", self._probe_log]).splitlines())

    def cleanup(self):
        super().cleanup()
        if self.directory is not None:
            shutil.rmtree(self.directory, ignore_errors=True)


def _real(target):
    if target != "host-local":
        env = os.environ
        host_id = env.get("FLIGHTCTL_CONFORMANCE_HOST_ID") or target
        transport = SshForcedCommandTransport({host_id: target}, ssh_config=env["FLIGHTCTL_CONFORMANCE_SSH_CONFIG"])
        return _RealRig(transport, runner=SshCommandRunner({host_id: target}), on_host=host_id,
                        enforcer=shlex.split(env["FLIGHTCTL_CONFORMANCE_ENFORCER"]), probe_log=env["FLIGHTCTL_CONFORMANCE_PROBE_LOG"],
                        host_id=host_id, lane_id=env.get("FLIGHTCTL_CONFORMANCE_LANE") or "conformance", controller=RealClock())
    directory = Path(tempfile.mkdtemp(prefix="flightctl-a5a4-"))
    atexit.register(shutil.rmtree, directory, ignore_errors=True)  # the garbage case never calls cleanup()
    site, log, wrapper = directory / "site", directory / "nvidia-smi.log", directory / "nvidia-smi"
    inventory = json.loads((ROOT / "config" / "inventory-v2.json.example").read_text(encoding="utf-8"))
    [host] = [entry for entry in inventory["hosts"] if entry["host_id"] == "host-1"]
    [lane] = [entry for entry in inventory["lanes"] if entry["lane_id"] == "lane-gpu1"]
    uuids = (os.environ.get("FLIGHTCTL_CONFORMANCE_GPU_UUIDS") or CARD).split(",")
    host["devices"] = [{**host["devices"][0], "device_id": f"gpu-b{at}", "uuid": uuid} for at, uuid in enumerate(uuids)]
    host["gpu_count"], lane["device_ids"] = len(uuids), [device["device_id"] for device in host["devices"]]
    assert_valid(inventory, "inventory")
    text = json.dumps(inventory)
    site.mkdir()
    (site / "inventory.json").write_text(text, encoding="utf-8")
    (site / "SHA256SUMS").write_text(f"{hashlib.sha256(text.encode('utf-8')).hexdigest()}  inventory.json\n", encoding="utf-8")
    log.touch()
    smi = os.environ.get("FLIGHTCTL_CONFORMANCE_NVIDIA_SMI") or shutil.which("nvidia-smi") or "nvidia-smi"
    wrapper.write_text(WRAPPER.format(log=shlex.quote(str(log)), smi=shlex.quote(smi)), encoding="utf-8")
    wrapper.chmod(0o700)
    argv = [sys.executable, str(ROOT / "flightctl" / "executor_stdio.py"), "--state", str(directory / "state.json"),
            "--host-id", "host-1", "--site-dir", str(site), "--nvidia-smi", str(wrapper)]
    controller = SimClock(boot_id="sim-controller", utc_start=datetime.now(timezone.utc))
    return _RealRig(LocalSubprocessTransport([*argv, "--controller", CONTROLLER], host_id="host-1"), runner=LocalCommandRunner(),
                    on_host=None, enforcer=[*argv, "--enforce"], probe_log=str(log), directory=directory,
                    host_id="host-1", lane_id="lane-gpu1", controller=controller)


registry.register("executor_transport", "fake", lambda target: _FakeRig())
registry.register("executor_transport", "real", _real)
