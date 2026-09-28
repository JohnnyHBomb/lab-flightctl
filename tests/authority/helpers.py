from __future__ import annotations

from datetime import datetime, timezone

from flightctl.authority import Authority, request_fingerprint
from flightctl.store import SQLiteStore
from tests.fakes.clock import FakeClock


PRINCIPAL = {"site_id": "site-a", "tenant_id": "tenant-a", "issuer": "issuer-a", "subject": "subject-a"}
OTHER_PRINCIPAL = {"site_id": "site-a", "tenant_id": "tenant-a", "issuer": "issuer-a", "subject": "subject-b"}


class Executor:
    def __init__(self, *, reserve="success", stop="success"):
        self.reserve = reserve
        self.stop = stop
        self.calls: list[dict[str, object]] = []

    def request(self, endpoint, message, timeout_s):
        self.calls.append({"endpoint": endpoint, "message": dict(message), "timeout_s": timeout_s})
        outcome = self.reserve if message["kind"] == "reserve" else self.stop
        if outcome != "success":
            return {"status": outcome, "error": f"scripted {outcome}"}
        identity = dict(message["identity"])
        return {
            "status": "ok",
            "response": {
                "kind": message["kind"],
                "echoed_identity": identity,
                "acknowledgement": {"reserve": "reserved", "stop": "stopped"}[message["kind"]],
                "ok": True,
                "observed_state": "starting" if message["kind"] == "reserve" else "free",
                "uncertain": False,
                "cgroup_occupants": [],
                "gpu_tenants": [],
                "error": None,
            },
        }


def request(request_id, op, args, *, lane="lane-gpu0", principal=PRINCIPAL, admission=None):
    result = {"schema": 1, "request_id": request_id, "op": op, "lane": lane, "args": dict(args)}
    if admission is not None:
        result["admission"] = admission
    result["request_fingerprint"] = request_fingerprint(result, principal=principal)
    return result


def make_authority(tmp_path, *, transport=None, clock=None, principal=PRINCIPAL, other_mapping=True, pipelines=None, policy=None, approval_verifier=None, lanes=None):
    clock = clock or FakeClock(datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc))
    transport = transport or Executor()
    mapping = [{"external_id": "peer-a", "principal": PRINCIPAL, "roles": ["agent"]}]
    if other_mapping:
        mapping.append({"external_id": "peer-b", "principal": OTHER_PRINCIPAL, "roles": ["agent"]})
    store = SQLiteStore(tmp_path / "state.sqlite")
    authority = Authority(
        store,
        transport,
        clock,
        lanes=lanes or [{"lane_id": "lane-gpu0", "host_id": "host-1", "reachability": "confirmed", "enabled": True}],
        identity_mapping=mapping,
        pipelines=pipelines,
        policy=policy,
        approval_verifier=approval_verifier,
    )
    return authority, transport, clock
