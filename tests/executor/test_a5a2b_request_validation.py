"""A5a2b named acceptance test: ExecutorV2 validates every v2 request against the frozen executor.schema.json#/$defs/request first."""

import copy
import json
from datetime import timedelta

from tests.contracts_v2.validation import assert_valid, examples
from tests.executor.test_a5a_executor_v2 import LANE, SENT, ScriptedProbe, call, observation, request
from tests.executor.test_a5a2_executor_v2 import FREED, HELD, HOLD, RELEASE, CountingStore, ScriptedInhibitor, ask, executor, max_end, reserve
from tests.sim.rig import SimClock

DROP, NAN, INF = object(), float("nan"), float("inf")  # DROP: the edit removes the key
FREE = {"fences": [], "observed_state": "free", "max_end_remaining_s": None, "inhibitor": None}  # a reply that describes no lane
EDITS = (  # (the valid request, the one place an edit makes invalid, the value put there); in brackets what the base did
    ("reserve", ("surplus",), 1), ("beat", ("identity", "surplus"), 1),  # an undeclared key (served)
    ("reserve", ("identity",), DROP), ("reserve", ("awake",), DROP), ("beat", ("deadlines",), DROP),  # a missing key (KeyError)
    ("stop", ("stop_authority", "reason"), DROP),
    ("reserve", ("identity", "generation"), True), ("beat", ("deadlines", 0, "in_s"), False),  # a boolean for an integer (served), a number
    ("reserve", ("identity", "generation"), "7"), ("ceiling", ("max_end", "in_s"), "60"),  # a string for an integer (TypeError), a number
    ("beat", ("identity", "generation"), 0), ("reserve", ("deadlines", 1, "in_s"), 31536001),  # out of range
    ("reserve", ("execution_policy", "class"), "root"), ("stop", ("stop_authority", "mode"), "whim"), ("inspect", ("scope",), "site"),
    ("reserve", ("schema_version",), 1), ("reserve", ("execution_policy", "preemptible"), True),  # protected and preemptible (served)
    ("reserve", ("controller_id",), "controller-a\n"), ("beat", ("identity", "lane", "lane_id"), "lane-gpu1\n"),  # a trailing newline (served; not_found)
    ("extend", ("approval_id",), "apr 1"), ("beat", ("identity", "token_sha256"), "C" * 63),
    ("reserve", ("deadlines", 1, "in_s"), NAN), ("extend", ("max_end", "in_s"), INF), ("beat", ("deadlines", 0, "in_s"), -INF),  # (ValueError)
    ("reserve", ("sent_at",), "notadateZ"), ("beat", ("sent_at",), "2026-10-02Z"),  # (ValueError; clock_skew)
    *(("stop", ("sent_at",), stamp) for stamp in ("2026-10-02T25:00:00Z", "2026-02-30T10:00:00Z", "2026-10-02T10:00:60Z", "2026-10-02T10:00Z",
                                                  "20261002T100000Z", "2026-10-02t10:00:00Z", "2026-10-02T10:00:00.Z", "2026-10-02T10:00:00Z\n")),
    ("reserve", ("deadlines", 0, "sender_utc"), "2026-13-02T10:00:00Z"), ("beat", ("deadlines", 1, "sender_utc"), "2026-10-02T10:00:00+00:00"),
)


def edit(req, place, value):
    """A copy of req with value put at place (its keys and indexes); DROP removes the key there."""
    req = copy.deepcopy(req)
    *path, last = place
    node = req
    for key in path:
        node = node[key]
    if value is DROP:
        del node[last]
    else:
        node[last] = value
    return req


def refused(host, req, echo=None, lane=FREE, *, schema=True):
    """The reply to one invalid request: a definite invalid refusal that echoes echo and describes lane (no lane: FREE)."""
    reply = host.handle(req)
    if schema:  # the request is an object with one of the schema's kinds and a valid controller_request_id: a valid reply exists
        assert_valid(reply, "executor", "reply")
    fields = req if isinstance(req, dict) else {}
    assert (reply["kind"], reply["controller_request_id"], reply["echoed_identity"]) == (fields.get("kind"), fields.get("controller_request_id"), echo)
    assert (reply["ok"], reply["definite"], reply["unit"], reply["occupancy"]) == (False, True, None, None)
    assert (reply["error"]["code"], reply["error"]["layer"], reply["error"]["cause"]) == ("invalid", "executor", None)
    assert {key: reply[key] for key in FREE} == lane


def test_invalid_requests_are_definite_and_write_nothing():
    store, probe, port = CountingStore(), ScriptedProbe(observation()), ScriptedInhibitor(HELD, FREED)
    host = executor(SimClock(boot_id="sim-host-1", utc_start=SENT), store, probe, port)
    reserved = call(host, reserve())  # a valid fence on the lane, holding the inhibitor
    saved, fenced = copy.deepcopy(store.value), {key: reserved[key] for key in FREE}
    valid = {"reserve": reserve(), "beat": request("beat", SENT), "stop": request("stop", SENT), "inspect": ask("inspect", SENT, scope="lease"),
             "ceiling": ask("ceiling", SENT, max_end=max_end(SENT, 60)), "extend": ask("extend", SENT, approval_id="apr-0000001", max_end=max_end(SENT, 7200))}
    for kind, place, value in EDITS:  # an invalid identity is not echoed; a reserve's definite refusal and an invalid identity describe no lane
        echo = None if place[0] == "identity" else valid[kind]["identity"]
        refused(host, edit(valid[kind], place, value), echo, fenced if echo and kind != "reserve" else FREE)
    first, as_reply, beat, session = copy.deepcopy(examples("executor")["invalid"])  # the contract's own (the first: unavailable)
    for req, echo, lane in ((first, None, FREE), (as_reply, None, FREE), (beat, beat["identity"], fenced), (session, None, FREE)):
        refused(host, req, echo, lane)
    stale = edit(reserve(SENT - timedelta(minutes=5)), ("surplus",), 1)  # invalid and stale: invalid, not clock_skew
    refused(host, stale, stale["identity"])
    elsewhere = edit(request("beat", SENT, lane=dict(LANE, host_id="host-2")), ("surplus",), 1)  # invalid and another host's lane: not not_found
    refused(host, elsewhere, elsewhere["identity"])
    nested = json.loads("[" * 1000 + "]" * 1000)  # parses, but nests too deep for copy.deepcopy
    for thing in ([], [valid["beat"]], "beat", 7, None, {}, {"kind": nested, "controller_request_id": nested}):  # not request objects
        refused(host, thing, schema=False)  # the same refusal (TypeError on the base); no schema-valid reply exists
    for place, value in ((("kind",), "renew"), (("kind",), DROP), (("controller_request_id",), "creq beat")):  # nor for these
        refused(host, edit(valid["beat"], place, value), valid["beat"]["identity"], fenced, schema=False)
    assert (store.value, store.saves, probe.calls, port.calls) == (saved, 1, [], [HOLD])  # nothing saved, neither port called
    assert call(host, valid["beat"] | {"sent_at": "2026-10-02T10:00:00.25Z"})["ok"]  # a fraction of a second is a valid date-time
    stopped = call(host, valid["stop"])
    assert (stopped["ok"], stopped["observed_state"], stopped["fences"], port.calls, store.saves) == (True, "free", [], [HOLD, RELEASE], 3)
