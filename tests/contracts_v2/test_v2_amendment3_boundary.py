"""Amendment 3 rev 2 (Sol 6.1 amd3 P1-3, P1-4, P1-5): the session boundary C9w pre-wires in R1, as reference oracles and
schema checks: friend key enrolment and fingerprint resolution, the executor session lifecycle, session-to-parent
attribution, verified seam loading, C9-package absence and stale removal, and the operator rules installed in R1."""

import base64
import copy
import hashlib
import inspect
import json
import os
import struct
from pathlib import Path

import pytest

from .validation import (C9_BODY_STATES, C9_LOADER_STATES, C9_PACKAGES, C9_WINDOW_MAX_TTL_S, CLEAR_PATH, PROBE_PATH, assert_invalid,
                         assert_valid, c9_absent_audit, c9_body_allowed, c9_loader_state, c9_seam_load, c9_window_close, c9_window_open,
                         c9_window_read, c9_window_sweep, errors, examples, executor_session_closes, helper_claim, helper_release, no_clear_route_audit,
                         operator_sudo_audit, parse_session_key, release_stale_removals, resolve_session_key, session_attribution,
                         session_key_enrol, session_key_list, session_key_revoke)

ROOT = Path(__file__).parents[2]
SLICES = (ROOT / "docs/v2/SLICES.md").read_text(encoding="utf-8")


def _blob(key_type: str, body: bytes) -> bytes:
    def s(b: bytes) -> bytes:
        return struct.pack(">I", len(b)) + b
    return s(key_type.encode()) + s(body)


ED_BLOB = _blob("ssh-ed25519", hashlib.sha256(b"flightctl-example-key").digest())
ED = "ssh-ed25519 " + base64.b64encode(ED_BLOB).decode()
ED_FPR = "SHA256:" + base64.b64encode(hashlib.sha256(ED_BLOB).digest()).decode().rstrip("=")
ED2 = "ssh-ed25519 " + base64.b64encode(_blob("ssh-ed25519", hashlib.sha256(b"second").digest())).decode()
NOW = "2026-10-02T10:00:00Z"


# ---------------------------------------------------------------- P1-4 key enrolment and resolution
def test_parse_session_key_canonicalises_bare_keys() -> None:
    key, problems = parse_session_key(ED + " friend@laptop")
    assert problems == [] and key == {"key_type": "ssh-ed25519", "public_key": ED, "key_fingerprint": ED_FPR}
    assert key["key_fingerprint"] == "SHA256:LA11xymOOE7XYYMCs390mZIYwTCX7SIWRqpUwai0uMY"  # the schema examples' fingerprint
    assert parse_session_key(ED)[0] == key


@pytest.mark.parametrize("label,text", [
    ("options before the type", 'command="/bin/sh" ' + ED),
    ("restrict option", "restrict " + ED),
    ("embedded newline (a second authorized_keys line)", ED + "\n" + ED2),
    ("trailing newline", ED + "\n"),
    ("carriage return", ED + "\r"),
    ("newline inside the comment (a second key line)", ED + " a\n" + ED2),
    ("DEL character", ED + " a\x7f"),
    ("NUL", ED + "\x00"),
    ("unknown type", "ssh-rsa " + ED.split(" ")[1]),
    ("well-formed key of a type not on the list", "ssh-rsa " + base64.b64encode(_blob("ssh-rsa", b"\x01\x00\x01" + b"\x00" * 64)).decode()),
    ("declared type differs from the blob (no length rule applies)", "ecdsa-sha2-nistp256 " + ED.split(" ")[1]),
    ("bad base64", "ssh-ed25519 AAAA$$$$"),
    ("blob type differs", "ssh-ed25519 " + base64.b64encode(_blob("ssh-dss", b"x" * 32)).decode()),
    ("short ed25519 blob", "ssh-ed25519 " + base64.b64encode(_blob("ssh-ed25519", b"x" * 31)).decode()),
    ("empty comment field", ED + "  x"),
    ("over-long comment", ED + " " + "c" * 101),
])
def test_parse_session_key_refuses(label, text) -> None:
    assert parse_session_key(text)[0] is None, label
    assert errors(text, "session", "public_key_text") or label in {"blob type differs", "short ed25519 blob", "bad base64",
                                                               "declared type differs from the blob (no length rule applies)"}, label


def test_session_key_enrol_refused_while_off_list_and_revoke_work() -> None:
    reg: dict = {}
    off = session_key_enrol(reg, "frienda", "not even a key", friend_sessions_on=False, now=NOW)
    assert off["ok"] is False and off["error"]["code"] == "unavailable" and reg == {}  # checked before parsing; no state
    on = session_key_enrol(reg, "frienda", ED + " laptop", friend_sessions_on=True, now=NOW, label="laptop")
    assert on["ok"] and on["key"]["public_key"] == ED
    assert_valid(on["key"], "session", "enrolled_key")
    assert session_key_list(reg, "frienda") and session_key_list(reg, "friendb") == [] and session_key_list(reg, "opr", operator=True)
    clash = session_key_enrol(reg, "friendb", ED, friend_sessions_on=True, now=NOW)
    assert clash["error"]["code"] == "conflict"
    assert session_key_revoke(reg, "friendb", ED_FPR, now=NOW)["error"]["code"] == "not_found"  # no existence oracle
    assert session_key_revoke(reg, "frienda", ED_FPR, now=NOW)["ok"]  # works while off too: it is cleanup
    assert reg[ED_FPR]["revoked_at"] == NOW


def test_session_open_resolves_only_own_unrevoked_key() -> None:
    reg: dict = {}
    session_key_enrol(reg, "frienda", ED, friend_sessions_on=True, now=NOW)
    assert resolve_session_key(reg, "frienda", ED_FPR) == ED
    assert resolve_session_key(reg, "friendb", ED_FPR) is None
    assert resolve_session_key(reg, "frienda", "SHA256:" + "B" * 43) is None
    session_key_revoke(reg, "opr", ED_FPR, now=NOW, operator=True)
    assert resolve_session_key(reg, "frienda", ED_FPR) is None


def test_key_ops_are_contracted_and_owned_by_c9w() -> None:
    ops = json.loads((ROOT / "contracts/v2/rpc-ops.schema.json").read_text(encoding="utf-8"))
    rows = {r[0]: r for r in ops["x-ops"][1:]}
    for op in ("session-key-enrol", "session-key-list", "session-key-revoke"):
        assert op in ops["$defs"]["op_name"]["enum"] and rows[op][3] == "session" and rows[op][7].startswith("session key ")
    assert rows["session-key-list"][4] is False and rows["session-key-enrol"][4] is True
    from .test_v2_amendment2 import op_owners
    owners = op_owners(SLICES)
    assert {owners[o] for o in ("session-key-enrol", "session-key-list", "session-key-revoke")} == {"C9w"}
    enrol = next(e for e in examples("rpc-ops")["valid"] if e["op"] == "session-key-enrol")
    assert_valid(enrol, "rpc-ops")
    for bad in ('command="/bin/sh" ' + ED, ED + "\n" + ED2, ED + "\n"):
        assert_invalid(dict(enrol, args={"public_key": bad}), "rpc-ops")
    assert_invalid(dict(enrol, op="session-key-revoke", args={"key_fingerprint": "abc"}), "rpc-ops")
    assert_valid(dict(enrol, op="session-key-revoke", args={"key_fingerprint": ED_FPR}), "rpc-ops")


# ---------------------------------------------------------------- P1-4 executor session lifecycle
def _session_examples():
    valid = [e for e in examples("executor")["valid"] if e.get("kind") == "session"]
    request = next(e for e in valid if "action" in e)
    reply = next(e for e in valid if "action" not in e)
    return request, reply


def test_executor_session_kind_schema() -> None:
    request, reply = _session_examples()
    assert_valid(request, "executor") and assert_valid(reply, "executor")
    assert reply["ok"] is False and reply["definite"] is True and reply["error"]["code"] == "unavailable" and reply["sessions"] == []
    assert_invalid({k: v for k, v in reply.items() if k != "sessions"}, "executor")  # session replies carry the registry
    close = {k: v for k, v in request.items() if k != "session"} | {"action": "close", "session_id": "ses-000001", "reason": "lease-end"}
    assert_valid(close, "executor")
    assert_invalid({k: v for k, v in close.items() if k != "reason"}, "executor")
    assert_invalid(dict(close, reason="local-expiry"), "executor")  # executor-initiated reasons are not requestable
    recon = {k: v for k, v in request.items() if k not in ("session", "identity")} | {
        "action": "reconcile", "lane": request["identity"]["lane"], "active_session_ids": []}
    assert_valid(recon, "executor")
    assert_invalid(dict(recon, identity=request["identity"]), "executor")
    bad_key = copy.deepcopy(request)
    bad_key["session"]["public_key"] = 'command="/bin/sh" ' + ED
    assert_invalid(bad_key, "executor")
    entry = {"session_id": "ses-000001", "parent_lease_id": "lse-0000100", "lane_id": "lane-gpu1", "account": "fc-frienda",
             "slice": "user-1001.slice", "state": "closed", "close_reason": "controller-loss",
             "close_proof": {"user_slice_empty": True, "occupancy_empty": True, "key_removed": True}}
    assert_valid(dict(reply, ok=True, error=None, sessions=[entry]), "executor")
    assert_invalid(dict(reply, sessions=[dict(entry, slice="user-0.slice")]), "executor")
    assert_invalid(dict(reply, sessions=[dict(entry, slice="system.slice")]), "executor")


def test_executor_closes_seeded_sessions_on_local_expiry_controller_loss_and_reconcile() -> None:
    reg = [
        {"session_id": "ses-00000a", "parent_lease_id": "lse-000001", "state": "open", "expires_mono": 100.0},
        {"session_id": "ses-00000b", "parent_lease_id": "lse-000002", "state": "open", "expires_mono": 900.0},
        {"session_id": "ses-00000c", "parent_lease_id": "lse-000003", "state": "open", "expires_mono": 900.0},
        {"session_id": "ses-00000d", "parent_lease_id": "lse-000004", "state": "closed", "expires_mono": 0.0},
        {"session_id": "ses-00000e", "parent_lease_id": "lse-000009", "state": "open", "expires_mono": 900.0},
    ]
    deadlines = {"lse-000001": 500.0, "lse-000002": 150.0, "lse-000003": 500.0}
    assert executor_session_closes(reg, mono_now=200.0, lease_deadline_mono=deadlines) == [
        ("ses-00000a", "local-expiry"), ("ses-00000b", "controller-loss"), ("ses-00000e", "controller-loss")]  # unknown parent: fail closed
    assert executor_session_closes(reg, mono_now=50.0, lease_deadline_mono=deadlines | {"lse-000009": 500.0},
                                   reconcile_active=["ses-00000a", "ses-00000e"]) == [("ses-00000b", "reconcile"), ("ses-00000c", "reconcile")]
    assert executor_session_closes([], mono_now=1e9, lease_deadline_mono={}) == []  # R1: an empty registry is a no-op


# ---------------------------------------------------------------- P1-4 session-to-parent attribution
OPEN = [{"session_id": "ses-00000a", "parent_lease_id": "lse-000001", "lane_id": "lane-gpu1", "slice": "user-1001.slice", "state": "open"},
        {"session_id": "ses-00000b", "parent_lease_id": "lse-000002", "lane_id": "lane-gpu1", "slice": "user-1002.slice", "state": "closed"}]


@pytest.mark.parametrize("label,cgroup,lane,expect", [
    ("CUDA child in the registered slice", "/user.slice/user-1001.slice/session-4.scope", "lane-gpu1", "lse-000001"),
    ("nohup child deeper in the slice", "/user.slice/user-1001.slice/user@1001.service/app.slice/x.scope", "lane-gpu1", "lse-000001"),
    ("same uid in a system unit (never by uid)", "/system.slice/cron.service", "lane-gpu1", None),
    ("prefix trick: another uid", "/user.slice/user-10011.slice/session-1.scope", "lane-gpu1", None),
    ("slice of a closed entry", "/user.slice/user-1002.slice/session-2.scope", "lane-gpu1", None),
    ("other lane", "/user.slice/user-1001.slice/session-4.scope", "lane-gpu2", None),
    ("dot-dot path", "/user.slice/user-1001.slice/../user-1003.slice/s.scope", "lane-gpu1", None),
    ("relative path", "user.slice/user-1001.slice/s.scope", "lane-gpu1", None),
])
def test_session_attribution_by_registered_slice_never_by_uid(label, cgroup, lane, expect) -> None:
    assert session_attribution(cgroup, OPEN, lane) == expect, label


def test_session_attribution_has_no_uid_input_and_refuses_ambiguity() -> None:
    assert "uid" not in inspect.signature(session_attribution).parameters
    twin = dict(OPEN[0], session_id="ses-00000z", parent_lease_id="lse-000099")
    assert session_attribution("/user.slice/user-1001.slice/s.scope", [OPEN[0], twin], "lane-gpu1") is None
    assert session_attribution("/user.slice/user-1001.slice/s.scope", [], "lane-gpu1") is None  # R1: nothing registered
    probe = json.loads((ROOT / "contracts/v2/gpu-probe.schema.json").read_text(encoding="utf-8"))
    assert "never used" in json.dumps(probe) and "session_attribution" in json.dumps(probe)


# ---------------------------------------------------------------- P1-5 verified seam loading (rev 3: three states, trust boundary)
ALL_HOOKS = ("session_open_body", "friend_unit_start_body", "claim_create_body", "claim_clear_body", "session_probe_body", "binding_measure")
# The reviewed state table (rev 3), written out independently of c9_loader.C9_BODY_STATES: True = may load.
EXPECTED_TABLE = {
    "session_open_body": {"off": False, "window": False, "on": True},
    "friend_unit_start_body": {"off": False, "window": False, "on": True},
    "claim_create_body": {"off": False, "window": False, "on": True},
    "claim_clear_body": {"off": False, "window": False, "on": True},
    "session_probe_body": {"off": False, "window": True, "on": False},
    "binding_measure": {"off": False, "window": True, "on": True},
}
C9_PKG_SRC = "".join(f"def {h}(*a, **k):\n    return '{h}'\n" for h in ALL_HOOKS)


def _tree(tmp_path: Path, pkg_src: str = C9_PKG_SRC, *, loader: bool = False) -> tuple[Path, dict[str, str]]:
    prefix = tmp_path / "prefix"
    files = {"flightctl/__init__.py": "", "flightctl/c9_seams.py": "# seams\n", "flightctl/c9/__init__.py": pkg_src}
    if loader:  # rev 4: the loader lives inside the verified prefix, as in production
        files["flightctl/c9_loader.py"] = Path(__file__).with_name("c9_loader.py").read_text(encoding="utf-8")
    for rel, text in files.items():
        (prefix / rel).parent.mkdir(parents=True, exist_ok=True)
        (prefix / rel).write_text(text, encoding="utf-8")
    for d in [tmp_path, prefix, *[p for p in prefix.rglob("*") if p.is_dir()]]:
        os.chmod(d, 0o755)
    for p in prefix.rglob("*"):
        if p.is_file():
            os.chmod(p, 0o644)
    manifest = {rel: hashlib.sha256(text.encode()).hexdigest() for rel, text in files.items()}
    return prefix, manifest


class Spy:
    def __init__(self, raises: BaseException | None = None, module=None):
        self.calls, self.raises, self.module = 0, raises, module

    def __call__(self):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.module or type("M", (), {h: staticmethod(lambda: None) for h in ALL_HOOKS})


def _load(tmp_path, prefix, manifest, spy, state="on", hooks=("session_open_body",)):
    return c9_seam_load(str(prefix), "flightctl/c9", state=state, manifest=manifest, expected_uid=os.getuid(), hooks=hooks,
                        trust_root=str(tmp_path), importer=spy, modules={})  # in-process: pytest itself is an outside module


@pytest.mark.parametrize("body", ALL_HOOKS)
@pytest.mark.parametrize("state", C9_LOADER_STATES)
def test_c9_body_state_table_every_cell(tmp_path: Path, body, state) -> None:
    """Every cell of the rev-3 table: the oracle table equals the reviewed one, and the loader returns the hook exactly
    when the cell allows it (refusing default otherwise; nothing is read or imported when no requested body may load)."""
    assert set(C9_BODY_STATES) == set(EXPECTED_TABLE) and c9_body_allowed(body, state) is EXPECTED_TABLE[body][state]
    prefix, manifest = _tree(tmp_path)
    spy = Spy()
    out = _load(tmp_path, prefix, manifest, spy, state=state, hooks=(body,))
    if EXPECTED_TABLE[body][state]:
        assert out["loaded"] and set(out["hooks"]) == {body} and spy.calls == 1
    else:
        assert not out["loaded"] and out["hooks"] is None and spy.calls == 0


def test_loader_returns_only_the_hooks_of_its_state(tmp_path: Path) -> None:
    prefix, manifest = _tree(tmp_path)
    for state in C9_LOADER_STATES:
        out = _load(tmp_path, prefix, manifest, Spy(), state=state, hooks=ALL_HOOKS)
        allowed = {h for h in ALL_HOOKS if EXPECTED_TABLE[h][state]}
        assert (set(out["hooks"]) if out["loaded"] else set()) == allowed, state
    assert not _load(tmp_path, prefix, manifest, Spy(), state="maybe")["loaded"]


WINDOW_CFG = {"operator_account": "opr", "probe_account": "fc-probe", "friend_sessions": False, "friend_sessions_global": False,
              "friend_sessions_host": False}
OPR = {"program": PROBE_PATH, "ruid": 0, "euid": 0, "sudo_user": "opr", "sudo_uid": 1500}


def _open(state_dir, cfg=WINDOW_CFG, inv=OPR, ttl=1800, now=1000.0, mono=50.0, boot="boot-a", audit=None):
    return c9_window_open(str(state_dir), cfg, inv, 1500, host_id="host-1", ttl_s=ttl, now=now, mono_now=mono, boot_id=boot,
                          audit=audit if audit is not None else [])


def test_enablement_window_open_rules(tmp_path: Path) -> None:
    audit: list = []
    for bad_cfg in ({**WINDOW_CFG, "friend_sessions": True}, {**WINDOW_CFG, "friend_sessions_global": True}, {**WINDOW_CFG, "friend_sessions_host": True}):
        assert not _open(tmp_path, cfg=bad_cfg, audit=audit)  # any friend flag on: the window precedes enablement
    for bad_inv in ({**OPR, "sudo_user": "runner"}, {**OPR, "sudo_uid": 1}, {**OPR, "euid": 1500}, {**OPR, "program": "/usr/local/libexec/flightctl-helper"}):
        assert not _open(tmp_path, inv=bad_inv, audit=audit)
    for ttl in (0, -1, C9_WINDOW_MAX_TTL_S + 1):
        assert not _open(tmp_path, ttl=ttl, audit=audit)
    assert _open(tmp_path, audit=audit)
    assert not _open(tmp_path, audit=audit), "no extension while a window is valid"
    assert c9_window_close(str(tmp_path), reason="success", now=1100.0, audit=audit, caller="opr")
    assert not c9_window_close(str(tmp_path), reason="failure", now=1101.0, audit=audit, caller="opr")  # idempotent
    assert _open(tmp_path, now=1200.0, mono=60.0, audit=audit)
    assert len(audit) == 15 and all("event" in a for a in audit)  # every call is an audit event
    with pytest.raises(ValueError):
        c9_window_close(str(tmp_path), reason="whatever", now=0.0, audit=audit, caller=None)


@pytest.mark.parametrize("label,kw,expect", [
    ("all flags off, no window", {"window": False}, "off"),
    ("all flags off, valid window", {}, "window"),
    ("window expired by wall clock", {"now": 1000.0 + 1800}, "off"),
    ("wall clock rolled back, monotonic expired", {"now": 900.0, "mono": 50.0 + 1800}, "off"),
    ("after a reboot (new boot id)", {"boot": "boot-b"}, "off"),
    ("record of another host", {"host": "host-2"}, "off"),
    ("all three flags on (window ignored)", {"cfg": {**WINDOW_CFG, "friend_sessions": True, "friend_sessions_global": True, "friend_sessions_host": True}}, "on"),
    ("mixed flags with a window: fail closed", {"cfg": {**WINDOW_CFG, "friend_sessions_host": True}}, "off"),
    ("mixed flags, global on only", {"cfg": {**WINDOW_CFG, "friend_sessions_global": True}}, "off"),
    ("corrupt window record", {"corrupt": True}, "off"),
])
def test_loader_state_from_flags_and_window(tmp_path: Path, label, kw, expect) -> None:
    if kw.get("window", True):
        assert _open(tmp_path)
    if kw.get("corrupt"):
        (tmp_path / "c9-enablement-window.json").write_text("{not json", encoding="utf-8")
    state = c9_loader_state(kw.get("cfg", WINDOW_CFG), c9_window_read(str(tmp_path)), host_id=kw.get("host", "host-1"),
                            now=kw.get("now", 1100.0), mono_now=kw.get("mono", 100.0), boot_id=kw.get("boot", "boot-a"))
    assert state == expect, label


def test_probe_add_has_exactly_one_working_state_the_window(tmp_path: Path) -> None:
    """Sol 6.1 amd3r2 P1, reproduced and closed: the probe ADD succeeds only when its body may load AND its own policy
    (refused while friend_sessions is on) admits it. Exactly one state does both: the enablement window."""
    from .validation import session_probe
    working = []
    for state in C9_LOADER_STATES:
        keys = tmp_path / state
        keys.mkdir()
        cfg = {**WINDOW_CFG, "friend_sessions": state == "on", "friend_sessions_global": state == "on", "friend_sessions_host": state == "on"}
        body_loads = c9_body_allowed("session_probe_body", state)
        policy_ok = session_probe(str(keys), cfg, OPR, 1500, account="fc-probe", pubkey=ED, ttl_s=600, now=1000.0, audit=[],
                                  mono_now=10.0, boot_id="boot-a")
        if body_loads and policy_ok:
            working.append(state)
    assert working == ["window"]


def test_c9w_table_text_matches_the_state_table() -> None:
    for body in ("session_open_body", "friend_unit_start_body", "claim_create_body", "claim_clear_body"):
        assert not c9_body_allowed(body, "window"), body
    c9w = SLICES.split("\n### C9w:", 1)[1].split("\n### ", 1)[0]
    table = c9w.split("| Body | off | window | on |", 1)[1].split("\n\n", 1)[0]
    for body, cells in EXPECTED_TABLE.items():
        row = next(l for l in table.splitlines() if f"`{body}`" in l)
        assert [c.strip() for c in row.strip().strip("|").split("|")[1:4]] == ["allowed" if cells[s] else "refused" for s in C9_LOADER_STATES], body


def _runbook_steps() -> dict[int, str]:
    c9 = SLICES.split("\n### C9:", 1)[1].split("\n### ", 1)[0]
    return {n: c9.split(f"\n  {n}. ", 1)[1].split(f"\n  {n + 1}. ", 1)[0] if n < 8 else c9.split("\n  8. ", 1)[1].split("\n- ", 1)[0]
            for n in (5, 7, 8)}


PROBE_CMDS = {"OPEN": "--window-open --ttl 3600", "ADD": "--account <probe_account> --pubkey-file <test key> --ttl 600",
              "REVOKE": "--sweep --all", "ROLLBACK": "--sweep --all --close-window", "EXPIRED": "--sweep --expired",
              "MEASURE": "--measure", "CLOSE_OK": "--window-close --result success", "CLOSE_FAIL": "--window-close --result failure"}


class _Host:
    """The runbook's pre-enablement steps on ONE host, carried through the actual oracles: c9_window_open/sweep/close,
    c9_loader_state, c9_body_allowed, session_probe and probe_sweep. Each command returns whether it succeeded."""

    def __init__(self, root: Path):
        from .validation import probe_sweep, session_probe
        self.session_probe, self.probe_sweep = session_probe, probe_sweep
        self.state_dir, self.keys = root / "state", root / "keys"
        self.state_dir.mkdir()
        self.keys.mkdir()
        self.cfg = {**WINDOW_CFG}
        self.now, self.mono, self.boot, self.audit = 1000.0, 50.0, "boot-a", []

    def state(self) -> str:
        return c9_loader_state(self.cfg, c9_window_read(str(self.state_dir)), host_id="host-1", now=self.now, mono_now=self.mono, boot_id=self.boot)

    def wait(self, s: float) -> None:
        self.now += s
        self.mono += s

    def reboot(self) -> None:
        self.boot, self.mono = "boot-b", 5.0

    def run(self, cmd: str) -> bool:
        root = {"program": PROBE_PATH, "ruid": 0, "euid": 0, "sudo_user": "opr", "sudo_uid": 1500}
        kw = dict(host_id="host-1", now=self.now, mono_now=self.mono, boot_id=self.boot, audit=self.audit)
        if cmd == PROBE_CMDS["OPEN"]:
            return _open(self.state_dir, cfg=self.cfg, now=self.now, mono=self.mono, boot=self.boot, audit=self.audit)
        if cmd == PROBE_CMDS["ADD"]:
            return c9_body_allowed("session_probe_body", self.state()) and self.session_probe(
                str(self.keys), self.cfg, root, 1500, account="fc-probe", pubkey=ED, ttl_s=600, now=self.now, audit=self.audit,
                mono_now=self.mono, boot_id=self.boot)
        if cmd in (PROBE_CMDS["REVOKE"], PROBE_CMDS["ROLLBACK"], PROBE_CMDS["EXPIRED"]):
            mode = "expired" if cmd == PROBE_CMDS["EXPIRED"] else "all"
            removed = self.probe_sweep(str(self.keys), self.cfg, root, now=self.now, audit=self.audit, mode=mode, mono_now=self.mono, boot_id=self.boot)
            closed = c9_window_sweep(str(self.state_dir), close_window=cmd == PROBE_CMDS["ROLLBACK"], **kw)
            return removed or closed
        if cmd == PROBE_CMDS["MEASURE"]:
            return c9_body_allowed("binding_measure", self.state())
        if cmd in (PROBE_CMDS["CLOSE_OK"], PROBE_CMDS["CLOSE_FAIL"]):
            return c9_window_close(str(self.state_dir), reason=cmd.rsplit(" ", 1)[1], now=self.now, audit=self.audit, caller="opr")
        raise AssertionError(f"unmapped runbook command {cmd}")

    def closed_and_off(self) -> bool:
        return (not (self.state_dir / "c9-enablement-window.json").exists() and self.state() == "off"
                and not any(self.cfg[k] for k in ("friend_sessions", "friend_sessions_global", "friend_sessions_host"))
                and not (self.keys / "fc-probe").exists())


def test_runbook_commands_are_all_mapped_and_in_their_steps() -> None:
    """Every session-probe command the runbook names is one the sequence model executes, and each command the scenarios
    use appears literally in its step (so the text and the sequence test cannot drift apart)."""
    import re
    steps = _runbook_steps()
    named = {c for n in steps for c in re.findall(r"flightctl-session-probe (--[^`]*)`", steps[n])}
    assert named == set(PROBE_CMDS.values()) - {PROBE_CMDS["EXPIRED"]}, named ^ set(PROBE_CMDS.values())
    assert f"`--sweep --expired`" in steps[5]
    where = {"OPEN": (7, 8), "ADD": (7,), "REVOKE": (7,), "ROLLBACK": (5,), "MEASURE": (8,), "CLOSE_OK": (8,), "CLOSE_FAIL": (7, 8)}
    for key, nums in where.items():
        for n in nums:
            assert PROBE_CMDS[key] in steps[n], (key, n)
    assert steps[7].index(PROBE_CMDS["OPEN"]) < steps[7].index(PROBE_CMDS["ADD"]) < steps[7].index(PROBE_CMDS["REVOKE"] + "`")
    assert steps[8].index(PROBE_CMDS["MEASURE"] + "`") < steps[8].index(PROBE_CMDS["CLOSE_OK"]) < steps[8].index("(d) write")


def test_runbook_sequence_success_path(tmp_path: Path) -> None:
    """Sol 6.1 amd3r3 P1, reproduced and closed: the window state is CARRIED from step 7 through step 8(a). The step-7
    revoke leaves the window open, the measurement is admitted, the window closes before any flag is written, and only
    then do the flags go on."""
    h = _Host(tmp_path)
    for cmd in ("OPEN", "ADD"):
        assert h.run(PROBE_CMDS[cmd]) and h.state() == "window", cmd
    assert h.run(PROBE_CMDS["REVOKE"]) and not (h.keys / "fc-probe").exists() and h.state() == "window"  # key gone, window open
    assert h.run(PROBE_CMDS["MEASURE"]), "step 8(a) measurement must be admitted after the step-7 revoke"
    assert h.run(PROBE_CMDS["CLOSE_OK"]) and h.closed_and_off()  # closed before step 8(d) writes any flag
    h.cfg.update(friend_sessions=True, friend_sessions_global=True, friend_sessions_host=True)  # 8(d)
    assert h.state() == "on" and not h.run(PROBE_CMDS["OPEN"]) and not h.run(PROBE_CMDS["ADD"])


@pytest.mark.parametrize("path", ["login fails, operator closes, timer fires", "operator gone, timer fires",
                                  "window expires before 8(a), abandoned", "host reboots before 8(a), abandoned",
                                  "measurement fails", "window expires before 8(a), reopened"])
def test_runbook_sequence_failure_paths_end_closed_and_off(tmp_path: Path, path) -> None:
    h = _Host(tmp_path)
    assert h.run(PROBE_CMDS["OPEN"]) and h.run(PROBE_CMDS["ADD"])
    if path == "login fails, operator closes, timer fires":
        assert h.run(PROBE_CMDS["CLOSE_FAIL"]) and h.state() == "off"
        assert not h.run(PROBE_CMDS["MEASURE"])
        h.wait(600)
        assert h.run(PROBE_CMDS["ROLLBACK"])  # removes the probe key
    elif path == "operator gone, timer fires":
        h.wait(600)
        assert h.run(PROBE_CMDS["ROLLBACK"])
    elif path.startswith(("window expires", "host reboots")):
        assert h.run(PROBE_CMDS["REVOKE"]) and h.state() == "window"
        if path.startswith("window expires"):
            h.wait(3601)
        else:
            h.reboot()
        assert h.state() == "off" and not h.run(PROBE_CMDS["MEASURE"])
        assert h.run(PROBE_CMDS["EXPIRED"])  # the per-minute sweep closes the no-longer-valid window
        if path.endswith("reopened"):
            assert h.run(PROBE_CMDS["OPEN"]) and h.run(PROBE_CMDS["MEASURE"]) and h.run(PROBE_CMDS["CLOSE_OK"])
    elif path == "measurement fails":
        assert h.run(PROBE_CMDS["REVOKE"]) and h.run(PROBE_CMDS["MEASURE"])
        assert h.run(PROBE_CMDS["CLOSE_FAIL"])
    assert h.closed_and_off(), path
    events = [a["event"] for a in h.audit]
    assert "window-opened" in events and ("window-closed" in events), events


@pytest.mark.parametrize("label", ["stale package", "tampered file", "extra file", "bytecode cache", "symlink", "group-writable",
                                   "missing manifest file", "writable prefix itself", "writable ancestor", "prefix outside the trust root",
                                   "unreadable file (filesystem error)", "unreadable directory (filesystem error)"])
def test_seam_loader_rejects_before_import(tmp_path: Path, label) -> None:
    prefix, manifest = _tree(tmp_path)
    trust = tmp_path
    if label == "stale package":
        manifest = {k: v for k, v in manifest.items() if not k.startswith("flightctl/c9/")}
    elif label == "tampered file":
        (prefix / "flightctl/c9/__init__.py").write_text("def session_open_body():\n    return 'tampered'\n", encoding="utf-8")
    elif label == "extra file":
        (prefix / "flightctl/c9/extra.py").write_text("x = 1\n", encoding="utf-8")
        os.chmod(prefix / "flightctl/c9/extra.py", 0o644)
    elif label == "bytecode cache":
        (prefix / "flightctl/c9/__pycache__").mkdir()
        os.chmod(prefix / "flightctl/c9/__pycache__", 0o755)
        (prefix / "flightctl/c9/__pycache__/x.pyc").write_bytes(b"\0")
        os.chmod(prefix / "flightctl/c9/__pycache__/x.pyc", 0o644)
    elif label == "symlink":
        (prefix / "flightctl/c9/link.py").symlink_to(prefix / "flightctl/c9_seams.py")
    elif label == "group-writable":
        os.chmod(prefix / "flightctl/c9_seams.py", 0o664)
    elif label == "missing manifest file":
        manifest = dict(manifest, **{"flightctl/c9/claims.py": "0" * 64})
    elif label == "writable prefix itself":
        os.chmod(prefix, 0o777)
    elif label == "writable ancestor":
        os.chmod(tmp_path, 0o777)
    elif label == "prefix outside the trust root":
        trust = tmp_path / "elsewhere"
        trust.mkdir()
    elif label == "unreadable file (filesystem error)":
        os.chmod(prefix / "flightctl/c9_seams.py", 0o000)
    elif label == "unreadable directory (filesystem error)":
        os.chmod(prefix / "flightctl/c9", 0o000)
    spy = Spy()
    try:
        out = c9_seam_load(str(prefix), "flightctl/c9", state="on", manifest=manifest, expected_uid=os.getuid(), hooks=("session_open_body",),
                           trust_root=str(trust), importer=spy, modules={})
    finally:
        for p in (prefix / "flightctl/c9", prefix / "flightctl/c9_seams.py"):
            os.chmod(p, 0o755 if p.is_dir() else 0o644)
    assert out["loaded"] is False and spy.calls == 0, (label, out)


def test_seam_loader_cached_module_without_a_package_refuses(tmp_path: Path) -> None:
    """Sol 6.1 amd3r2: NOT an equivalent mutant. No C9 package in the manifest or the tree, but something still resolves
    flightctl.c9 (a cached sys.modules entry, modelled by an importer that returns a module): the loader must refuse
    before importing, and it purges the cached entry."""
    import sys
    import types
    prefix, manifest = _tree(tmp_path)
    os.unlink(prefix / "flightctl/c9/__init__.py")
    os.rmdir(prefix / "flightctl/c9")
    manifest = {k: v for k, v in manifest.items() if not k.startswith("flightctl/c9/")}
    cached = types.ModuleType("flightctl.c9")
    cached.session_open_body = lambda: "cached"
    saved = sys.modules.get("flightctl.c9")
    sys.modules["flightctl.c9"] = cached
    try:
        spy = Spy(module=cached)
        out = _load(tmp_path, prefix, manifest, spy)
        assert out["loaded"] is False and spy.calls == 0 and "flightctl.c9" in out["purged"] and "flightctl.c9" not in sys.modules
    finally:
        sys.modules.pop("flightctl.c9", None)
        if saved is not None:
            sys.modules["flightctl.c9"] = saved


@pytest.mark.parametrize("exc", [ImportError("boom"), SystemExit("exit at import"), KeyboardInterrupt(), RuntimeError("x")])
def test_seam_loader_any_import_time_exception_refuses(tmp_path: Path, exc) -> None:
    prefix, manifest = _tree(tmp_path)
    out = _load(tmp_path, prefix, manifest, Spy(raises=exc))
    assert out["loaded"] is False and out["reason"].startswith("import failed")


def test_seam_loader_missing_hook_refuses_and_cleanup_still_works(tmp_path: Path) -> None:
    prefix, manifest = _tree(tmp_path)
    assert _load(tmp_path, prefix, manifest, Spy(module=type("M", (), {})))["loaded"] is False
    assert _load(tmp_path, prefix, manifest, Spy(module=type("M", (), {"session_open_body": 42})))["loaded"] is False  # not callable
    state = str(tmp_path / "claims")
    os.mkdir(state)
    assert helper_claim(state, "fc-frienda", "lse-0000100")  # seeded claim (as if created before the flags went off)
    proof = {"user_slice_empty": True, "occupancy_empty": True, "key_removed": True}
    assert helper_release(state, "fc-frienda", "lse-0000100", proof)  # cleanup never consults the loader


# ---- the REAL interpreter (measured: python3 -I keeps site and site-packages; -I -S does not)
def _py(flags, code, cwd=None):
    import subprocess
    import sys
    r = subprocess.run([sys.executable, *flags, "-c", code], capture_output=True, text=True, timeout=60, cwd=cwd)
    assert r.returncode == 0, r.stderr[-2000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


PROBE_CODE = ("import json,sys; print(json.dumps({'no_site': sys.flags.no_site, 'site_loaded': 'site' in sys.modules, "
              "'third_party_dirs': [p for p in sys.path if 'site-packages' in p or 'dist-packages' in p]}))")


def test_trusted_startup_flags_measured_on_this_interpreter() -> None:
    strict = _py(["-I", "-S", "-B"], PROBE_CODE)
    assert strict == {"no_site": 1, "site_loaded": False, "third_party_dirs": []}
    loose = _py(["-I", "-B"], PROBE_CODE)
    assert loose["site_loaded"] is True and loose["no_site"] == 0  # -I alone still runs site (Sol 6.1 amd3r2, re-measured)


GUARD_SCRIPT = r'''
import importlib, importlib.util, json, os, sys, sysconfig, types
stdlib = list(sys.path)                                   # under -I -S: exactly the stdlib entries (measured)
a = json.loads(sys.argv[1])
prefix, outside, manifest, trust, case = a["prefix"], a["outside"], a["manifest"], a["trust"], a["case"]
# the loader itself lives INSIDE the verified prefix (production layout), so it passes the pre-load module check
spec = importlib.util.spec_from_file_location("c9_loader", os.path.join(prefix, "flightctl", "c9_loader.py"))
L = importlib.util.module_from_spec(spec); sys.modules["c9_loader"] = L; spec.loader.exec_module(L)
nested = os.path.join(sysconfig.get_paths()["stdlib"], "site-packages")
uid, res = os.getuid(), {}


def load(importer=None, p=prefix):
    out = L.c9_seam_load(p, "flightctl/c9", state="on", manifest=manifest, expected_uid=uid, hooks=["session_open_body"],
                         stdlib_dirs=stdlib, trust_root=trust, importer=importer)
    return {"loaded": out["loaded"], "reason": out["reason"]}


if case == "guard":
    sys.path[:] = [*stdlib, prefix, outside]              # a leaked third-party directory on sys.path
    g = L.C9ImportGuard(stdlib, prefix); sys.meta_path.insert(0, g)
    for name in ("colorsys", "flightctl.c9_seams", "outside_mod"):
        try:
            importlib.import_module(name); res[name] = "imported"
        except ImportError:
            res[name] = "refused"
elif case in ("nested-guard", "nested-load"):
    cand = sorted(n for n in (os.listdir(nested) if os.path.isdir(nested) else [])
                  if os.path.isfile(os.path.join(nested, n, "__init__.py")) and n.isidentifier())
    if not cand:
        res = {"skip": "no package nested under the stdlib directory on this interpreter"}
    elif case == "nested-guard":                          # Sol 6.1 amd3r3 control 1
        sys.path[:] = [*stdlib, prefix, nested]
        g = L.C9ImportGuard(stdlib, prefix); sys.meta_path.insert(0, g)
        try:
            importlib.import_module(cand[0]); res = {"nested": "imported"}
        except ImportError:
            res = {"nested": "refused"}
    else:                                                 # Sol 6.1 amd3r3 control 2: the importer appends it and imports
        def importer():
            sys.path.append(nested)
            importlib.import_module(cand[0])
            return importlib.import_module("flightctl.c9")
        res = load(importer)
elif case == "cached":
    m = types.ModuleType("flightctl.c9"); m.session_open_body = lambda: 1; sys.modules["flightctl.c9"] = m
    res = load(); res["c9_cached_after"] = "flightctl.c9" in sys.modules
elif case == "preloaded-outside":
    sys.path.append(outside); import outside_mod; sys.path.remove(outside)   # an outside module cached BEFORE the guard
    res = load()
elif case == "alias":
    res = load(p=a["alias"]); res["file_canonical"] = sys.modules["flightctl.c9"].__file__.startswith(os.path.realpath(prefix) + os.sep)
elif case == "alias-swapped":
    def importer():
        os.unlink(a["alias"]); os.symlink(a["evil"], a["alias"])   # repoint the alias after verification
        return importlib.import_module("flightctl.c9")
    res = load(importer, p=a["alias"])
    res["evil_body"] = res["loaded"] and sys.modules["flightctl.c9"].session_open_body() == "evil"
elif case == "path":
    seen = {}
    def importer():
        seen["path"] = list(sys.path)
        return importlib.import_module("flightctl.c9")
    sys.path.append(outside)                              # inherited, non-pristine sys.path
    res = load(importer); res["path_exact"] = seen.get("path") == [*stdlib, os.path.realpath(prefix)]
else:
    res = load()
print(json.dumps(res))
'''


def _strict_run(tmp_path: Path, case: str, pkg_src: str | None = C9_PKG_SRC, *, alias: bool = False) -> dict:
    prefix, manifest = _tree(tmp_path, pkg_src or C9_PKG_SRC, loader=True)
    if pkg_src is None:
        os.unlink(prefix / "flightctl/c9/__init__.py")
        os.rmdir(prefix / "flightctl/c9")
        manifest = {k: v for k, v in manifest.items() if not k.startswith("flightctl/c9/")}
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "outside_mod.py").write_text("X = 1\n", encoding="utf-8")
    args = {"prefix": str(prefix), "outside": str(outside), "manifest": manifest, "trust": str(tmp_path), "case": case}
    if alias:
        evil = tmp_path / "evil"
        (evil / "flightctl/c9").mkdir(parents=True)
        (evil / "flightctl/__init__.py").write_text("", encoding="utf-8")
        (evil / "flightctl/c9/__init__.py").write_text("def session_open_body():\n    return 'evil'\n", encoding="utf-8")
        args.update(alias=str(tmp_path / "alias"), evil=str(evil))
        os.symlink(prefix, tmp_path / "alias")
    script = tmp_path / "run.py"
    script.write_text(GUARD_SCRIPT, encoding="utf-8")
    got = _py(["-I", "-S", "-B"], f"import sys; sys.argv = ['run', {json.dumps(args)!r}]; exec(open({str(script)!r}).read())")
    assert not list(prefix.rglob("__pycache__")), "-B: no bytecode written into the verified prefix"
    return got


@pytest.mark.parametrize("case,pkg_src,expect", [
    ("happy: the body imports only stdlib", "import colorsys\n" + C9_PKG_SRC, {"loaded": True}),
    ("the body imports a module outside stdlib and the prefix", "import outside_mod\n" + C9_PKG_SRC, {"loaded": False}),
    ("SystemExit at import time", "raise SystemExit('bye')\n", {"loaded": False}),
    ("the body puts a directory on sys.path itself and imports from it",
     "import sys\nsys.path.append(__import__('os').path.join(__import__('os').path.dirname(__file__), '..', '..', '..', 'outside'))\nimport outside_mod\n" + C9_PKG_SRC,
     {"loaded": False}),
    ("cached flightctl.c9 but no package installed", None, {"loaded": False, "c9_cached_after": False}),
    ("guard alone, with a leaked directory on sys.path", C9_PKG_SRC, {"colorsys": "imported", "flightctl.c9_seams": "imported", "outside_mod": "refused"}),
    ("preloaded-outside: an outside module cached before the guard", C9_PKG_SRC, {"loaded": False}),
    ("path: inherited sys.path is replaced by stdlib + canonical prefix", C9_PKG_SRC, {"loaded": True, "path_exact": True}),
])
def test_import_boundary_under_the_real_trusted_interpreter(tmp_path: Path, case, pkg_src, expect) -> None:
    mode = ("guard" if case.startswith("guard") else "cached" if pkg_src is None else case.split(":")[0]
            if case.startswith(("preloaded-outside", "path")) else "load")
    got = _strict_run(tmp_path, mode, pkg_src)
    assert {k: got[k] for k in expect} == expect, (case, got)


@pytest.mark.parametrize("case", ["nested-guard", "nested-load"])
def test_nested_site_packages_refused_under_the_real_interpreter(tmp_path: Path, case) -> None:
    """Sol 6.1 amd3r3 P1, both real-interpreter controls: on this host the interpreter's site-packages is NESTED inside
    its stdlib directory (measured: /usr/lib/python3.14/site-packages). A package there is refused by the guard, and a
    loader whose importer appends that directory and imports from it refuses."""
    got = _strict_run(tmp_path, case)
    if "skip" in got:
        pytest.skip(got["skip"])
    assert got == ({"nested": "refused"} if case == "nested-guard" else {"loaded": False, "reason": "import failed: ImportError"}), got


def test_canonical_prefix_alias_is_bound_and_swap_proof(tmp_path: Path) -> None:
    """Sol 6.1 amd3r3: imports use the canonical, verified prefix, never the alias spelling; repointing the alias after
    verification cannot substitute another tree."""
    got = _strict_run(tmp_path, "alias", alias=True)
    assert got["loaded"] is True and got["file_canonical"] is True, got
    swapped = _strict_run(tmp_path / "s", "alias-swapped", alias=True) if (tmp_path / "s").mkdir() is None else None
    assert swapped["loaded"] is True and swapped["evil_body"] is False, swapped


def test_stdlib_trust_is_exact(tmp_path: Path) -> None:
    from .c9_loader import C9ImportGuard, stdlib_trusted
    std = tmp_path / "std"
    for rel in ("mod.py", "pkg/__init__.py", "site-packages/third/__init__.py", "pkg/dist-packages/x.py", "vendored/v.py"):
        (std / rel).parent.mkdir(parents=True, exist_ok=True)
        (std / rel).write_text("", encoding="utf-8")
    assert stdlib_trusted(str(std / "mod.py"), [str(std)]) and stdlib_trusted(str(std / "pkg/__init__.py"), [str(std)])
    assert not stdlib_trusted(str(std / "site-packages/third/__init__.py"), [str(std)])
    assert not stdlib_trusted(str(std / "pkg/dist-packages/x.py"), [str(std)])
    assert not stdlib_trusted(str(std / "vendored/v.py"), [str(std)], excluded=[str(std / "vendored")])
    assert not stdlib_trusted(str(tmp_path / "elsewhere.py"), [str(std)])
    g = C9ImportGuard([str(std)], str(tmp_path / "prefix"), excluded=[])
    assert g.trusted(str(std / "mod.py")) and not g.trusted(str(std / "site-packages/third/__init__.py"))


def test_third_party_locations_are_the_interpreter_purelib_and_platlib() -> None:
    import sysconfig
    from .c9_loader import third_party_locations
    paths = sysconfig.get_paths()
    assert set(third_party_locations()) == {paths["purelib"], paths["platlib"]}


def test_c9_absent_audit_and_stale_removal() -> None:
    r1 = ["flightctl/authority.py", "flightctl/c9_seams.py", "helper/flightctl-helper", "helper/c9_seams.py"]
    assert c9_absent_audit(r1) == []
    for planted in ("flightctl/c9/__init__.py", "helper/c9/claims.py", "helper/c9"):
        assert c9_absent_audit(r1 + [planted]), planted
    assert release_stale_removals(r1 + ["helper/c9/claims.py"], r1) == ["helper/c9/claims.py"]
    assert release_stale_removals(r1, r1) == []
    assert C9_PACKAGES == ("flightctl/c9/", "helper/c9/")


# ---------------------------------------------------------------- P1-3 operator rules installed in R1 against stubs
@pytest.mark.parametrize("program,example", [(CLEAR_PATH, "config/flightctl-claim-clear-sudoers.example"),
                                             (PROBE_PATH, "config/flightctl-session-probe-sudoers.example")])
def test_r1_installs_both_operator_rules_and_the_audit_passes(program, example) -> None:
    text = (ROOT / example).read_text(encoding="utf-8")
    assert "only when enabling C9" not in text and "R1" in text
    sudo_l = ("Matching Defaults entries for opr on h:\n    env_reset, !setenv\n\n"
              f"Runas and Command-specific defaults for opr:\n    Defaults!{program} timestamp_timeout=0\n\n"
              f"User opr may run the following commands on h:\n    (root) {program}\n")
    assert operator_sudo_audit(sudo_l, "opr", sudoers_text=text, program=program) == []
    assert no_clear_route_audit("User runner may run the following commands on h:\n    (root) NOPASSWD: /usr/local/libexec/flightctl-helper\n",
                                "runner", program=program) == []
    c7h = SLICES.split("\n### C7h:", 1)[1].split("\n### ", 1)[0]
    assert "REFUSING STUB programs" in c7h and example in c7h and program in c7h


# ---------------------------------------------------------------- rev 3 (Sol 6.1 amd3r2): full key structure, revocation, proof shape
P256_GX = bytes.fromhex("6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296")
P256_GY = bytes.fromhex("4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5")


def _key(key_type: str, *fields: bytes) -> str:
    def s(b: bytes) -> bytes:
        return struct.pack(">I", len(b)) + b
    return f"{key_type} " + base64.b64encode(s(key_type.encode()) + b"".join(s(f) for f in fields)).decode()


GOOD_KEYS = {
    "ssh-ed25519": _key("ssh-ed25519", b"\x11" * 32),
    "sk-ssh-ed25519@openssh.com": _key("sk-ssh-ed25519@openssh.com", b"\x11" * 32, b"ssh:"),
    "ecdsa-sha2-nistp256": _key("ecdsa-sha2-nistp256", b"nistp256", b"\x04" + P256_GX + P256_GY),
    "sk-ecdsa-sha2-nistp256@openssh.com": _key("sk-ecdsa-sha2-nistp256@openssh.com", b"nistp256", b"\x04" + P256_GX + P256_GY, b"ssh:"),
}


@pytest.mark.parametrize("key_type", sorted(GOOD_KEYS))
def test_parse_session_key_accepts_each_complete_supported_structure(key_type) -> None:
    key, problems = parse_session_key(GOOD_KEYS[key_type])
    assert problems == [] and key["key_type"] == key_type and key["public_key"] == GOOD_KEYS[key_type]


@pytest.mark.parametrize("label,text", [
    ("Sol amd3r2: Ed25519 nested length 31", _key("ssh-ed25519", b"\x11" * 31)),
    ("Ed25519 nested length 33", _key("ssh-ed25519", b"\x11" * 33)),
    ("Ed25519 with an extra field", _key("ssh-ed25519", b"\x11" * 32, b"x")),
    ("Ed25519 trailing bytes", "ssh-ed25519 " + base64.b64encode(base64.b64decode(GOOD_KEYS["ssh-ed25519"].split()[1]) + b"\0").decode()),
    ("Sol amd3r2: ECDSA blob with only its type", _key("ecdsa-sha2-nistp256")),
    ("ECDSA wrong curve name", _key("ecdsa-sha2-nistp256", b"nistp384", b"\x04" + P256_GX + P256_GY)),
    ("ECDSA compressed point", _key("ecdsa-sha2-nistp256", b"nistp256", b"\x02" + P256_GX)),
    ("ECDSA on-curve coordinates behind a wrong prefix byte", _key("ecdsa-sha2-nistp256", b"nistp256", b"\x05" + P256_GX + P256_GY)),
    ("ECDSA point with one byte too many", _key("ecdsa-sha2-nistp256", b"nistp256", b"\x04" + P256_GX + P256_GY + b"\x00")),
    ("ECDSA point off the curve", _key("ecdsa-sha2-nistp256", b"nistp256", b"\x04" + P256_GX + bytes(32))),
    ("ECDSA coordinate >= p", _key("ecdsa-sha2-nistp256", b"nistp256", b"\x04" + b"\xff" * 64)),
    ("sk key without application", _key("sk-ssh-ed25519@openssh.com", b"\x11" * 32)),
    ("sk key with a non-ssh application", _key("sk-ssh-ed25519@openssh.com", b"\x11" * 32, b"web:x")),
    ("declared Ed25519, blob ECDSA", "ssh-ed25519 " + GOOD_KEYS["ecdsa-sha2-nistp256"].split()[1]),
])
def test_parse_session_key_refuses_incomplete_structures(label, text) -> None:
    assert parse_session_key(text)[0] is None, label


def test_revoked_key_is_never_re_enrolled_without_an_operator_purge() -> None:
    reg: dict = {}
    assert session_key_enrol(reg, "frienda", ED, friend_sessions_on=True, now=NOW)["ok"]
    assert session_key_revoke(reg, "frienda", ED_FPR, now=NOW)["ok"]
    for who in ("frienda", "friendb"):
        again = session_key_enrol(reg, who, ED, friend_sessions_on=True, now=NOW)
        assert again["ok"] is False and again["error"]["code"] == "conflict", who
    assert resolve_session_key(reg, "frienda", ED_FPR) is None
    assert session_key_revoke(reg, "frienda", ED_FPR, now=NOW, purge=True)["error"]["code"] == "denied"  # the friend cannot purge
    fresh = {}
    session_key_enrol(fresh, "frienda", ED, friend_sessions_on=True, now=NOW)
    assert session_key_revoke(fresh, "opr", ED_FPR, now=NOW, operator=True, purge=True)["error"]["code"] == "conflict"  # revoke first
    assert session_key_revoke(reg, "opr", ED_FPR, now=NOW, operator=True, purge=True)["ok"]
    assert session_key_enrol(reg, "frienda", ED, friend_sessions_on=True, now=NOW)["ok"]  # a NEW record after the purge
    ops = json.loads((ROOT / "contracts/v2/rpc-ops.schema.json").read_text(encoding="utf-8"))
    assert "never re-enrolled" in ops["$defs"]["session_key_revoke_args"]["description"]
    assert ops["$defs"]["session_key_revoke_args"]["properties"]["purge"]["type"] == "boolean"


@pytest.mark.parametrize("label,state,reason,proof,valid", [
    ("open, nothing yet", "open", None, None, True),
    ("open with a reason", "open", "user", None, False),
    ("closing with a reason", "closing", "lease-end", None, True),
    ("closing without a reason", "closing", None, None, False),
    ("closed with a full proof", "closed", "lease-end", {"user_slice_empty": True, "occupancy_empty": True, "key_removed": True}, True),
    ("Sol amd3r2: closed with null reason and proof", "closed", None, None, False),
    ("closed with a reason but no proof", "closed", "user", None, False),
    ("closed with a false fact", "closed", "user", {"user_slice_empty": True, "occupancy_empty": False, "key_removed": True}, False),
    ("failed with the measured proof", "failed", "gateway-error", {"user_slice_empty": False, "occupancy_empty": True, "key_removed": True}, True),
    ("failed without a reason", "failed", None, {"user_slice_empty": False, "occupancy_empty": True, "key_removed": True}, False),
])
def test_executor_registry_state_requires_reason_and_proof(label, state, reason, proof, valid) -> None:
    _, reply = _session_examples()
    entry = {"session_id": "ses-000001", "parent_lease_id": "lse-0000100", "lane_id": "lane-gpu1", "account": "fc-frienda",
             "slice": "user-1001.slice", "state": state, "close_reason": reason, "close_proof": proof}
    assert (errors(dict(reply, ok=True, error=None, sessions=[entry]), "executor") == []) is valid, label
