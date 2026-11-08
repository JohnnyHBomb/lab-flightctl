"""A5b2 named acceptance tests: a protocol-2 authority beats every live lease, renew rolls the expiry inside the ceiling, and max-end goes out with the margin."""

import json
import threading
import time
from datetime import timedelta

import pytest

from flightctl.authority import Authority
from flightctl.clock import RealClock
from flightctl.executor import ExecutorV2, MemoryStateStore
from flightctl.store import SQLiteStore
from flightctl.transport import LocalSubprocessTransport
from tests.authority.helpers import PRINCIPAL, request as rpc_request
from tests.contracts_v2.validation import assert_valid, ceiling_confirmed, host_ceiling_bound
from tests.executor.wire_site import CARDS, Rig
from tests.sim.rig import SimClock

CARD = "GPU-00000000-0000-0000-0000-000000000011"
LANE = {"lane_id": "lane-gpu1", "host_id": "host-1", "reachability": "confirmed", "enabled": True}
LANE_REF = {"site_id": "site-a", "host_id": "host-1", "lane_id": "lane-gpu1"}
MAPPING = [{"external_id": "peer-a", "principal": PRINCIPAL, "roles": ["agent"]}]
ACQUIRE = {"class": "batch", "purpose": "simulated work", "est_s": 600, "max_s": 3600}


def authority_v2(database, transport, clock):
    return Authority(SQLiteStore(database), transport, clock, executor_protocol=2, lanes=[LANE], identity_mapping=MAPPING)


def rpc(authority, request_id, op, args):
    return authority.handle(rpc_request(request_id, op, args, lane="lane-gpu1"), peer="peer-a")


def deadlines(request):  # the (kind, in_s) of a request's relative deadlines, sorted by kind
    return sorted((item["kind"], item["in_s"]) for item in request["deadlines"])


class Site:
    """host-1's ExecutorV2 on its own SimClock behind an in-process ExecutorTransport; keeps (request, reply, the authority's monotonic time at return) of every call.

    skew_s: how far the host's UTC is ahead of the authority's. delays: the seconds the request leg of each call takes in turn (both clocks run), then none.
    tamper: a function of the transport result, applied after the host replied."""

    def __init__(self, root, *, skew_s=0, delays=()):
        root.mkdir(exist_ok=True)
        self.calls, self.delays, self.tamper = [], list(delays), None
        self.control, self.host_clock = SimClock(boot_id="sim-ctl"), SimClock(boot_id="sim-host-1", skew_s=skew_s)
        self.host = ExecutorV2(self.host_clock, host_id="host-1", store=MemoryStateStore(), lane_cards={"lane-gpu1": [CARD]})
        self.authority = authority_v2(root / "state.sqlite", self, self.control)

    def advance(self, seconds):
        self.control.advance(seconds)
        self.host_clock.advance(seconds)

    def call(self, host_id, request, *, timeout_s):
        assert (host_id, timeout_s) == ("host-1", 30.0)
        assert_valid(request, "executor", "request")
        self.advance(self.delays.pop(0) if self.delays else 0)  # the request leg
        reply = self.host.handle(json.loads(json.dumps(request)))
        assert_valid(reply, "executor", "reply")
        self.calls.append((request, reply, self.control.monotonic()))
        result = {"status": "ok", "reply": reply, "error": None}
        return self.tamper(result) if self.tamper else result


class Minutes(threading.Event):
    """run_beat_loop's stop event on simulated time: each wait moves both clocks on by its timeout, then runs the host's enforcer; the `count`th wait sets it."""

    def __init__(self, site, count):
        super().__init__()
        self.site, self.count, self.waits, self.acted = site, count, [], []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.site.advance(timeout)
        self.acted += self.site.host.enforce_deadlines()
        if len(self.waits) == self.count:
            self.set()
        return self.is_set()


def changed(**fields):  # the host's reply with these fields changed
    return lambda result: {**result, "reply": {**result["reply"], **fields}}


def raises(result):
    raise OSError("the transport is down")


def test_renew_rolls_within_ceiling_and_refuses_past_it(tmp_path):
    site = Site(tmp_path)
    authority, start = site.authority, site.control.utc()
    granted = rpc(authority, "acquire-1", "acquire", ACQUIRE)
    assert granted["status"] == 200, granted
    token, lease_id = granted["data"]["token"], granted["data"]["lease"]["lease_id"]

    def at(seconds):  # the lease's time text this many seconds after the acquire
        return (start + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def renew(request_id, extend_s):
        return rpc(authority, f"renew-{request_id}", "renew", {"token": token, "extend_s": extend_s})

    def lease():  # the stored (max_end, approved_max_end)
        record = authority.store.get_lease(lease_id=lease_id)[0]
        return record["max_end"], record["approved_max_end"]

    def beat():  # the deadlines of the beat sent now
        assert [item["ok"] for item in authority.beat_leases()] == [True]
        return deadlines(site.calls[-1][0])

    assert lease() == (at(3600), at(3600))  # an acquire starts at the ceiling
    site.advance(300)
    refused = [renew(f"bad-{n}", extend) for n, extend in enumerate((0, -5, 1.5, "60"))]
    assert [(item["status"], item["error"]["code"]) for item in refused] == [(403, "invalid")] * 4
    assert renew(1, 120)["status"] == 200 and lease() == (at(420), at(3600))  # a renew some minutes in: expiry = now + extension, the ceiling stays
    assert beat() == [("expiry", 120), ("heartbeat-stale", 600)]
    site.advance(30)
    assert renew(2, 45)["status"] == 200 and lease() == (at(375), at(3600))  # it may move the expiry earlier
    site.advance(30)
    assert renew(3, 7200)["status"] == 200 and lease() == (at(3600), at(3600))  # past the ceiling: the expiry stops at the approved maximum
    assert beat() == [("expiry", 3240), ("heartbeat-stale", 600)]
    site.advance(3599 - 360)
    assert renew(4, 60)["status"] == 200 and lease() == (at(3600), at(3600))  # still before it
    site.advance(1)
    late = renew(5, 60)
    assert (late["status"], late["error"]["code"], lease()) == (409, "conflict", (at(3600), at(3600)))  # at it: refused, nothing moved


def test_beat_loop_keeps_lease_alive_30min_sim(tmp_path):
    site = Site(tmp_path)
    authority, host = site.authority, site.host
    granted = rpc(authority, "acquire-1", "acquire", ACQUIRE)
    assert granted["status"] == 200, granted
    lease_id, reserve = granted["data"]["lease"]["lease_id"], site.calls[0][0]
    assert deadlines(reserve) == [("expiry", 3600), ("heartbeat-stale", 600), ("max-end", 3570)]  # approved - now - the 30 s margin
    assert {item["sender_utc"] for item in reserve["deadlines"]} == {reserve["sent_at"]}
    minutes = Minutes(site, 30)
    authority.run_beat_loop(minutes)  # the default interval is the policy's heartbeat_s: a beat at once, then one a minute for 30 simulated minutes
    assert (minutes.waits, minutes.acted, len(site.calls)) == ([60] * 30, [], 31)  # the host's enforcer acted on nothing
    for minute, (beat, reply, _) in enumerate(site.calls[1:]):
        assert (beat["kind"], beat["identity"], "execution_policy" in beat) == ("beat", reserve["identity"], False)
        assert deadlines(beat) == [("expiry", 3600 - 60 * minute), ("heartbeat-stale", 600)], minute
        assert (reply["ok"], reply["definite"], reply["observed_state"]) == (True, True, "reserved"), minute
    site.advance(0.5)
    assert [(item["lease_id"], item["ok"]) for item in authority.beat_leases()] == [(lease_id, True)]
    assert deadlines(site.calls[-1][0]) == [("expiry", 1799), ("heartbeat-stale", 600)]  # whole seconds, rounded down
    v1 = Authority(SQLiteStore(tmp_path / "state.sqlite"), site, site.control, lanes=[LANE], identity_mapping=MAPPING)  # protocol 1 over the same database
    assert (v1.beat_leases(), len(site.calls)) == ([], 32)
    unanswered = {"not reserved": changed(observed_state="quarantined"), "not definite": changed(definite=False), "not ok": changed(ok=False), "other request": changed(controller_request_id="x"),
                  "other identity": changed(echoed_identity=None), "other schema": changed(schema_version=1), "raised": raises, "lost": lambda result: {"status": "lost", "reply": None, "error": None}}
    for name, tamper in unanswered.items():  # a beat that raises, is refused or is not answered is reported not ok, never raised
        site.tamper = tamper
        assert [item["ok"] for item in authority.beat_leases()] == [False], name
    site.tamper = None
    site.advance(599)  # the beats stop: one second short of the stale duration after the last one
    assert host.enforce_deadlines() == []
    site.advance(1)
    assert host.enforce_deadlines() == [{"lane": LANE_REF, "generation": 1, "due": ["heartbeat-stale"], "state": "quarantined"}]
    assert [item["ok"] for item in authority.beat_leases()] == [False] and site.calls[-1][1]["error"]["code"] == "conflict"  # a refused beat is reported, not raised
    record = authority.store.get_lease(lease_id=lease_id)[0]
    for state in ("starting", "running", "stopping", "closed", "quarantined"):  # only an acknowledged starting or running lease is beaten
        for status in ("pending", "acknowledged", "uncertain", "released"):
            authority.store.put_lease({**record, "state": state}, reservation_status=status)
            assert bool(authority.beat_leases()) == (status == "acknowledged" and state in ("starting", "running")), (state, status)
    short = Site(tmp_path / "short")  # a preemptible lease whose approved maximum lies inside the margin: max-end is 0, never negative, and its stale duration is 180 s
    assert rpc(short.authority, "acquire-1", "acquire", {**ACQUIRE, "class": "service", "est_s": 10, "max_s": 20})["status"] == 200
    short.authority.preemptible_stale_s = 90  # the stale durations are read each time a message is built
    short.authority.beat_leases()
    assert [deadlines(call[0]) for call in short.calls] == [[("expiry", 20), ("heartbeat-stale", 180), ("max-end", 0)], [("expiry", 20), ("heartbeat-stale", 90)]]


def test_slow_round_trip_triggers_shorten_only_ceiling(tmp_path):
    def acquire(name, delays):  # the host's clock is 30 s behind the authority's; the approved maximum is 3600 s after the acquire, at monotonic 3600
        site = Site(tmp_path / name, skew_s=-30, delays=delays)
        return site, rpc(site.authority, "acquire-1", "acquire", ACQUIRE), [call[0]["kind"] for call in site.calls]

    site, granted, kinds = acquire("slow", [60, 45.5])  # the reserve's request leg takes 60 s, the first ceiling's 45.5 s, the second's none
    assert (granted["status"], kinds) == (200, ["reserve", "ceiling", "ceiling"]), granted  # a ceiling that fits the margin: the grant goes ahead
    assert [at + reply["max_end_remaining_s"] for _, reply, at in site.calls] == [3630, 3615.5, 3569.5]  # the host's max-end: after the maximum, still after it, shortened
    assert host_ceiling_bound(send_t=0, rtt_s=site.calls[0][2], in_s=3570, approved_max_end_t=3600)["must_shorten"]
    assert [ceiling_confirmed(reply_received_t=at, max_end_remaining_s=reply["max_end_remaining_s"], approved_max_end_t=3600) for _, reply, at in site.calls] == [False, False, True]
    for (ceiling, _, _), sent_t in zip(site.calls[1:], (60, 105.5)):
        assert set(ceiling) == {"schema_version", "kind", "controller_request_id", "controller_id", "sent_at", "identity", "max_end"}  # no execution_policy, no deadlines
        assert ceiling["max_end"] == {"kind": "max-end", "in_s": int(3570 - sent_t), "sender_utc": ceiling["sent_at"]}
        assert ceiling["identity"] == site.calls[0][0]["identity"]
    fast, _, kinds = acquire("fast", [])
    fast.delays = [45]  # a slow beat sends no ceiling either
    fast.authority.beat_leases()
    assert (kinds, [call[0]["kind"] for call in fast.calls], acquire("margin", [30])[2]) == (["reserve"], ["reserve", "beat"], ["reserve"])  # a round trip at or below the margin sends none
    refused = Site(tmp_path / "refused", skew_s=40, delays=[31])  # the host's clock 40 s ahead: a slow reserve that is definitely refused sends no ceiling
    result = rpc(refused.authority, "acquire-1", "acquire", ACQUIRE)
    assert ([call[0]["kind"] for call in refused.calls], result["status"], refused.calls[0][1]["error"]["code"]) == (["reserve"], 503, "clock_skew")
    assert acquire("never fits", [60] * 9)[2] == ["reserve", "ceiling", "ceiling", "ceiling"]  # at most 3 ceilings per reserve
    wide = Site(tmp_path / "wide", skew_s=-30, delays=[40])
    wide.authority.ceiling_margin_s = 45  # read each time a message is built: max-end and the slow threshold follow it
    assert (rpc(wide.authority, "acquire-1", "acquire", ACQUIRE)["status"], [(call[0]["kind"], dict(deadlines(call[0]))["max-end"]) for call in wide.calls]) == (200, [("reserve", 3555)])


@pytest.mark.realtime
def test_beat_loop_real_time(tmp_path):
    rig, rounds, stale_s, sent = Rig(tmp_path), [], 10, []  # rounds: (monotonic at the start, at the end, the beats) of each beat_leases() call; sent: the kind of each transport call
    wire = LocalSubprocessTransport(rig.argv("--controller", "controller-a"), host_id="host-1")
    wire_call, wire.call = wire.call, lambda host_id, message, **kw: sent.append(message["kind"]) or wire_call(host_id, message, **kw)
    authority = authority_v2(tmp_path / "authority.sqlite", wire, RealClock())
    authority.preemptible_stale_s = stale_s  # a preemptible (service) lease: freed on the stub's emptiness proof once stale
    granted = rpc(authority, "acquire-1", "acquire", {**ACQUIRE, "class": "service"})
    assert granted["status"] == 200, granted
    beat_leases = authority.beat_leases

    def timed():
        began = time.monotonic()
        beats = beat_leases()
        rounds.append((began, time.monotonic(), beats))
        return beats

    def enforce():  # the host timer's one-shot in its own process: (monotonic at its return, the list it acted on)
        done = rig.run(rig.argv("--enforce"))
        assert done.returncode == 0, done
        return time.monotonic(), json.loads(done.stdout)

    authority.beat_leases = timed
    stop = threading.Event()
    loop = threading.Thread(target=authority.run_beat_loop, args=(stop, 3.0))
    loop_began = time.monotonic()
    loop.start()
    try:
        begun = time.monotonic()
        while time.monotonic() - begun < 20:
            assert enforce()[1] == []  # beaten every 3 s, a lease with a 10 s stale duration never goes stale
            time.sleep(1)
    finally:
        stop.set()
        stopped = time.monotonic()
        loop.join(timeout=10)
    beats_sent = sent.count("beat")
    assert not loop.is_alive() and time.monotonic() - stopped <= 3.0  # it returned within one interval of the stop
    count = len(rounds)
    assert rig.smi_calls() == [] and 5 <= count <= 8 and all(beat["ok"] for _, _, beats in rounds for beat in beats) and all(len(beats) == 1 for _, _, beats in rounds), rounds
    assert rounds[0][0] - loop_began < 1.5 and all(3.0 <= b[0] - a[0] < 8.0 for a, b in zip(rounds, rounds[1:])), [r[0] - loop_began for r in rounds]  # the first at once, then every 3 s
    last_began = rounds[-1][0]
    returned, acted = enforce()
    while not acted and returned < last_began + stale_s + 10:  # nobody beats now: the enforcer acts once the stale duration has passed
        time.sleep(0.5)
        returned, acted = enforce()
    assert returned >= last_began + stale_s, (returned - last_began, acted)
    assert acted == [{"lane": LANE_REF, "generation": 1, "due": ["heartbeat-stale"], "state": "free"}] and f"-q -d PIDS -i {CARDS['lane-gpu1']}" in rig.smi_calls()
    assert (len(rounds), sent.count("beat")) == (count, beats_sent)  # the loop beat nothing after it returned
    print(f"{count} beat rounds in {stopped - begun:.1f} s, the enforcer acted {returned - last_began:.1f} s after the last one began (stale {stale_s} s)")
