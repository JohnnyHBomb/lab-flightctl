"""A5b1 part 1 named acceptance tests: the authority's executor client speaks protocol v2 (identity before reserve, definite refusal, cause chain)."""

import hashlib
import json
import time

import pytest

from flightctl.authority import Authority
from flightctl.clock import RealClock
from flightctl.executor import ExecutorV2, MemoryStateStore
from flightctl.store import SQLiteStore
from flightctl.transport import LocalSubprocessTransport
from tests.authority.helpers import PRINCIPAL, request as rpc_request
from tests.contracts_v2.validation import assert_valid, occupancy_from_capture
from tests.executor.wire_site import CARDS, Rig
from tests.sim.rig import SimClock

CARD = "GPU-00000000-0000-0000-0000-000000000011"
LANE = {"lane_id": "lane-gpu1", "host_id": "host-1", "reachability": "confirmed", "enabled": True}
MAPPING = [{"external_id": "peer-a", "principal": PRINCIPAL, "roles": ["agent"]}]
ACQUIRE = {"purpose": "simulated work", "est_s": 600, "max_s": 3600}
TENANT = {"policy": "yield", "lane_noise_mib": 2048, "noise_cap_mib": 128, "noise_allowlist": [{"argv0": "browser", "uid": 1000}]}
LOST = {"code": "reply_lost", "message": "no reply", "layer": "transport", "cause": {"code": "transport_failed", "message": "ssh exited 255", "layer": "transport", "cause": None}}


class Probe:  # host-1's occupancy probe: the lane's card is idle until a tenant is set
    tenant = False

    def occupancy(self, host_id, lane_id, uuids, *, noise_allowlist, noise_cap_mib, lane_noise_mib, timeout_s):
        smi, procs = f"{CARD}, 300, 24576, 0, 41, 18.2, 200, Not Active, Not Active, [N/A]", f"{CARD}, 77, python, 300" if self.tenant else ""
        return occupancy_from_capture(smi, procs, returncode=0, lane_id=lane_id, host_id=host_id, observed_at="2026-10-01T12:00:00Z", lane_uuids=uuids,
                                      noise_allowlist=noise_allowlist, noise_cap_mib=noise_cap_mib, lane_noise_mib=lane_noise_mib)


class Site:
    """host-1's ExecutorV2 behind an in-process ExecutorTransport that checks and keeps every (request, reply); authority() opens a protocol-2 authority on a database."""

    def __init__(self, root):
        root.mkdir(exist_ok=True)
        self.root, self.calls, self.probe, self.tamper = root, [], Probe(), None  # tamper: a function of the transport result, applied after the host replied
        self.control, self.host_clock = SimClock(boot_id="sim-ctl"), SimClock(boot_id="sim-host-1")
        self.host = ExecutorV2(self.host_clock, host_id="host-1", store=MemoryStateStore(), lane_cards={"lane-gpu1": [CARD]}, occupancy=self.probe)

    def authority(self, database="state.sqlite"):
        return authority_v2(self.root / database, self, self.control)

    def call(self, host_id, request, *, timeout_s):
        assert (host_id, timeout_s) == ("host-1", 30.0)
        assert_valid(request, "executor", "request")
        reply = self.host.handle(json.loads(json.dumps(request)))
        assert_valid(reply, "executor", "reply")
        self.calls.append((request, reply))
        result = {"status": "ok", "reply": reply, "error": None}
        return self.tamper(result) if self.tamper else result


def authority_v2(database, transport, clock):
    return Authority(SQLiteStore(database), transport, clock, executor_protocol=2, lanes=[LANE], identity_mapping=MAPPING)


def rpc(authority, request_id, op, args):
    return authority.handle(rpc_request(request_id, op, args, lane="lane-gpu1"), peer="peer-a")


def raises(result):
    raise OSError("the transport is down")


def altered(**fields):  # the host's reply with these fields changed is no longer an answer to the request
    return lambda result: {**result, "reply": {**result["reply"], **fields}}


def test_release_identity_matches_reserve(tmp_path):
    site = Site(tmp_path)
    granted = rpc(site.authority(), "acquire-1", "acquire", ACQUIRE)
    assert granted["status"] == 200, granted
    data, lease, (reserve, _) = granted["data"], granted["data"]["lease"], site.calls[0]
    identity = {"lane": {"site_id": "site-a", "host_id": "host-1", "lane_id": "lane-gpu1"}, "generation": 1, "lease_id": lease["lease_id"],
                "token_sha256": hashlib.sha256(data["token"].encode()).hexdigest(), "run_id": lease["run_id"], "unit": "flightctl-lane-gpu1-g1.service"}
    assert (reserve["kind"], reserve["identity"], data["token"] in json.dumps(site.calls)) == ("reserve", identity, False)
    stored = site.authority().store.get_lease(lease_id=lease["lease_id"])[0]
    assert all(stored[key] == lease[key] == identity[key] for key in ("run_id", "unit", "token_sha256"))
    assert (reserve["schema_version"], reserve["controller_id"], reserve["sent_at"], reserve["awake"]) == (2, "controller-a", "2026-10-01T12:00:00Z", {"hold_inhibitor": False})
    assert reserve["deadlines"] == [{"kind": "max-end", "in_s": 3600, "sender_utc": reserve["sent_at"]}]
    assert reserve["execution_policy"] == {"class": "batch", "protected": True, "preemptible": False, "grace_s": 0, "work_mode": "holder", "holder_binding": "client-heartbeat",
                                           "external_tenant": {"policy": "collision", "lane_noise_mib": 1024, "noise_cap_mib": 64, "noise_allowlist": []}}
    released = rpc(site.authority(), "release-1", "release", {"token": data["token"]})  # a new authority on the same database
    stop = site.calls[1][0]
    assert (released["status"], stop["kind"], stop["identity"]) == (200, "stop", identity), released
    assert (stop["stop_authority"]["mode"], stop["stop_authority"]["approval_id"]) == ("owner-release", None)
    store = site.authority().store
    assert store.get_lane("lane-gpu1")["state"] == "free"
    store.put_lane({**store.get_lane("lane-gpu1"), "policy": {"external_tenant": TENANT}})  # the lane now has a tenant rule of its own
    again = rpc(site.authority(), "acquire-2", "acquire", ACQUIRE)
    second = site.calls[2][0]
    assert (again["data"]["generation"], again["data"]["lease"]["run_id"] != lease["run_id"], second["identity"]["unit"], second["execution_policy"]["external_tenant"]) == (2, True, "flightctl-lane-gpu1-g2.service", TENANT)
    assert len({call[0]["controller_request_id"] for call in site.calls}) == 3  # every message has a fresh request id


def test_definite_refusal_cancels_lease_lane_stays_free(tmp_path):
    site = Site(tmp_path)
    authority = site.authority()
    site.host_clock.advance(40)  # the host's clock is 40 s ahead of the authority's: the reserve is stale on arrival
    refused = rpc(authority, "acquire-1", "acquire", ACQUIRE)
    reply = site.calls[0][1]
    assert (reply["ok"], reply["definite"], reply["error"]["code"]) == (False, True, "clock_skew")
    error = refused["error"]
    assert (refused["status"], error["code"], error["retryable"], error["failure_class"], error["cause"]) == (503, "unavailable", True, "state", reply["error"])
    [(lease, status)] = authority.store.leases()
    assert (lease["state"], lease["close_reason"], status, authority.store.get_lane("lane-gpu1")["state"]) == ("closed", "reserve-refused", "released", "free")
    assert (rpc(authority, "acquire-1", "acquire", ACQUIRE), len(site.calls)) == (refused, 1)  # remembered: the retry sends nothing
    site.control.advance(40)  # the authority's clock catches up
    granted = rpc(authority, "acquire-2", "acquire", ACQUIRE)
    assert (granted["status"], granted["data"]["generation"]) == (200, 2), granted
    site.host_clock.advance(40)  # the same definite refusal of a stop frees nothing: the stop did not prove the lane empty
    stopped = rpc(authority, "release-1", "release", {"token": granted["data"]["token"]})
    assert (site.calls[-1][1]["error"]["code"], stopped["status"], stopped["error"]["cause"]["code"], authority.store.get_lane("lane-gpu1")["state"]) == ("clock_skew", 503, "clock_skew", "quarantined")


def test_executor_cause_reaches_rpc_error(tmp_path):
    site = Site(tmp_path)
    authority = site.authority()
    granted = rpc(authority, "acquire-1", "acquire", ACQUIRE)
    site.probe.tenant = True  # a GPU tenant holds the lane's card when the stop probes it
    failed = rpc(authority, "release-1", "release", {"token": granted["data"]["token"]})
    reply = site.calls[1][1]
    assert (reply["ok"], reply["definite"], reply["error"]["code"]) == (False, False, "gpu_tenant")
    assert (failed["status"], failed["error"]["code"], failed["error"]["cause"]) == (503, "unknown", reply["error"])
    lease, status = authority.store.get_lease(lease_id=granted["data"]["lease"]["lease_id"])
    assert (lease["state"], status, authority.store.get_lane("lane-gpu1")["state"]) == ("quarantined", "uncertain", "quarantined")
    assert "cause" not in rpc(authority, "acquire-2", "acquire", ACQUIRE)["error"]  # no executor call produced this refusal
    uncertain = {"lost": (lambda result: {"status": "lost", "reply": None, "error": LOST}, LOST), "raised": (raises, None)}  # the transport's error is the cause; none for a call that raised
    uncertain |= {name: (altered(**fields), None) for name, fields in {  # no cause either for a reply that does not answer, or whose error is no object, or that is not a definite success
        "schema_version": {"schema_version": 1}, "kind": {"kind": "beat"}, "request id": {"controller_request_id": "executor-other"}, "identity": {"echoed_identity": None},
        "not definite": {"ok": True, "definite": False, "observed_state": "free", "error": None}, "not free": {"ok": True, "definite": True, "observed_state": "stopping", "error": None},
        "v1 shape": {"acknowledgement": "stopped", "ok": False, "error": "stop pending", "observed_state": "stopping", "uncertain": False}}.items()}  # the v1 pending stop is no 202 in protocol 2
    for name, (tamper, cause) in uncertain.items():
        other = Site(tmp_path / name)
        other.probe.tenant = True
        token = rpc(other.authority(), "acquire-1", "acquire", ACQUIRE)["data"]["token"]
        other.tamper = tamper
        failed = rpc(other.authority(), "release-1", "release", {"token": token})
        assert (failed["status"], failed["error"]["code"], failed["error"]["cause"], other.authority().store.get_lane("lane-gpu1")["state"]) == (503, "unknown", cause, "quarantined"), name
    rpc(site.authority("restored.sqlite"), "acquire-1", "acquire", ACQUIRE)  # a database that does not know the lease: its reserve meets the host's fence, an answer that is not definite
    fenced = site.calls[-1][1]
    assert (fenced["ok"], fenced["definite"], fenced["error"]["code"]) == (False, False, "fenced")
    restored = site.authority("restored.sqlite")
    assert (restored.store.get_lane("lane-gpu1")["state"], restored.store.leases()[0][1]) == ("quarantined", "uncertain")


@pytest.mark.realtime
def test_reserve_stop_real_executor_process(tmp_path):
    rig, clock, database = Rig(tmp_path), RealClock(), tmp_path / "authority.sqlite"

    def entry(controller):  # the real entry point in a child process, serving as that controller
        return LocalSubprocessTransport(rig.argv("--controller", controller), host_id="host-1")

    authority = authority_v2(database, entry("controller-a"), clock)
    begun = time.monotonic()
    granted = rpc(authority, "acquire-1", "acquire", ACQUIRE)
    reserved = time.monotonic()
    released = rpc(authority, "release-1", "release", {"token": granted["data"]["token"]})
    stopped = time.monotonic()
    assert (granted["status"], released["status"], authority.store.get_lane("lane-gpu1")["state"]) == (200, 200, "free"), (granted, released)
    assert f"-q -d PIDS -i {CARDS['lane-gpu1']}" in rig.smi_calls()  # the child process probed the lane's card before it freed the lane
    denied = rpc(authority_v2(database, entry("controller-b"), clock), "acquire-2", "acquire", ACQUIRE)  # the entry point of another controller refuses
    cause = denied["error"]["cause"]
    assert (denied["status"], denied["error"]["code"], cause["code"], cause["layer"]) == (503, "unknown", "denied", "transport"), denied
    assert "Permission denied" in cause["message"]
    print(f"reserve {reserved - begun:.3f} s, release {stopped - reserved:.3f} s, denied call {time.monotonic() - stopped:.3f} s")
