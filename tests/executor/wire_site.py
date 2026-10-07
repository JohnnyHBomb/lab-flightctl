"""A5a3 test rig: host-1's verified site copy, a recording stub nvidia-smi and the entry point's argv, all under one directory."""

import copy
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from flightctl.transport import LocalSubprocessTransport
from tests.contracts_v2.validation import assert_valid, examples

ROOT = Path(__file__).resolve().parents[2]
CARDS = {"lane-gpu1": "GPU-00000000-0000-0000-0000-000000000002", "lane-gpu2": "GPU-00000000-0000-0000-0000-000000000003"}
LANE1 = {"site_id": "site-a", "host_id": "host-1", "lane_id": "lane-gpu1"}
LANE2 = dict(LANE1, lane_id="lane-gpu2")
OTHER = dict(LANE1, lane_id="lane-gpu0")  # a lane of host-0 in the inventory: host-1 does not serve it
STUB = """#!/bin/sh
echo "$*" >> '{log}'
case "$1" in --query-gpu=*) printf '%s\\n' {rows} ;; esac
"""


def _inventory():
    """The contract's confirmed inventory example with a second lane on host-1 (lane-gpu2) and one on host-0 (lane-gpu0)."""
    inventory = json.loads((ROOT / "config" / "inventory-v2.json.example").read_text(encoding="utf-8"))
    host, lane = inventory["hosts"][1], inventory["lanes"][0]
    host["devices"].append({**host["devices"][0], "device_id": "gpu-c", "uuid": CARDS["lane-gpu2"], "pci_bus_id": "0000:04:00.0"})
    inventory["lanes"] += [{**copy.deepcopy(lane), "lane_id": "lane-gpu2", "device_ids": ["gpu-c"]},
                           {**copy.deepcopy(lane), "lane_id": "lane-gpu0", "host_id": "host-0", "device_ids": ["gpu-a"]}]
    assert_valid(inventory, "inventory")
    return inventory


def request(kind, deadlines=None, *, lane=LANE1, protected=True):
    """A v2 reserve, beat or stop of the lane's generation 7 lease: the contract's reserve example (awake.hold_inhibitor false) sent now."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    req = copy.deepcopy(examples("executor")["valid"][0])
    deadlines = deadlines or {"expiry": 1800, "heartbeat-stale": 600, **({"max-end": 14400} if kind == "reserve" else {})}
    req["identity"]["lane"], req["execution_policy"]["protected"] = lane, protected
    req.update(kind=kind, controller_request_id=f"creq-{kind}", sent_at=stamp, awake={"hold_inhibitor": False},
               deadlines=[{"kind": name, "in_s": in_s, "sender_utc": stamp} for name, in_s in deadlines.items()])
    if kind == "stop":
        req["stop_authority"] = {"mode": "owner-release", "approval_id": None, "reason": "test stop"}
    for field in {"beat": ("awake", "execution_policy"), "stop": ("awake", "deadlines")}.get(kind, ()):
        del req[field]
    assert_valid(req, "executor", "request")
    return req


class Rig:
    """Under `root`: the state path, host-1's site copy (files and SHA256SUMS) and a stub nvidia-smi that logs every call it gets."""

    def __init__(self, root):
        self.state, self.site, self.smi, self.log = root / "state.json", root / "site", root / "nvidia-smi", root / "nvidia-smi.log"
        files = {"adapters.json": '{"profile": "sim"}\n', "inventory.json": json.dumps(_inventory())}
        sums = "".join(f"{hashlib.sha256(text.encode()).hexdigest()}  {name}\n" for name, text in files.items())
        self.site.mkdir()
        for name, text in {**files, "SHA256SUMS": sums}.items():
            (self.site / name).write_text(text, encoding="utf-8")
        rows = " ".join(f"'{card}, 300, 24576, 0, 41, 18.2, 200, Not Active, Not Active, [N/A]'" for card in CARDS.values())
        self.smi.write_text(STUB.format(log=self.log, rows=rows), encoding="utf-8")
        self.smi.chmod(0o755)

    def argv(self, *options):
        """The entry point's command with the state, host-1, its site copy and the stub, then `options`."""
        return [sys.executable, str(ROOT / "flightctl" / "executor_stdio.py"), "--state", str(self.state), "--host-id", "host-1",
                "--site-dir", str(self.site), "--nvidia-smi", str(self.smi), *options]

    def call(self, req):
        """One serve invocation as controller-a through LocalSubprocessTransport: the transport's result."""
        return LocalSubprocessTransport(self.argv("--controller", "controller-a"), host_id="host-1").call("host-1", req, timeout_s=30)

    def reply(self, req):
        """The v2 reply to `req`: the call must succeed and the reply must match the schema."""
        result = self.call(req)
        assert result["status"] == "ok", result
        assert_valid(result["reply"], "executor", "reply")
        return result["reply"]

    def smi_calls(self):
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []
