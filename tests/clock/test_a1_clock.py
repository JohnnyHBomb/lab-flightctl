"""A1: the clock real twin (host boot id and monotonic clock, never the process id)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
MADE_UP_BOOT_ID = "0f1e2d3c-4b5a-4968-8776-a5b4c3d2e1f0"

_CLOCK_CHILD = """
import json
from flightctl.clock import RealClock
clock = RealClock()
print(json.dumps({"boot_id": clock.boot_id(), "monotonic": clock.monotonic()}))
"""

_AUTHORITY_CHILD = """
import json, sys
from flightctl.authority import Authority
from flightctl.store import SQLiteStore
from tests.authority.helpers import PRINCIPAL, request
store = SQLiteStore(sys.argv[1])
authority = Authority(
    store,
    None,
    lanes=[{"lane_id": "lane-gpu0", "host_id": "host-1", "reachability": "confirmed", "enabled": True}],
    identity_mapping=[{"external_id": "peer-a", "principal": PRINCIPAL, "roles": ["agent"]}],
)
reply = authority.handle(request(sys.argv[2], "free", {}), peer="peer-a")
kind = type(authority.clock)
store.close()
print(json.dumps({"status": reply["status"], "error": reply.get("error"), "clock": kind.__module__ + "." + kind.__qualname__}))
"""


def _child(script: str, *args: str) -> dict:
    done = subprocess.run([sys.executable, "-c", script, *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.realtime
def test_boot_id_stable_across_process_restart() -> None:
    from flightctl.clock import BOOT_ID_PATH

    host_boot = Path(BOOT_ID_PATH).read_text(encoding="utf-8").strip()
    seen = []
    for _ in range(2):
        before = time.monotonic()
        reply = _child(_CLOCK_CHILD)
        after = time.monotonic()
        assert before <= reply["monotonic"] <= after, (before, reply, after)
        seen.append(reply["boot_id"])
    assert seen[0] == seen[1] == host_boot
    assert UUID.fullmatch(seen[0]), seen[0]


class _OtherBootClock:
    def utc(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()

    def boot_id(self) -> str:
        return MADE_UP_BOOT_ID


@pytest.mark.realtime
def test_authority_restart_on_same_boot_keeps_serving(tmp_path) -> None:
    from flightctl.authority import Authority
    from flightctl.store import SQLiteStore
    from tests.authority.helpers import PRINCIPAL, request

    state = str(tmp_path / "state.sqlite")
    replies = [_child(_AUTHORITY_CHILD, state, f"free-{index}") for index in range(2)]
    assert [reply["status"] for reply in replies] == [200, 200], replies
    assert [reply["clock"] for reply in replies] == ["flightctl.clock.RealClock"] * 2, replies

    store = SQLiteStore(state)
    rebooted = Authority(
        store,
        None,
        _OtherBootClock(),
        lanes=[{"lane_id": "lane-gpu0", "host_id": "host-1", "reachability": "confirmed", "enabled": True}],
        identity_mapping=[{"external_id": "peer-a", "principal": PRINCIPAL, "roles": ["agent"]}],
    )
    reply = rebooted.handle(request("free-rebooted", "free", {}), peer="peer-a")
    store.close()
    assert reply["status"] == 503, reply
    assert "clock skew or reboot requires reconciliation" in json.dumps(reply), reply


def test_real_clock_refuses_an_unreadable_boot_id(tmp_path) -> None:
    import flightctl.authority
    import flightctl.clock
    from flightctl.clock import ClockUnavailable, RealClock

    assert flightctl.authority.RealClock is flightctl.clock.RealClock

    good = tmp_path / "boot_id"
    good.write_text(MADE_UP_BOOT_ID + "\n", encoding="utf-8")
    clock = RealClock(boot_id_path=str(good))
    assert clock.boot_id() == MADE_UP_BOOT_ID
    good.unlink()
    assert clock.boot_id() == MADE_UP_BOOT_ID, "read once, at construction"

    missing = tmp_path / "missing"
    with pytest.raises(ClockUnavailable, match=re.escape(str(missing))):
        RealClock(boot_id_path=str(missing))
    for name, content in {
        "empty": "",
        "process": "process-42",
        "upper": MADE_UP_BOOT_ID.upper(),
        "nodashes": MADE_UP_BOOT_ID.replace("-", ""),
        "extra": MADE_UP_BOOT_ID + " extra",
        "binary": b"\xff\xfe",
    }.items():
        bad = tmp_path / name
        bad.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        with pytest.raises(ClockUnavailable, match=re.escape(str(bad))):
            RealClock(boot_id_path=str(bad))


def test_clock_twins_registered_for_conformance() -> None:
    import tests.conformance.impl_clock  # noqa: F401
    from flightctl.clock import BOOT_ID_PATH, RealClock
    from tests.conformance import registry

    found = dict(registry.implementations("clock"))
    assert sorted(found) == ["fake", "real"]

    real = found["real"]("host-1")
    assert isinstance(real, RealClock)
    assert real.utc().tzinfo is timezone.utc
    assert real.boot_id() == Path(BOOT_ID_PATH).read_text(encoding="utf-8").strip()

    fake = found["fake"]("host-1")
    assert not isinstance(fake, RealClock)
    assert fake.boot_id().startswith("sim-")
    first = fake.monotonic()
    time.sleep(0.01)
    assert fake.monotonic() == first
    fake.advance(1.5)
    assert fake.monotonic() == first + 1.5
