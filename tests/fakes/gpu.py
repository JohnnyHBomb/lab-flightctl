"""Fixture-backed GPU parsing; it never calls a vendor utility or reads sysfs."""

from __future__ import annotations

import csv
import io
from typing import Mapping


def _unknown(reason: str) -> dict[str, object]:
    return {"vendor": None, "model": None, "vram_bytes": None, "driver": None, "count": None, "reason": reason, "devices": []}


def parse_nvidia_smi(raw: str) -> dict[str, object]:
    rows = list(csv.reader(io.StringIO(raw.strip()))) if raw.strip() else []
    if not rows:
        return _unknown("nvidia-smi returned no output")
    devices: list[dict[str, object]] = []
    for index, row in enumerate(rows):
        if len(row) < 4:
            return _unknown("unparsable nvidia-smi row")
        model, memory_mib, driver, vendor = (part.strip() for part in row[:4])
        try:
            vram_bytes = int(memory_mib) * 1024 * 1024
        except ValueError:
            return _unknown("invalid VRAM measurement")
        devices.append({"device_id": f"gpu{index}", "vendor": vendor.lower(), "model": model, "vram_bytes": vram_bytes, "driver": driver})
    return {"vendor": devices[0]["vendor"], "model": devices[0]["model"], "vram_bytes": devices[0]["vram_bytes"], "driver": devices[0]["driver"], "count": len(devices), "reason": None, "devices": devices}


def parse_amd_sysfs(raw: str) -> dict[str, object]:
    fields = {}
    for line in raw.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key.strip()] = value.strip()
    required = ("vendor", "model", "vram_bytes", "driver")
    if any(not fields.get(key) for key in required):
        return _unknown("incomplete AMD/sysfs observation")
    try:
        vram = int(fields["vram_bytes"])
    except ValueError:
        return _unknown("invalid VRAM measurement")
    device = {"device_id": "gpu0", "vendor": fields["vendor"].lower(), "model": fields["model"], "vram_bytes": vram, "driver": fields["driver"]}
    return {**device, "count": 1, "reason": None, "devices": [device]}


class FakeGPUProbe:
    def __init__(self, fixtures: Mapping[str, Mapping[str, object]] | None = None, scripted: list[Mapping[str, object]] | None = None) -> None:
        self.fixtures = dict(fixtures or {})
        self.scripted = list(scripted or [])
        self.calls: list[dict[str, object]] = []

    def inspect(self, host: str) -> Mapping[str, object]:
        self.calls.append({"host": host})
        if self.scripted:
            return dict(self.scripted.pop(0))
        fixture = self.fixtures.get(host)
        if fixture is None:
            return _unknown("no scripted fixture")
        raw = str(fixture.get("raw", ""))
        family = fixture.get("family", "nvidia")
        if family == "nvidia":
            return parse_nvidia_smi(raw)
        if family == "amd":
            return parse_amd_sysfs(raw)
        if family == "none":
            return {"vendor": None, "model": None, "vram_bytes": None, "driver": None, "count": 0, "reason": "confirmed no GPU", "devices": []}
        return _unknown("unknown fixture family")
