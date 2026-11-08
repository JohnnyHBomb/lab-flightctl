"""A4ub named acceptance tests: the executor host's unit templates, their renderer render_unit, and the enforcer timer on a real host."""

import hashlib
import os
import re
import string
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tools.check_portability import scan_text

ROOT = Path(__file__).resolve().parents[2]
SERVICE, TIMER = "units/host/flightctl-executor.service.template", "units/host/flightctl-executor.timer.template"
EXAMPLE = {"FLIGHTCTL_PYTHON": "/site/venv/bin/python", "FLIGHTCTL_CHECKOUT": "/site/checkout", "FLIGHTCTL_STATE": "/site/state/executor.json",
           "FLIGHTCTL_HOST_ID": "host-1", "FLIGHTCTL_SITE_DIR": "/site/config"}
WORKLOAD = re.compile(r"flightctl-[a-z][a-z0-9._-]{0,63}-g[1-9][0-9]{0,17}\.service")  # A6's transient workload units
INHIBITOR = re.compile(r"flightctl-awake-[a-z0-9._-]+-g[0-9]+\.service")  # A11's transient idle inhibitors
UNIT = "flightctl-a4ub-timer-test"  # the on-lab test's own transient timer and service


def sections(text):
    """{section: [its lines]} of a unit file, in file order; comment and blank lines are dropped."""
    found = {}
    for line in text.splitlines():
        if line.startswith("["):
            found[line.strip("[]")] = lines = []
        elif line.strip() and not line.startswith("#"):
            lines.append(line)
    return found


def test_templates_render_without_site_strings():
    from flightctl.siteconfig import SiteConfigRefused, render_unit

    found = sorted(path.relative_to(ROOT).as_posix() for path in (ROOT / "units").glob("**/*.template"))
    assert found == [SERVICE, TIMER]
    placeholders = set()
    for path in found:
        text = (ROOT / path).read_text(encoding="utf-8")
        assert scan_text(text, path) == [] and not re.search(r"(^|[\s=])/", text) and not re.search(r"^\s*(User|Group|Environment)=", text, re.M)
        names = set(re.findall(r"@([A-Z][A-Z0-9_]*)@", text))
        values = {name: EXAMPLE[name] for name in names}
        rendered, expected = render_unit(text, values), text
        for name, value in values.items():
            expected = expected.replace(f"@{name}@", value)
        assert "@" not in rendered and rendered == expected and all(value in rendered for value in values.values())
        placeholders |= names
    assert placeholders == set(EXAMPLE)
    service, timer = (sections((ROOT / path).read_text(encoding="utf-8")) for path in found)
    assert list(service) == ["Unit", "Service"] and [line.split("=")[0] for line in service["Unit"]] == ["Description"]
    assert sorted(service["Service"]) == sorted([
        "Type=oneshot", "TimeoutStartSec=50s",
        "ExecStart=@FLIGHTCTL_PYTHON@ @FLIGHTCTL_CHECKOUT@/flightctl/executor_stdio.py --enforce --state @FLIGHTCTL_STATE@ "
        "--host-id @FLIGHTCTL_HOST_ID@ --site-dir @FLIGHTCTL_SITE_DIR@"])
    assert list(timer) == ["Unit", "Timer", "Install"] and [line.split("=")[0] for line in timer["Unit"]] == ["Description"]
    assert sorted(timer["Timer"]) == ["AccuracySec=1s", "OnActiveSec=1s", "OnUnitActiveSec=60s"] and timer["Install"] == ["WantedBy=timers.target"]
    installed = [Path(path).name.removesuffix(".template") for path in found]  # installed names are never transient names
    assert installed == ["flightctl-executor.service", "flightctl-executor.timer"]
    assert not any(pattern.fullmatch(name) for name in [*installed, f"{UNIT}.service", f"{UNIT}.timer"] for pattern in (WORKLOAD, INHIBITOR))

    one, good = "ExecStart=@FLIGHTCTL_PYTHON@ --enforce\n", {"FLIGHTCTL_PYTHON": "/a"}
    assert render_unit(one, good) == "ExecStart=/a --enforce\n"
    for bad in ({}, {**good, "FLIGHTCTL_STATE": "/b"}, {"FLIGHTCTL_PYTHON": ""}, {"FLIGHTCTL_PYTHON": "/a\nExecStartPre=/b"},
                {"FLIGHTCTL_PYTHON": "/a b"}, {"FLIGHTCTL_PYTHON": "/a%b"}):  # missing, unknown, empty, newline, space, percent
        with pytest.raises(SiteConfigRefused):
            render_unit(one, bad)


def test_render_unit_rules():
    from flightctl.siteconfig import SiteConfigRefused, render_unit

    for template, values in ((b"@A@", {"A": "x"}), ("@A@", [("A", "x")]), ("@A@", None), (None, {}), (5, 5)):
        with pytest.raises(TypeError):  # before anything else, and not SiteConfigRefused
            render_unit(template, values)
    plain = "a@b @lower@ @ @Ab@ @1X@ @A-B@ @_X@ 100% $HOME\n"  # none of this is a placeholder
    assert render_unit(plain, {}) == plain
    values = {"A": "/v", "B_2": "w"}
    assert render_unit("@A@@B_2@ @A@ @@A@@ @A@B@\r\n\t\xe9", values) == "/vw /v @/v@ /vB@\r\n\t\xe9" and values == {"A": "/v", "B_2": "w"}
    every = string.ascii_letters + string.digits + "._/+:=,-"
    assert render_unit("@A@", {"A": every}) == every and render_unit("@A@", {"A": "x" * 1024}) == "x" * 1024
    for bad in ("x" * 1025, *(f"/a{chr(code)}" for code in range(0x100) if chr(code) not in every), None, 5, True, b"x", ["x"]):
        with pytest.raises(SiteConfigRefused):
            render_unit("@A@", {"A": bad})
    for template, values in (("none", {"A": "x"}), ("@A@", {"A": "x", 1: "y"}), ("@A@", {"a": "x"}), ("@A@ @B@", {"A": "x"})):
        with pytest.raises(SiteConfigRefused):
            render_unit(template, values)


def run(*argv, ok=True):
    done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 or not ok, f"{argv}: exit {done.returncode}: {done.stderr}"
    return done.stdout


def show(unit, *properties):
    out = run("systemctl", "--user", "show", unit, "--property=" + ",".join(properties))
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


@pytest.mark.realtime
@pytest.mark.onlab
@pytest.mark.skipif(os.environ.get("FLIGHTCTL_CONFORMANCE_TARGET") != "host-local",
                    reason="on-lab: needs FLIGHTCTL_CONFORMANCE_TARGET=host-local (this host's systemd user manager)")
def test_timer_unit_runs_one_shot(tmp_path):
    from flightctl.siteconfig import render_unit

    site, state = tmp_path / "site", tmp_path / "state.json"
    site.mkdir()
    inventory = (ROOT / "config" / "inventory-v2.json.example").read_bytes()
    (site / "inventory.json").write_bytes(inventory)
    (site / "SHA256SUMS").write_text(f"{hashlib.sha256(inventory).hexdigest()}  inventory.json\n", encoding="utf-8")
    values = {"FLIGHTCTL_PYTHON": sys.executable, "FLIGHTCTL_CHECKOUT": str(ROOT), "FLIGHTCTL_STATE": str(state),
              "FLIGHTCTL_HOST_ID": "host-1", "FLIGHTCTL_SITE_DIR": str(site)}
    service = sections(render_unit((ROOT / SERVICE).read_text(encoding="utf-8"), values))
    timer = sections(render_unit((ROOT / TIMER).read_text(encoding="utf-8"), {}))
    [exec_start] = [line.split("=", 1)[1] for line in service["Service"] if line.startswith("ExecStart=")]
    command = ["systemd-run", "--user", f"--unit={UNIT}", *(f"--timer-property={line}" for line in timer["Timer"]),
               *(f"--property={line}" for line in service["Service"] if not line.startswith("ExecStart=")), "--", *exec_start.split()]
    lock = Path(f"{state}.lock")
    try:
        run(*command)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            active = show(f"{UNIT}.service", "ActiveState")["ActiveState"]
            if active == "failed" or (active == "inactive" and lock.exists()):  # a finished run may already be unloaded
                break
            time.sleep(0.5)
        assert active == "inactive", f"the service did not finish within 30 s: {active}"
        assert show(f"{UNIT}.service", "Result", "ExecMainStatus") == {"Result": "success", "ExecMainStatus": "0"}
        assert show(f"{UNIT}.timer", "ActiveState", "SubState") == {"ActiveState": "active", "SubState": "waiting"}
        assert lock.exists()  # the real enforcer ran: it takes its state lock first
    finally:
        for unit in (f"{UNIT}.timer", f"{UNIT}.service"):
            run("systemctl", "--user", "stop", unit, ok=False)
            run("systemctl", "--user", "reset-failed", unit, ok=False)
    assert UNIT not in run("systemctl", "--user", "list-units", "--all", "--plain", "--no-legend")
