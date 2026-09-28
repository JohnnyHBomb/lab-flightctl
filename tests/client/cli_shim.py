#!/usr/bin/env python3
"""Executable transport shim used by the real lanes.sh quoting test."""

from __future__ import annotations

import copy
import json
import sys
from io import StringIO
from pathlib import Path
from typing import Mapping

from flightctl.flightctl import main
from tests.fakes.ssh import FakeTransport


ROOT = Path(__file__).parents[2]
CLI = json.loads((ROOT / "tests" / "contracts" / "vectors" / "cli.json").read_text(encoding="utf-8"))["cases"]
ADMISSION = json.loads((ROOT / "tests" / "contracts" / "vectors" / "rpc.json").read_text(encoding="utf-8"))["request_defaults"]["admission"]


class EchoFake:
    def __init__(self) -> None:
        self._fake = FakeTransport(
            [
                {"outcome": "success", "response": copy.deepcopy(CLI[0]["response"])},
                {"outcome": "success", "response": copy.deepcopy(CLI[1]["response"])},
            ]
        )

    @property
    def calls(self) -> list[dict[str, object]]:
        return self._fake.calls

    def request(self, endpoint: str, message: Mapping[str, object], timeout_s: float) -> Mapping[str, object]:
        raw = self._fake.request(endpoint, message, timeout_s)
        response = raw.get("response")
        if isinstance(response, Mapping):
            echoed = dict(response)
            echoed["request_id"] = message["request_id"]
            return {**raw, "response": echoed}
        return raw


def run() -> int:
    if len(sys.argv) < 3 or sys.argv[1:3] != ["-m", "flightctl.flightctl"]:
        return 2
    transport = EchoFake()
    handoffs: list[tuple[Mapping[str, object], tuple[str, ...]]] = []
    stdout, stderr = StringIO(), StringIO()
    code = main(
        sys.argv[3:],
        transport=transport,
        admission=ADMISSION,
        handoff=lambda grant, workload: handoffs.append((grant, tuple(workload))) or True,
        stdout=stdout,
        stderr=stderr,
    )
    print(json.dumps({"exit": code, "calls": transport.calls, "handoffs": handoffs, "stdout": stdout.getvalue(), "stderr": stderr.getvalue()}))
    return code


if __name__ == "__main__":
    raise SystemExit(run())
