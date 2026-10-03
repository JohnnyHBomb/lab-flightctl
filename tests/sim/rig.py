"""Simulated host clocks joining the real authority and executors in process.

Nothing here reads real time; every clock moves only when advanced.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from itertools import count

from flightctl.authority import Authority, request_fingerprint
from flightctl.executor import Executor, MemoryStateStore
from flightctl.store import SQLiteStore
from tests.fakes.systemd import FakeSystemd


START_UTC = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
PEER = "peer-a"
PRINCIPAL = {"site_id": "site-a", "tenant_id": "tenant-a", "issuer": "issuer-a", "subject": "subject-a"}
CONTROLLER = "controller-a"


class SimClock:
    """One host's clock: own boot id, own monotonic origin, own UTC skew."""

    def __init__(self, *, boot_id, utc_start=START_UTC, monotonic_start=0.0, skew_s=0.0):
        self._boot_id = boot_id
        self._utc = utc_start.astimezone(timezone.utc)
        self._monotonic = float(monotonic_start)
        self._skew_s = skew_s

    def utc(self):
        return self._utc + timedelta(seconds=self._skew_s)

    def monotonic(self):
        return self._monotonic

    def boot_id(self):
        return self._boot_id

    def advance(self, seconds):
        if seconds < 0:
            raise ValueError("cannot advance by negative seconds")
        self._utc += timedelta(seconds=seconds)
        self._monotonic += seconds

    def reboot(self, boot_id):
        if boot_id == self._boot_id:
            raise ValueError("reboot requires a new boot id")
        self._boot_id = boot_id
        self._monotonic = 0.0


class _GPUProbe:
    def inspect(self, host):
        return {"ok": True, "status": "ok", "complete": True, "gpu_tenants": []}


class SimHost:
    """One host: its clock, systemd, GPU probe, durable state store and real Executor."""

    def __init__(self, host_id, clock):
        self.host_id = host_id
        self.clock = clock
        self.systemd = FakeSystemd()
        self.gpu_probe = _GPUProbe()
        self.state_store = MemoryStateStore()
        self.executor = Executor(
            clock, self.systemd, self.gpu_probe,
            state_store=self.state_store, trusted_controller=CONTROLLER,
        )

    def reboot(self, boot_id):
        self.clock.reboot(boot_id)
        self.systemd = FakeSystemd()
        self.executor = Executor(
            self.clock, self.systemd, self.gpu_probe,
            state_store=self.state_store, trusted_controller=CONTROLLER,
        )


class InProcessTransport:
    """Delivers authority messages to the bound host's executor in-process."""

    def __init__(self):
        self._hosts = {}

    def bind(self, endpoint, host):
        self._hosts[endpoint] = host

    def request(self, endpoint, message, timeout_s):
        host = self._hosts.get(endpoint)
        if host is None:
            return {"status": "unreachable", "error": f"no host bound at {endpoint}"}
        reply = host.executor.handle(deepcopy(message), authenticated_controller=CONTROLLER)
        return {"status": "ok", "response": reply}


class SimSite:
    """A site of hosts with distinct clocks, joined by an in-process transport."""

    def __init__(self, tmp_path, hosts, *, controller_host, lanes):
        if controller_host not in hosts:
            raise ValueError("controller host must be in hosts")
        if any(host_id not in hosts for host_id in lanes.values()):
            raise ValueError("every lane host must be in hosts")
        self._path = tmp_path / "authority.sqlite"
        self._controller_host = controller_host
        self._lanes = dict(lanes)
        self._request_ids = count(1)
        self.hosts = {host_id: SimHost(host_id, SimClock(**args)) for host_id, args in hosts.items()}
        self.transport = InProcessTransport()
        for host_id, host in self.hosts.items():
            self.transport.bind(host_id, host)
        self.restart_authority()

    def restart_authority(self):
        self.authority = Authority(
            SQLiteStore(self._path), self.transport, self.hosts[self._controller_host].clock,
            lanes=[
                {"lane_id": lane_id, "host_id": host_id, "reachability": "confirmed", "enabled": True}
                for lane_id, host_id in self._lanes.items()
            ],
            identity_mapping=[{"external_id": PEER, "principal": PRINCIPAL, "roles": ["agent"]}],
        )
        return self.authority

    def rpc(self, op, args, *, lane=None):
        request = {"schema": 1, "request_id": f"sim-{next(self._request_ids)}", "op": op, "lane": lane, "args": deepcopy(dict(args))}
        request["request_fingerprint"] = request_fingerprint(request, principal=PRINCIPAL)
        return self.authority.handle(request, peer=PEER)

    def tick(self):
        self.authority.enforce_deadlines(peer=PEER)
        for host in self.hosts.values():
            host.executor.enforce_deadlines()

    def advance(self, seconds, *, step_s=60.0):
        if seconds < 0 or step_s <= 0:
            raise ValueError("advance needs nonnegative seconds and a positive step")
        remaining = seconds
        while remaining > 0:
            step = min(step_s, remaining)
            for host in self.hosts.values():
                host.clock.advance(step)
            self.tick()
            remaining -= step

    def executor_lane(self, lane_id):
        host_id = self._lanes[lane_id]
        return self.hosts[host_id].executor.state_snapshot(
            {"site_id": "site-a", "host_id": host_id, "lane_id": lane_id}
        )
