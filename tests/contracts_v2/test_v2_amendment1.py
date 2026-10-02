"""Amendment 1 (owner decisions of 2 Oct 2026; docs/v2/AMENDMENT-1.md): friend sessions behind a flag (off in R1),
private-CA TLS for A7, operator/executor separation, and the deferred, lockout-safe sshd change for C9."""

import copy
import json
import re
from pathlib import Path

import pytest

from .validation import (
    adapters_semantics,
    assert_invalid,
    assert_valid,
    c9_host_eligible,
    examples,
    friend_sessions_consistent,
    helper_subcommand_enabled,
    sshd_change_verified,
    sshd_effective,
    sshd_keys_plan,
    required_ports,
    tls_cert_decision,
    tls_listener_action,
)

ROOT = Path(__file__).parents[2]
SLICES = (ROOT / "docs/v2/SLICES.md").read_text(encoding="utf-8")


def adapters():
    return copy.deepcopy(examples("adapters")["valid"][0])


def section(title: str) -> str:
    return SLICES.split(title, 1)[1].split("\n### ", 1)[0]


# ------------------------------------------------------------------ Q1 friend_sessions flag

def test_friend_sessions_flag_is_required_and_off_in_examples() -> None:
    a = adapters()
    assert a["features"]["friend_sessions"] is False
    assert_valid(a, "adapters")
    del a["features"]["friend_sessions"]
    assert_invalid(a, "adapters")
    cfg = json.loads((ROOT / "config/adapters-v2.json.example").read_text(encoding="utf-8"))
    assert cfg["features"]["friend_sessions"] is False


def test_friend_sessions_flag_semantics() -> None:
    a = adapters()
    a["features"]["sessions"] = True
    assert any("requires 'friend_sessions'" in p for p in adapters_semantics(a))
    assert "session_gateway" in required_ports({"friend_sessions": True})
    assert "session_gateway" not in required_ports({k: v for k, v in adapters()["features"].items()})


def test_helper_flag_off_refuses_creation_allows_cleanup() -> None:
    """Rev 2 (Sol 6 amd1): flag off refuses only the paths that CREATE friend work (rev 8: 'on' needs both gates and
    the live binding)."""
    off = dict(examples("helper-config")["valid"][0], friend_sessions=False)
    on = dict(off, friend_sessions=True, friend_sessions_global=True, friend_sessions_host=True, c9_binding=dict(BIND))
    assert not helper_subcommand_enabled(off, "session-open", live=LIVE) and helper_subcommand_enabled(on, "session-open", live=LIVE)
    assert not helper_subcommand_enabled(off, "unit-start", friend=True, live=LIVE)
    assert helper_subcommand_enabled(on, "unit-start", friend=True, live=LIVE)
    for cleanup in ("session-close", "unit-stop", "output-collect", "claim-release", "claim-reconcile", "account-check"):
        assert helper_subcommand_enabled(off, cleanup), cleanup  # friend work created earlier can still be cleaned up
    for sub_ in ("unit-start", "unit-stop", "output-collect"):
        assert helper_subcommand_enabled(off, sub_)  # agents unaffected
    assert not helper_subcommand_enabled(on, "claim-clear") and not helper_subcommand_enabled(on, "anything-else")


def test_flag_wording_is_precise_everywhere() -> None:
    texts = [SLICES, (ROOT / "docs/v2/AMENDMENT-1.md").read_text(encoding="utf-8"),
             json.dumps(json.loads((ROOT / "contracts/v2/helper-config.schema.json").read_text(encoding="utf-8")))]
    for text in texts:
        assert "refuses every friend path" not in text
    assert "`test_friend_flag_off_refuses_creation_allows_cleanup`" in SLICES


def test_r1_plan_has_no_friend_sessions() -> None:
    c9 = section("### C9: ")
    assert "NOT in R1" in c9 and "features.friend_sessions" in c9  # Amendment 3: milestone D
    casm = section("### C-ASM: ")
    assert "opens a session" not in casm and "per-job DynamicUser" in casm
    row = next(l for l in SLICES.splitlines() if l.startswith("| C9 |"))
    # Amendment 3 (owner's C9 decision): C9 is NOT in R1 (milestone D, after C-ASM); C9w delivers its pathways in R1
    assert "NOT in R1" in row and "| D |" in row
    c9w = next(l for l in SLICES.splitlines() if l.startswith("| C9w |"))
    assert "| C |" in c9w and "refusing" in c9w
    index = [l.split("|")[1].strip().strip("*") for l in SLICES.split("## Packet index", 1)[1].split("**Assembly prerequisites**", 1)[0].splitlines() if l.startswith("|")]
    assert index.index("C9w") < index.index("C-ASM") < index.index("C9")


# ------------------------------------------------------------------ Q2 private-CA TLS

def test_tls_block_required_and_ca_agnostic() -> None:
    a = adapters()
    assert a["tls"]["mode"] == "private-ca"
    del a["tls"]
    assert_invalid(a, "adapters")
    schema = json.loads((ROOT / "contracts/v2/adapters.schema.json").read_text(encoding="utf-8"))
    props = schema["properties"]["tls"]["properties"]
    assert set(props) == {"mode", "server_name", "cert_file", "key_file", "chain_file", "rotation", "trust_anchor"}
    assert "step" not in json.dumps(props["mode"]) and "vault" not in json.dumps(props["mode"]).lower()


@pytest.mark.parametrize("rotation,bad", [
    ({"check_interval_s": 3600, "renew_before_s": 1209600, "alert_before_s": 259200}, False),
    ({"check_interval_s": 3600, "renew_before_s": 259200, "alert_before_s": 259200}, True),   # alert not below renew
    ({"check_interval_s": 300000, "renew_before_s": 1209600, "alert_before_s": 259200}, True),  # checks too rare
])
def test_tls_block_rotation_order(rotation, bad) -> None:
    a = adapters()
    a["tls"]["rotation"] = rotation
    assert bool([p for p in adapters_semantics(a) if "tls" in p]) is bad


ROOT_A, ROOT_B = "a" * 64, "b" * 64
BASE = dict(server_name="controller-a.example.internal", cert_names=["controller-a.example.internal"], not_before=0.0,
            not_after=1_000_000.0, now=10.0, alert_before_s=259200, chain_ok=True, key_matches=True,
            chain_root_sha256=ROOT_A, anchor_sha256s={ROOT_A})


def test_tls_cert_decision() -> None:
    assert tls_cert_decision(**BASE) == "serve"
    assert tls_cert_decision(**dict(BASE, now=800_000.0)) == "alert"
    for change in ({"cert_names": ["*.example.internal"]}, {"now": 1_000_000.0}, {"now": -1.0}, {"chain_ok": False}, {"key_matches": False}):
        assert tls_cert_decision(**dict(BASE, **change)) == "refuse", change


def test_chain_rooted_elsewhere_and_pins() -> None:
    assert tls_cert_decision(**dict(BASE, chain_root_sha256=ROOT_B)) == "refuse"  # another CA (public/system included)
    assert tls_cert_decision(**dict(BASE, chain_root_sha256=None)) == "refuse"    # chain terminates nowhere known
    assert tls_cert_decision(**dict(BASE, anchor_sha256s={ROOT_A, ROOT_B}, pinned_root_sha256s={ROOT_B})) == "refuse"  # anchor but not pinned
    assert tls_cert_decision(**dict(BASE, pinned_root_sha256s={ROOT_A})) == "serve"
    a = adapters()
    del a["tls"]["trust_anchor"]
    assert_invalid(a, "adapters")
    a = adapters()
    a["tls"]["trust_anchor"]["root_ca_files"] = [a["tls"]["chain_file"]]
    assert any("trust_anchor" in p for p in adapters_semantics(a))


def incumbent(**change):
    """tls_cert_decision re-run on the certificate being served, under the CURRENT config (rev 3)."""
    return tls_cert_decision(**dict(BASE, **change))


def test_incumbent_expiry_stops_tls() -> None:
    assert tls_listener_action(candidate="serve", incumbent=incumbent()) == "use-candidate"
    assert tls_listener_action(candidate="alert", incumbent=None) == "use-candidate"
    assert tls_listener_action(candidate="refuse", incumbent=incumbent()) == "keep-incumbent"
    assert tls_listener_action(candidate="refuse", incumbent=incumbent(now=1_000_000.0)) == "stop-tls"  # incumbent expired
    assert tls_listener_action(candidate=None, incumbent=incumbent(now=1_500_000.0)) == "stop-tls"      # files unreadable too
    assert tls_listener_action(candidate="refuse", incumbent=None) == "stop-tls"                        # start without a good set
    a7 = section("### A7: ")
    assert "STOPS serving TLS" in a7 and "an expired certificate is never served" in a7
    for name in ("test_trust_anchor_only_configured_root", "test_chain_rooted_elsewhere_refused", "test_incumbent_expiry_stops_tls_and_alerts",
                 "test_cert_rotation_reloads_without_dropping_connections", "test_anchor_change_revalidates_incumbent"):
        assert f"`{name}`" in a7


@pytest.mark.parametrize("label,current", [
    # Sol 6 amd1 r2: root A removed from trust_anchor while A's certificate is still date-valid
    ("root removed from trust_anchor", {"anchor_sha256s": {ROOT_B}}),
    ("pin removed / repinned to another root", {"anchor_sha256s": {ROOT_A, ROOT_B}, "pinned_root_sha256s": {ROOT_B}}),
    ("server_name changed", {"server_name": "controller-b.example.internal"}),
])
def test_config_change_revalidates_incumbent(label, current) -> None:
    served = incumbent(**current)  # the date-valid incumbent judged under the NEW config
    assert served == "refuse", label
    assert tls_listener_action(candidate="refuse", incumbent=served) == "stop-tls", label  # no valid candidate: stop + alert
    assert tls_listener_action(candidate=None, incumbent=served) == "stop-tls", label
    assert tls_listener_action(candidate="serve", incumbent=served) == "use-candidate", label  # a valid candidate is served


def test_no_literal_authorized_keys_file_line_in_docs() -> None:
    """Rev 3 (Sol 6 amd1 r2): no doc may give a literal AuthorizedKeysFile value; the only way to form it is the C9
    runbook's discovered-path template (every effective path + the managed directory)."""
    literal = re.compile(r"AuthorizedKeysFile\s+[`'\"]?(?:[./~%]|none\b)")
    sources = [*sorted((ROOT / "docs/v2").glob("*")), *sorted((ROOT / "contracts/v2").glob("*")), *sorted((ROOT / "config").glob("*"))]
    hits = [f"{p.name}: {m.group(0)}" for p in sources if p.is_file() for m in literal.finditer(p.read_text(encoding="utf-8"))]
    assert hits == []
    assert "AuthorizedKeysFile .ssh/authorized_keys /etc/ssh" not in "".join(
        p.read_text(encoding="utf-8") for p in sources if p.is_file())
    oq = (ROOT / "docs/v2/OPEN-QUESTIONS.txt").read_text(encoding="utf-8")
    assert "C9 pre-enable runbook" in oq and "effective key paths" in oq.lower()


def test_no_tailscale_cert_dependency_left() -> None:
    a7 = section("### A7: ")
    assert "tailscale cert" in a7  # only in the stated reason
    assert re.search(r"Why a private CA and not `tailscale cert`", a7)
    assert "--min-validity" not in a7 and "CertDomains" not in a7
    assert "tailscale whois" in a7  # identity unchanged
    oq = (ROOT / "docs/v2/OPEN-QUESTIONS.txt").read_text(encoding="utf-8")
    assert "run `tailscale cert`" not in oq and "Distribute the private root CA" in oq
    readme = (ROOT / "contracts/v2/README.md").read_text(encoding="utf-8")
    assert "the certificate `tailscale cert` issues" not in readme
    assert "ts.net" not in section("### C11b: ")


# ------------------------------------------------------------------ Q3 / Q4 records, sshd runbook

def test_open_questions_record_q1_to_q4() -> None:
    oq = (ROOT / "docs/v2/OPEN-QUESTIONS.txt").read_text(encoding="utf-8")
    assert "Q1  ANSWERED 2 Oct 2026" in oq
    assert "Operator account DECIDED" in oq and "NO\n    NOPASSWD route reaching claim-clear" in oq
    assert "DEDICATED account" in oq
    assert "DEFERRED by\n    Amendment 1 (Q4)" in oq


def test_c9_hosts_openssh_only() -> None:
    assert c9_host_eligible({"ssh_server": "openssh"})
    for value in ("tailscale-ssh", "none", "unknown", None):
        assert not c9_host_eligible({"ssh_server": value} if value else {})
    host = copy.deepcopy(examples("inventory")["valid"][0]["hosts"][0])
    assert_valid(dict(examples("inventory")["valid"][0], hosts=[dict(host, ssh_server="tailscale-ssh")]), "inventory")
    assert_invalid(dict(examples("inventory")["valid"][0], hosts=[dict(host, ssh_server="dropbear")]), "inventory")


@pytest.mark.parametrize("phrase", [
    "DISCOVER the effective configuration", "Never assume `.ssh/authorized_keys`", "sshd -T -C user=<u>", "Never remove an existing path",
    "VERIFY (oracle `sshd_change_verified`)", "`sshd -t`", "SECOND root session", "STOP if: a `Match` block",
    "console access", "Arm an automatic rollback BEFORE reloading", "RELOAD, never restart", "cancel the rollback timer",
    "DEDICATED test sshd", "ignores `authorized_keys`",
])
def test_c9_runbook_lockout_safety(phrase: str) -> None:
    assert phrase in section("### C9: ")


def test_c9_runbook_steps_are_in_a_safe_order() -> None:
    c9 = section("### C9: ")
    order = ["console access", "Back up the current sshd configuration", "DISCOVER the effective configuration", "WRITE a drop-in",
             "VERIFY (oracle", "Arm an automatic rollback",
             "RELOAD, never restart", "cancel the rollback timer"]
    positions = [c9.index(p) for p in order]
    assert positions == sorted(positions)


def test_repo_names_no_operator_account() -> None:
    oq = (ROOT / "docs/v2/OPEN-QUESTIONS.txt").read_text(encoding="utf-8")
    assert "site data, recorded in the lab site-config drafts" in oq
    cfg = examples("helper-config")["valid"][0]
    assert cfg["operator_account"] == "opr"  # placeholder only


# ------------------------------------------------------------------ rev 2: effective sshd key settings (Sol 6 amd1)

NONDEFAULT = """port 22
authorizedkeysfile /etc/ssh/keys/%u
authorizedkeyscommand none
authorizedkeyscommanduser none
"""
DEFAULT = "authorizedkeysfile .ssh/authorized_keys .ssh/authorized_keys2\nauthorizedkeyscommand none\n"


def test_runbook_preserves_non_default_path() -> None:
    before = sshd_effective(NONDEFAULT)
    plan = sshd_keys_plan(before)
    assert plan == ["/etc/ssh/keys/%u", "/etc/ssh/flightctl-keys/%u"]  # the host's real path survives, managed dir appended
    assert sshd_keys_plan(sshd_effective(DEFAULT)) == [".ssh/authorized_keys", ".ssh/authorized_keys2", "/etc/ssh/flightctl-keys/%u"]
    after = sshd_effective(NONDEFAULT.replace("/etc/ssh/keys/%u", " ".join(plan)))
    assert sshd_change_verified(before, after) == []


@pytest.mark.parametrize("after_text,why", [
    ("authorizedkeysfile .ssh/authorized_keys /etc/ssh/flightctl-keys/%u\nauthorizedkeyscommand none\nauthorizedkeyscommanduser none\n", "hardcoded default replaced the real path"),
    ("authorizedkeysfile /etc/ssh/keys/%u\nauthorizedkeyscommand none\nauthorizedkeyscommanduser none\n", "drop-in had no effect (first value wins)"),
    ("authorizedkeysfile /etc/ssh/keys/%u /etc/ssh/flightctl-keys/%u\nauthorizedkeyscommand /usr/bin/x\nauthorizedkeyscommanduser nobody\n", "command changed"),
    ("authorizedkeysfile /etc/ssh/flightctl-keys/%u /etc/ssh/keys/%u\nauthorizedkeyscommand none\nauthorizedkeyscommanduser none\n", "order changed"),
])
def test_runbook_verification_catches(after_text: str, why: str) -> None:
    assert sshd_change_verified(sshd_effective(NONDEFAULT), sshd_effective(after_text)), why


def test_runbook_verification_stops_on_command_only_host() -> None:
    with pytest.raises(ValueError):
        sshd_keys_plan(sshd_effective("authorizedkeysfile none\nauthorizedkeyscommand /usr/bin/fetch-keys %u\n"))


def test_c9_carried_obligations_named() -> None:
    c9 = section("### C9: ")
    assert "`test_authority_refuses_friend_session_on_ineligible_host`" in c9
    assert "`test_runbook_against_effective_sshd_config`" in c9
    oq = (ROOT / "docs/v2/OPEN-QUESTIONS.txt").read_text(encoding="utf-8")
    assert "Name the operator account for claim-clear" not in oq and "Install the claim-clear sudoers rule on that account" in oq


# ------------------------------------------------------------------ rev 4: bounded session probe (Sol 6 amd1 r3)

from .validation import (  # noqa: E402
    CLEAR_PATH,
    HELPER_PATH,
    PROBE_PATH,
    PROBE_MAX_TTL_S,
    helper_config_semantics,
    no_clear_route_audit,
    operator_sudo_audit,
    probe_sweep,
    session_probe,
    sshd_expiry_effective,
)

PROBE_OP = {"program": PROBE_PATH, "ruid": 0, "euid": 0, "sudo_user": "opr", "sudo_uid": 1500}
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIProbeKeyForTheRunbookOnly0123456789"


def probe_cfg(**change):
    cfg = dict(examples("helper-config")["valid"][0], friend_sessions=False, probe_account="fc-probe")
    cfg.update(change)
    return cfg


def probe(tmp_path, cfg=None, inv=PROBE_OP, account="fc-probe", ttl=600, now=1_000.0, audit=None):
    audit = [] if audit is None else audit
    return session_probe(str(tmp_path), cfg or probe_cfg(), inv, 1500, account=account, pubkey=KEY, ttl_s=ttl, now=now, audit=audit,
                         mono_now=now, boot_id="boot-a"), audit


def test_probe_works_with_flag_off_for_the_probe_account_only(tmp_path: Path) -> None:
    ok, audit = probe(tmp_path)
    assert ok and audit[-1]["event"] == "probe-granted"
    line = (tmp_path / "fc-probe").read_text(encoding="utf-8")
    assert line.startswith('restrict,command="') and 'expiry-time="' in line and line.rstrip().endswith(KEY)
    assert not (tmp_path / "fc-frienda").exists()


@pytest.mark.parametrize("label,kwargs", [
    ("friend account", {"account": "fc-frienda"}),
    ("operator account", {"account": "opr"}),
    ("non-operator caller", {"inv": dict(PROBE_OP, sudo_user="runner", sudo_uid=1001)}),
    ("through the executor's helper", {"inv": dict(PROBE_OP, program=HELPER_PATH)}),
    ("through claim-clear's program", {"inv": dict(PROBE_OP, program=CLEAR_PATH)}),
    ("not root", {"inv": dict(PROBE_OP, euid=1500)}),
    ("flag on", {"cfg": probe_cfg(friend_sessions=True)}),
    ("probing disabled", {"cfg": probe_cfg(probe_account=None)}),
    ("ttl over the cap", {"ttl": PROBE_MAX_TTL_S + 1}),
    ("zero ttl", {"ttl": 0}),
])
def test_probe_refusals(tmp_path: Path, label, kwargs) -> None:
    ok, audit = probe(tmp_path, **kwargs)
    assert not ok, label
    assert audit[-1]["event"] == "probe-refused" and audit[-1]["reasons"], label
    assert [p.name for p in tmp_path.iterdir()] == [], label  # nothing written


ROOT_TIMER = {"program": PROBE_PATH, "ruid": 0, "euid": 0, "sudo_user": None, "sudo_uid": None}  # systemd unit/timer
OPERATOR_SUDO = dict(PROBE_OP)  # the operator's step-7 revoke through sudo is also uid 0


def test_probe_ttl_expiry_and_removal(tmp_path: Path) -> None:
    cfg, audit = probe_cfg(), []
    assert probe(tmp_path, cfg=cfg, now=1_000.0, ttl=600, audit=audit)[0]
    assert not probe(tmp_path, cfg=cfg, now=1_100.0, audit=audit)[0]  # an unexpired probe key already exists
    line = (tmp_path / "fc-probe").read_text(encoding="utf-8")
    assert 'expiry-time="19700101002640Z"' in line  # 1_000 + 600 s in UTC, Z-suffixed (rev 5)
    assert not probe_sweep(str(tmp_path), cfg, ROOT_TIMER, now=1_599.0, audit=audit, mono_now=1_599.0, boot_id="boot-a", mode="expired")  # not yet
    assert audit[-1]["event"] == "sweep-skipped-unexpired" and audit[-1]["expires_at"] == 1_600.0  # rev 6: audited, not silent
    assert (tmp_path / "fc-probe").exists()
    assert probe_sweep(str(tmp_path), cfg, ROOT_TIMER, now=1_600.0, audit=audit, mono_now=1_600.0, boot_id="boot-a", mode="expired")
    assert not (tmp_path / "fc-probe").exists() and audit[-1]["event"] == "probe-swept-expired"
    assert probe(tmp_path, cfg=cfg, now=1_700.0, audit=audit)[0]  # after expiry a new probe may be written


@pytest.mark.parametrize("who", ["root timer (rollback unit)", "operator revoke via sudo"])
def test_probe_removed_on_revoke_and_rollback(tmp_path: Path, who: str) -> None:
    cfg, audit = probe_cfg(), []
    inv = ROOT_TIMER if who.startswith("root") else OPERATOR_SUDO
    assert probe(tmp_path, cfg=cfg, audit=audit)[0]
    assert probe_sweep(str(tmp_path), cfg, inv, now=1_001.0, audit=audit, mono_now=1_001.0, boot_id="boot-a", mode="all")  # before expiry
    assert list(tmp_path.iterdir()) == [] and audit[-1]["event"] == "probe-swept-all"
    before = len(audit)
    assert not probe_sweep(str(tmp_path), cfg, inv, now=1_002.0, audit=audit, mono_now=1_002.0, boot_id="boot-a", mode="all")  # idempotent, no error
    assert len(audit) == before + 1 and audit[-1]["event"] == "sweep-none-present"  # rev 6: the no-op is audited too


# ------------------------------------------------------------------ rev 5 (Sol 6 amd1 r4): Z suffix and root-only sweep

ISSUE = 1_790_950_000.0  # 2 Oct 2026, so real time-zone offsets apply


@pytest.mark.parametrize("server_tz", ["UTC", "America/New_York", "Pacific/Chatham", "Asia/Kolkata", "Pacific/Kiritimati"])
def test_probe_expiry_is_utc_whatever_the_server_tz(tmp_path: Path, server_tz: str) -> None:
    assert probe(tmp_path, now=ISSUE, ttl=900)[0]
    line = (tmp_path / "fc-probe").read_text(encoding="utf-8")
    value = re.search(r'expiry-time="([0-9]{12}(?:[0-9]{2})?Z?)"', line).group(1)
    assert value.endswith("Z")
    effective = sshd_expiry_effective(value, server_tz)
    assert ISSUE < effective <= ISSUE + 900


def test_sshd_expiry_semantics_without_z_follow_the_server_tz() -> None:
    # the oracle reproduces sshd(8): an unsuffixed value is local time, so it shifts with the server's zone
    assert sshd_expiry_effective("20261002120000Z", "America/New_York") == sshd_expiry_effective("20261002120000Z", "UTC")
    assert sshd_expiry_effective("20261002120000", "America/New_York") - sshd_expiry_effective("20261002120000", "UTC") == 4 * 3600
    assert sshd_expiry_effective("202610021200", "UTC") == sshd_expiry_effective("20261002120000", "UTC")


def test_sweep_works_as_root_without_sudo_and_add_is_refused_there(tmp_path: Path) -> None:
    cfg, audit = probe_cfg(), []
    ok, _ = probe(tmp_path, cfg=cfg, inv=ROOT_TIMER, audit=audit)  # add path from a root unit with no sudo context
    assert not ok and audit[-1]["event"] == "probe-refused" and list(tmp_path.iterdir()) == []
    assert probe(tmp_path, cfg=cfg, audit=audit)[0]  # the operator adds
    assert probe_sweep(str(tmp_path), cfg, ROOT_TIMER, now=1_001.0, audit=audit, mono_now=1_001.0, boot_id="boot-a", mode="all")  # root removes, no sudo vars
    for bad in (dict(ROOT_TIMER, ruid=1500), dict(ROOT_TIMER, euid=1500), dict(ROOT_TIMER, program=HELPER_PATH)):
        assert probe(tmp_path, cfg=cfg, audit=audit)[0]
        assert not probe_sweep(str(tmp_path), cfg, bad, now=1_002.0, audit=audit, mono_now=1_002.0, boot_id="boot-a", mode="all")
        assert audit[-1]["event"] == "sweep-refused" and (tmp_path / "fc-probe").exists()
        assert probe_sweep(str(tmp_path), cfg, ROOT_TIMER, now=1_002.0, audit=audit, mono_now=1_002.0, boot_id="boot-a", mode="all")


def test_sweep_never_touches_non_probe_keys(tmp_path: Path) -> None:
    cfg, audit = probe_cfg(friend_accounts=["fc-frienda"]), []
    friend_key = tmp_path / "fc-frienda"
    friend_key.write_text("ssh-ed25519 AAAAfriendkey\n", encoding="utf-8")
    assert not probe_sweep(str(tmp_path), cfg, ROOT_TIMER, now=1.0, audit=audit, mono_now=1.0, boot_id="boot-a", mode="all", account="fc-frienda")
    assert audit[-1]["event"] == "sweep-refused" and friend_key.exists()
    # a non-probe line sitting at the probe path (e.g. someone put a real key there) is refused and left in place
    (tmp_path / "fc-probe").write_text("ssh-ed25519 AAAArealkey\n", encoding="utf-8")
    assert not probe_sweep(str(tmp_path), cfg, ROOT_TIMER, now=1.0, audit=audit, mono_now=1.0, boot_id="boot-a", mode="all")
    assert audit[-1]["event"] == "sweep-refused-non-probe"
    assert (tmp_path / "fc-probe").exists() and friend_key.exists()
    assert not probe_sweep(str(tmp_path), cfg, ROOT_TIMER, now=1.0, audit=audit, mono_now=1.0, boot_id="boot-a", mode="extend")  # no other modes


def test_sweep_contract_text() -> None:
    cfg = json.loads((ROOT / "contracts/v2/helper-config.schema.json").read_text(encoding="utf-8"))["x-operator-session-probe"]
    for phrase in ("--sweep", "ROOT-ONLY", "REMOVE-ONLY", "flightctl-session-probe-sweep.timer", "idempotent", "Z-suffixed"):
        assert phrase in cfg, phrase
    step5 = section("### C9: ").split("  5. ", 1)[1].split("\n  6. ", 1)[0]
    assert "--sweep --all" in step5


def test_probe_account_is_dedicated() -> None:
    good = probe_cfg()
    assert_valid(good, "helper-config") and helper_config_semantics(good) == []
    assert_invalid(dict(good, probe_account="fc-frienda"), "helper-config")  # must look like a probe account
    assert helper_config_semantics(dict(good, friend_accounts=["fc-frienda", "fc-probe"]))
    assert not helper_subcommand_enabled(good, "session-probe")  # never a subcommand of the executor's helper


def test_probe_sudo_route_is_audited_like_claim_clear() -> None:
    example = (ROOT / "config/flightctl-session-probe-sudoers.example").read_text(encoding="utf-8")
    assert f"Defaults!{PROBE_PATH} timestamp_timeout=0" in example and "NOPASSWD" not in example.split("#")[-1]
    sudo_l = ("Matching Defaults entries for opr on h:\n    env_reset, !setenv\n\n"
              f"Runas and Command-specific defaults for opr:\n    Defaults!{PROBE_PATH} timestamp_timeout=0\n\n"
              f"User opr may run the following commands on h:\n    (root) {PROBE_PATH}\n")
    assert operator_sudo_audit(sudo_l, "opr", program=PROBE_PATH) == []
    assert operator_sudo_audit(sudo_l.replace("timestamp_timeout=0", "timestamp_timeout=5"), "opr", program=PROBE_PATH)
    executor = "User runner may run the following commands on h:\n    (root) NOPASSWD: /usr/local/libexec/*\n"
    assert no_clear_route_audit(executor, "runner", program=PROBE_PATH)


def test_runbook_uses_the_probe_with_the_flag_off() -> None:
    c9 = section("### C9: ")
    step7 = c9.split("  7. ", 1)[1].split("\n  8. ", 1)[0]
    assert "flag still OFF" in step7 and PROBE_PATH in step7 and "--sweep --all" in step7  # rev 5: revoke = sweep
    assert "removes any probe key" in c9.split("  5. ", 1)[1].split("\n  6. ", 1)[0]
    assert "probe key is gone" in c9.split("  8. ", 1)[1]
    for name in ("test_authority_refuses_session_open_when_flag_off", "test_runbook_probe_login_with_flag_off"):
        assert f"`{name}`" in c9
    assert "`test_session_probe_bounds`" in section("### C7h: ")


def test_every_sweep_outcome_is_audited(tmp_path: Path) -> None:
    """Rev 6 (Sol 6 amd1 r5): C7h requires an audit event for EVERY session-probe call, so no sweep outcome is silent."""
    cfg, audit = probe_cfg(), []
    outcomes = []

    def sweep(mode, **kw):
        before = len(audit)
        n = kw.pop("now", 1_001.0)
        probe_sweep(str(tmp_path), cfg, kw.pop("inv", ROOT_TIMER), now=n, audit=audit, mode=mode, mono_now=n, boot_id="boot-a", **kw)
        assert len(audit) == before + 1, (mode, kw)  # exactly one event per call
        outcomes.append(audit[-1]["event"])

    sweep("expired")                                   # nothing there
    assert probe(tmp_path, cfg=cfg, now=1_000.0, ttl=600, audit=audit)[0]
    sweep("expired")                                   # present, not yet expired
    sweep("all", account="fc-frienda")                 # another account
    sweep("all", inv=dict(ROOT_TIMER, euid=1500))      # not root
    sweep("all")                                       # removed
    (tmp_path / "fc-probe").write_text("ssh-ed25519 AAAAreal\n", encoding="utf-8")
    sweep("all")                                       # non-probe content left in place
    assert outcomes == ["sweep-none-present", "sweep-skipped-unexpired", "sweep-refused", "sweep-refused", "probe-swept-all",
                        "sweep-refused-non-probe"]
    assert all(e["at"] for e in audit)


# ------------------------------------------------------------------ rev 7/8 (Sol 6 amd1 r6, r7): per-host, host-bound gate

from .validation import (  # noqa: E402
    authority_admits_friend_work,
    friend_session_host_allowed,
    helper_binding_ok,
    helper_flag_expected,
    schema_prose,
)

NOW = 1_790_950_000.0  # 2 Oct 2026
HA = {"sshd_host_key_sha256": "1" * 64, "machine_id_sha256": "2" * 64, "sshd_effective_sha256": "3" * 64}
HB = {"sshd_host_key_sha256": "4" * 64, "machine_id_sha256": "5" * 64, "sshd_effective_sha256": "6" * 64}
PROOF7 = dict({"proof_id": "c9-host-a-20261002", "artefact_sha256": "c" * 64, "recorded_at": "2026-10-02T12:00:00Z",
               "runbook_revision": "amendment-1-rev-10", "sshd_verified": True, "probe_login_ok": True, "probe_key_removed": True,
               "no_conditional_key_settings": True,
               "host_id": "host-a"}, **HA)
STORE = {"c9-host-a-20261002": {"sha256": "c" * 64, "host_id": "host-a"}}
BIND = {"proof_id": "c9-host-a-20261002", "host_id": "host-a", **HA}
LIVE = {"host_id": "host-a", **HA}


def two_hosts():
    base = copy.deepcopy(examples("inventory")["valid"][0]["hosts"][0])
    a = dict(copy.deepcopy(base), host_id="host-a", ssh_server="openssh", friend_sessions_enabled=True, c9_proof=dict(PROOF7), **HA)
    b = dict(copy.deepcopy(base), host_id="host-b", ssh_server="openssh", friend_sessions_enabled=False, c9_proof=None, **HB)
    return a, b


def site(global_on=True):
    s = adapters()
    s["features"]["friend_sessions"] = global_on
    return s


def helper_for(s, host, store=STORE, now=NOW):
    on = helper_flag_expected(s, host, store, now)
    return dict(probe_cfg(), friend_sessions=on, friend_sessions_global=s["features"]["friend_sessions"],
                friend_sessions_host=host.get("friend_sessions_enabled") is True,
                c9_binding=({"proof_id": host["c9_proof"]["proof_id"], "host_id": host["host_id"],
                             **{k: host["c9_proof"][k] for k in HA}} if on else None))


def session_open_ok(s, host, helper_cfg, live, store=STORE, now=NOW) -> bool:
    return authority_admits_friend_work(s, host, store, now)[0] and helper_subcommand_enabled(helper_cfg, "session-open", live=live)


def test_per_host_gate_only_proven_host_accepts() -> None:
    a, b = two_hosts()
    s = site(True)
    helper = {h["host_id"]: helper_for(s, h) for h in (a, b)}
    assert session_open_ok(s, a, helper["host-a"], LIVE)                           # proven host accepts
    assert not session_open_ok(s, b, helper["host-b"], {"host_id": "host-b", **HB})  # unproven OpenSSH host refuses
    assert authority_admits_friend_work(s, b, STORE, NOW) == (False, "host friend_sessions_enabled is false")
    assert friend_sessions_consistent(s, [a, b], helper, STORE, NOW) == []        # rev 8: a mixed deployment is valid
    wrong = dict(helper, **{"host-b": dict(helper["host-b"], friend_sessions=True)})
    assert friend_sessions_consistent(s, [a, b], wrong, STORE, NOW)              # the check reports it before activation
    assert not session_open_ok(s, b, wrong["host-b"], {"host_id": "host-b", **HB})
    off = site(False)
    assert not friend_session_host_allowed(off, a, STORE, NOW)
    assert authority_admits_friend_work(off, a, STORE, NOW) == (False, "feature friend_sessions is off")


@pytest.mark.parametrize("label,change,reason", [
    ("no proof", {"c9_proof": None}, "host has no valid C9 runbook proof"),
    ("probe login failed", {"c9_proof": dict(PROOF7, probe_login_ok=False)}, "host has no valid C9 runbook proof"),
    ("bad artefact hash", {"c9_proof": dict(PROOF7, artefact_sha256="xyz")}, "host has no valid C9 runbook proof"),
    ("tailscale-ssh host", {"ssh_server": "tailscale-ssh"}, "host ssh_server is not openssh"),
])
def test_per_host_gate_needs_a_valid_proof_on_an_openssh_host(label, change, reason) -> None:
    a, _ = two_hosts()
    assert authority_admits_friend_work(site(True), dict(a, **change), STORE, NOW) == (False, reason), label


def test_per_host_gate_schema() -> None:
    inv = copy.deepcopy(examples("inventory")["valid"][0])
    a, b = two_hosts()
    assert_valid(dict(inv, hosts=[a, b]), "inventory")
    assert_invalid(dict(inv, hosts=[dict(a, c9_proof=None)]), "inventory")              # enabled without proof
    assert_invalid(dict(inv, hosts=[dict(a, ssh_server="tailscale-ssh")]), "inventory")  # enabled on a Tailscale-SSH host
    assert_invalid(dict(inv, hosts=[dict(a, c9_proof=dict(PROOF7, probe_key_removed=False))]), "inventory")
    assert_invalid(dict(inv, hosts=[dict(a, sshd_host_key_sha256=None)]), "inventory")  # enabled without an observed host key
    proof_no_binding = {k: v for k, v in PROOF7.items() if k != "machine_id_sha256"}
    assert_invalid(dict(inv, hosts=[dict(a, c9_proof=proof_no_binding)]), "inventory")
    step8 = section("### C9: ").split("  8. ", 1)[1]
    assert "for THIS host only" in step8 and "enables no host by itself" in step8 and "MUST PASS before activation" in step8
    for name in ("test_per_host_gate_only_proven_host_accepts", "test_helper_refuses_without_both_flags_and_live_binding",
                 "test_proof_bound_to_host", "test_proof_invalidated_by_sshd_change_or_reimage", "test_consistency_check_passes_before_activation"):
        assert f"`{name}`" in section("### C9: ")


# ---- rev 8: Sol's cases and the adversarial self-pass over the C9 enablement path

def test_r8_helper_flag_true_with_global_false_is_refused_at_the_helper() -> None:
    cfg = dict(probe_cfg(), friend_sessions=True, friend_sessions_global=False, friend_sessions_host=True, c9_binding=dict(BIND))
    assert not helper_subcommand_enabled(cfg, "session-open", live=LIVE)  # Sol 6 amd1 r7


@pytest.mark.parametrize("fs,glob,hostflag", [(f, g, h) for f in (False, True) for g in (False, True) for h in (False, True)])
def test_r8_every_helper_flag_combination(fs, glob, hostflag) -> None:
    cfg = dict(probe_cfg(), friend_sessions=fs, friend_sessions_global=glob, friend_sessions_host=hostflag, c9_binding=dict(BIND))
    assert helper_subcommand_enabled(cfg, "session-open", live=LIVE) is (fs and glob and hostflag)
    assert helper_subcommand_enabled(cfg, "unit-start", friend=True, live=LIVE) is (fs and glob and hostflag)
    assert helper_subcommand_enabled(cfg, "session-close")  # cleanup never depends on the flags


@pytest.mark.parametrize("label,live", [
    ("no live facts", None),
    ("sshd config changed since the proof", dict(LIVE, sshd_effective_sha256="9" * 64)),
    ("host re-imaged (machine-id)", dict(LIVE, machine_id_sha256="8" * 64)),
    ("host key rotated", dict(LIVE, sshd_host_key_sha256="7" * 64)),
    ("binding copied to another host", dict(LIVE, host_id="host-b")),
])
def test_r8_helper_rechecks_the_live_binding(label, live) -> None:
    cfg = dict(probe_cfg(), friend_sessions=True, friend_sessions_global=True, friend_sessions_host=True, c9_binding=dict(BIND))
    assert helper_binding_ok(cfg, LIVE)
    assert not helper_subcommand_enabled(cfg, "session-open", live=live), label


def test_r8_proof_copied_to_another_host_is_refused() -> None:
    a, b = two_hosts()
    s = site(True)
    copied = dict(b, friend_sessions_enabled=True, c9_proof=dict(PROOF7))                  # Sol's copy case
    assert authority_admits_friend_work(s, copied, STORE, NOW) == (False, "proof is bound to another host")
    edited = dict(copied, c9_proof=dict(PROOF7, host_id="host-b"))                        # host id edited in the copy
    ok, reason = authority_admits_friend_work(s, edited, STORE, NOW)
    assert not ok and "does not match the host's observed value" in reason
    forged = dict(copied, c9_proof=dict(PROOF7, host_id="host-b", **HB))                  # binding forged to host B's facts
    assert authority_admits_friend_work(s, forged, STORE, NOW) == (False, "proof artefact missing or does not match the stored artefact")


@pytest.mark.parametrize("label,host_change,store,reason_part", [
    ("sshd config changed (observed sshd -T hash differs)", {"sshd_effective_sha256": "9" * 64}, STORE, "sshd_effective_sha256"),
    ("host re-imaged (machine-id and host key differ)", {"machine_id_sha256": "8" * 64, "sshd_host_key_sha256": "7" * 64}, STORE, "_sha256"),
    ("observed facts unknown", {"sshd_host_key_sha256": None}, STORE, "sshd_host_key_sha256"),
    ("artefact missing from the store", {}, {}, "artefact"),
    ("artefact hash differs", {}, {"c9-host-a-20261002": {"sha256": "d" * 64, "host_id": "host-a"}}, "artefact"),
])
def test_r8_stale_or_unbacked_proofs_are_refused(label, host_change, store, reason_part) -> None:
    a, _ = two_hosts()
    ok, reason = authority_admits_friend_work(site(True), dict(a, **host_change), store, NOW)
    assert not ok and reason_part in reason, label


def test_r8_clock_rollback_and_future_dated_proofs() -> None:
    a, _ = two_hosts()
    s = site(True)
    assert authority_admits_friend_work(s, a, STORE, NOW)[0]
    rolled_back = 1_759_000_000.0  # the authority's clock jumped back about a year
    assert authority_admits_friend_work(s, a, STORE, rolled_back) == (False, "proof is dated in the future (clock rollback?)")


def test_r8_probe_cannot_be_extended_by_a_clock_rollback(tmp_path: Path) -> None:
    cfg, audit = probe_cfg(), []
    assert session_probe(str(tmp_path), cfg, PROBE_OP, 1500, account="fc-probe", pubkey=KEY, ttl_s=600, now=10_000.0, audit=audit,
                         mono_now=500.0, boot_id="boot-a")
    # wall clock rolled back an hour, but the monotonic clock passed the deadline: the sweep removes it
    assert probe_sweep(str(tmp_path), cfg, ROOT_TIMER, now=10_000.0 - 3600 + 700, audit=audit, mode="expired", mono_now=1_101.0, boot_id="boot-a")
    assert not (tmp_path / "fc-probe").exists()
    assert session_probe(str(tmp_path), cfg, PROBE_OP, 1500, account="fc-probe", pubkey=KEY, ttl_s=600, now=20_000.0, audit=audit,
                         mono_now=50.0, boot_id="boot-a")
    # after a reboot the monotonic clock restarted: the probe is treated as expired
    assert probe_sweep(str(tmp_path), cfg, ROOT_TIMER, now=20_001.0, audit=audit, mode="expired", mono_now=5.0, boot_id="boot-b")
    assert not (tmp_path / "fc-probe").exists()


def test_r8_activation_order_and_mixed_deployment() -> None:
    a, b = two_hosts()
    s = site(True)
    helper = {"host-a": helper_for(s, a), "host-b": helper_for(s, b)}
    assert friend_sessions_consistent(s, [a, b], helper, STORE, NOW) == []
    for label, bad in [("global copy wrong", dict(helper["host-a"], friend_sessions_global=False)),
                       ("host copy wrong", dict(helper["host-a"], friend_sessions_host=False)),
                       ("binding differs", dict(helper["host-a"], c9_binding=dict(BIND, sshd_effective_sha256="9" * 64)))]:
        assert friend_sessions_consistent(s, [a, b], dict(helper, **{"host-a": bad}), STORE, NOW), label


TAILNET_CERT = re.compile(r"tailscale cert|tailnet HTTPS|HTTPS certificates for the tailnet|CertDomains|tailnet certificate", re.I)
RATIONALE = re.compile(r"certificate-transparency|is removed|instead of|not `tailscale cert`|replaces", re.I)


def tailnet_cert_offenders(text: str) -> list[str]:
    flat = re.sub(r"\s+", " ", text)
    sentences = re.split(r"(?<=[.;])\s+|\s+-\s+(?=[A-Z])", flat)
    return [s[:140] for s in sentences if TAILNET_CERT.search(s) and not RATIONALE.search(s)]


def test_no_active_tailnet_cert_instruction() -> None:
    """Rev 7 (Sol 6 amd1 r6): active packet/contract text may mention tailscale cert or tailnet HTTPS only as the
    certificate-transparency rationale; FROZEN.md (historical), AMENDMENT-1.md (change log) and the review traces are
    exempt."""
    exempt = {"FROZEN.md", "AMENDMENT-1.md", "RESPONSE-TO-SOL6.tsv", "CONFORMANCE.tsv"}
    sources = [p for p in [*sorted((ROOT / "docs/v2").glob("*")), *sorted((ROOT / "contracts/v2").glob("*")), *sorted((ROOT / "config").glob("*"))]
               if p.is_file() and p.name not in exempt]
    found = {}
    for p in sources:
        raw = p.read_text(encoding="utf-8")
        text = schema_prose(json.loads(raw)) if p.name.endswith(".schema.json") else raw
        hits = tailnet_cert_offenders(text)
        if hits:
            found[p.name] = hits
    assert found == {}
    # Sol's leftover A-ASM sentence would be caught
    assert tailnet_cert_offenders("Install the authority over HTTPS (owner action: enable tailnet HTTPS certificates).")
    a_asm = SLICES.split("### A-ASM", 1)[1].split("\n### ", 1)[0]
    assert "private-CA" in a_asm and "trust_anchor" in a_asm


# ------------------------------------------------------------------ rev 9 (Sol 6 amd1 r8)

import itertools  # noqa: E402

from .validation import (  # noqa: E402
    authority_admits_any_work,
    friend_sessions_disable,
    helper_create_friend_work,
    sshd_effective_digest,
    sshd_host_keys_sha256,
)


def enabled_pair():
    a, b = two_hosts()
    b = dict(b, friend_sessions_enabled=True, c9_proof=dict(PROOF7, proof_id="c9-host-b-20261002", host_id="host-b", **HB))
    store = dict(STORE, **{"c9-host-b-20261002": {"sha256": "c" * 64, "host_id": "host-b"}})
    s = site(True)
    return s, [a, b], {h["host_id"]: helper_for(s, h, store) for h in (a, b)}, store


# --- 1. fail-closed disable

def test_r9_disable_sols_case_stale_helper_copy() -> None:
    s, hosts, helpers, store = enabled_pair()
    assert helper_subcommand_enabled(helpers["host-a"], "session-open", live=LIVE)  # enabled before
    state = friend_sessions_disable(hosts, helpers, reachable={"host-a", "host-b"})
    assert state["site_state"] == "off" and state["pending"] == []
    for hid, cfg in state["helpers"].items():
        assert not helper_subcommand_enabled(cfg, "session-open", live=dict(LIVE, host_id=hid))  # no stale accept


def test_r9_disable_unreachable_host_stays_pending_and_gets_no_work() -> None:
    s, hosts, helpers, store = enabled_pair()
    state = friend_sessions_disable(hosts, helpers, reachable={"host-b"})
    assert state["site_state"] == "disabling" and state["pending"] == ["host-a"]  # never reported off while pending
    assert not authority_admits_any_work("host-a", state) and authority_admits_any_work("host-b", state)
    later = friend_sessions_disable(hosts, state["helpers"], reachable={"host-a"}, already_off={"host-b"})
    assert later["site_state"] == "off" and later["pending"] == []


def test_r9_disable_property_over_every_reachability_subset() -> None:
    s, hosts, helpers, store = enabled_pair()
    for n in range(3):
        for reach in itertools.combinations(["host-a", "host-b"], n):
            state = friend_sessions_disable(hosts, helpers, reachable=set(reach))
            accepting = [h for h, c in state["helpers"].items() if helper_subcommand_enabled(c, "session-open", live=dict(LIVE, host_id=h))]
            if state["site_state"] == "off":
                assert accepting == [], reach
            assert set(accepting) <= set(state["pending"]), reach  # only pending (unreachable) hosts may still hold a stale copy
            assert all(not authority_admits_any_work(h, state) for h in state["pending"])


# --- 2. user-specific sshd binding, all host keys, one-operation check/create

GLOBAL_T = "port 22\nauthorizedkeysfile .ssh/authorized_keys /etc/ssh/flightctl-keys/%u\nauthorizedkeyscommand none\n"
USER_OK = GLOBAL_T
USER_MATCH_DROPPED = "port 22\nauthorizedkeysfile .ssh/authorized_keys\nauthorizedkeyscommand none\n"  # Match User removed the managed path
FILES = {"/etc/ssh/sshd_config": "AuthorizedKeysFile .ssh/authorized_keys /etc/ssh/flightctl-keys/%u\nPasswordAuthentication no\n"}


def test_r9_user_specific_binding_sols_match_user_case() -> None:
    before = sshd_effective_digest(GLOBAL_T, {"fc-frienda": USER_OK, "fc-probe": USER_OK}, FILES)
    after = sshd_effective_digest(GLOBAL_T, {"fc-frienda": USER_MATCH_DROPPED, "fc-probe": USER_OK}, FILES)  # global output unchanged
    assert before != after
    a, _ = two_hosts()
    proof = dict(PROOF7, sshd_effective_sha256=before)
    host = dict(a, sshd_effective_sha256=before, c9_proof=proof)
    s = site(True)
    cfg = dict(helper_for(s, host), c9_binding=dict(BIND, sshd_effective_sha256=before))
    assert authority_admits_friend_work(s, host, STORE, NOW)[0]
    assert helper_subcommand_enabled(cfg, "session-open", live=dict(LIVE, sshd_effective_sha256=before))
    assert not helper_subcommand_enabled(cfg, "session-open", live=dict(LIVE, sshd_effective_sha256=after))           # helper refuses
    assert not authority_admits_friend_work(s, dict(host, sshd_effective_sha256=after), STORE, NOW)[0]                # authority refuses


def test_r9_user_digest_is_canonical_and_covers_new_accounts() -> None:
    base = sshd_effective_digest(GLOBAL_T, {"fc-frienda": USER_OK, "fc-probe": USER_OK}, FILES)
    assert sshd_effective_digest("\n".join(reversed(GLOBAL_T.splitlines())) + "\n\n", {"fc-probe": USER_OK, "fc-frienda": "  " + USER_OK}, FILES) == base
    assert sshd_effective_digest(GLOBAL_T, {"fc-frienda": USER_OK, "fc-probe": USER_OK, "fc-friendb": USER_OK}, FILES) != base  # new friend
    assert sshd_effective_digest(GLOBAL_T.replace("port 22", "port 2222"), {"fc-frienda": USER_OK, "fc-probe": USER_OK}, FILES) != base


KEYS = ["ssh-ed25519 AAAAC3edkey host@a", "ecdsa-sha2-nistp256 AAAAE2ecdsakey host@a", "ssh-rsa AAAAB3rsakey host@a"]


def test_r9_host_keys_digest_covers_all_keys() -> None:
    base = sshd_host_keys_sha256(KEYS)
    assert sshd_host_keys_sha256(list(reversed(KEYS))) == base                                   # order-insensitive
    assert sshd_host_keys_sha256([k.rsplit(" ", 1)[0] + " other-comment" for k in KEYS]) == base  # comments dropped
    assert sshd_host_keys_sha256(KEYS[:2] + ["ssh-rsa AAAAB3ROTATED host@a"]) != base             # rotate the THIRD key
    assert sshd_host_keys_sha256(KEYS + ["ssh-ed25519 AAAAC3extra"]) != base                      # a key added
    assert sshd_host_keys_sha256(KEYS[1:]) != base                                                # a key removed


def test_r9_check_create_reverify_is_one_operation() -> None:
    cfg = dict(probe_cfg(), friend_sessions=True, friend_sessions_global=True, friend_sessions_host=True, c9_binding=dict(BIND))
    calls = []
    assert helper_create_friend_work(cfg, "session-open", measure=lambda: dict(LIVE), create=lambda: calls.append("create"),
                                     undo=lambda: calls.append("undo"))
    assert calls == ["create"]
    seq = iter([dict(LIVE), dict(LIVE, sshd_effective_sha256="9" * 64)])  # config changes between check and commit
    calls.clear()
    assert not helper_create_friend_work(cfg, "session-open", measure=lambda: next(seq), create=lambda: calls.append("create"),
                                         undo=lambda: calls.append("undo"))
    assert calls == ["create", "undo"]
    calls.clear()
    off = dict(cfg, friend_sessions_global=False)
    assert not helper_create_friend_work(off, "session-open", measure=lambda: dict(LIVE), create=lambda: calls.append("create"),
                                         undo=lambda: calls.append("undo"))
    assert calls == []  # refused before anything is created


# --- 3. monotonic source required

def test_r9_monotonic_and_boot_id_are_required(tmp_path: Path) -> None:
    cfg, audit = probe_cfg(), []
    base = dict(account="fc-probe", pubkey=KEY, ttl_s=600, now=10_000.0, audit=audit)
    with pytest.raises(TypeError):
        session_probe(str(tmp_path), cfg, PROBE_OP, 1500, **base, boot_id="boot-a")
    with pytest.raises(TypeError):
        session_probe(str(tmp_path), cfg, PROBE_OP, 1500, **base, mono_now=1.0)
    with pytest.raises(TypeError):
        probe_sweep(str(tmp_path), cfg, ROOT_TIMER, now=1.0, audit=audit, mode="expired", boot_id="boot-a")
    with pytest.raises(TypeError):
        probe_sweep(str(tmp_path), cfg, ROOT_TIMER, now=1.0, audit=audit, mode="expired", mono_now=1.0)


def test_r9_monotonic_record_without_mono_is_swept_as_expired(tmp_path: Path) -> None:
    cfg, audit = probe_cfg(), []
    assert probe(tmp_path, cfg=cfg, now=10_000.0, ttl=600, audit=audit)[0]
    (tmp_path / "fc-probe.probe.json").write_text(json.dumps({"expires_at": 10_600.0}), encoding="utf-8")  # legacy record
    assert probe_sweep(str(tmp_path), cfg, ROOT_TIMER, now=10_001.0, audit=audit, mode="expired", mono_now=1.0, boot_id="boot-a")


def test_r9_security_review_clause() -> None:
    c9 = section("### C9: ")
    assert "requires a dedicated security review of the then-current C9 implementation" in c9
    assert "R1 ships with every friend flag off" in c9
    for name in ("test_disable_ordering_fail_closed", "test_user_specific_sshd_binding", "test_all_host_keys_bound",
                 "test_check_create_reverify_is_one_operation"):
        assert f"`{name}`" in c9
    assert "`test_probe_sweep_requires_real_monotonic_clock`" in section("### C7h: ")


# ------------------------------------------------------------------ rev 10 (Sol 6 amd1 r9): context-dependent key settings

from .validation import sshd_context_problems, sshd_effective, sshd_keys_plan  # noqa: E402

MAIN = "/etc/ssh/sshd_config"
GLOBAL_OK = "AuthorizedKeysFile .ssh/authorized_keys /etc/ssh/flightctl-keys/%u\nPasswordAuthentication no\n"


def test_r10_sols_match_address_counterexample_is_refused() -> None:
    files = {MAIN: GLOBAL_OK + "Match Address 192.0.2.9\n    AuthorizedKeysFile /etc/ssh/other-keys/%u\n"}
    problems = sshd_context_problems(files)
    assert problems and "authorizedkeysfile" in problems[0]


def test_r10_include_hidden_match_is_refused() -> None:
    # the configuration measured on OpenSSH 10.5p1 (scratch, 2 Oct): Include-hidden Match Address
    files = {MAIN: GLOBAL_OK + "Include /etc/ssh/sshd_config.d/*.conf\n",
             "/etc/ssh/sshd_config.d/50-other.conf": "Match Address 192.0.2.9\n    AuthorizedKeysFile /etc/ssh/other-keys/%u\n"}
    assert sshd_context_problems(files)


def test_r10_unrelated_match_is_accepted() -> None:
    assert sshd_context_problems({MAIN: GLOBAL_OK + "Match Address 192.0.2.9\n    X11Forwarding no\n    AllowTcpForwarding no\n"}) == []
    assert sshd_context_problems({MAIN: GLOBAL_OK + "Match all\n    X11Forwarding no\n"}) == []
    assert sshd_context_problems({MAIN: GLOBAL_OK + "# Match Address 1.2.3.4\n# AuthorizedKeysFile /x\n"}) == []  # comments


@pytest.mark.parametrize("label,files", [
    ("Include inside a Match block", {MAIN: GLOBAL_OK + "Match Host lab-*\n    Include /etc/ssh/keys.conf\n",
                                      "/etc/ssh/keys.conf": "AuthorizedKeysFile /etc/ssh/other-keys/%u\n"}),
    ("keyword=value and mixed case", {MAIN: GLOBAL_OK + "match LocalPort 2222\n authorizedKeysFile=/etc/ssh/other-keys/%u\n"}),
    ("password login in a Match", {MAIN: GLOBAL_OK + "Match RDomain blue\n    PasswordAuthentication yes\n"}),
    ("PAM stack chosen per context", {MAIN: GLOBAL_OK + "Match Address 198.51.100.0/24\n    PAMServiceName other\n"}),
    ("principals via CA in a Match", {MAIN: GLOBAL_OK + "Match Group fc-*\n    TrustedUserCAKeys /etc/ssh/ca.pub\n"}),
    ("global directive after an included Match (sticky)", {MAIN: "Include /etc/ssh/a.conf\nAuthorizedKeysFile /x/%u\n",
                                                           "/etc/ssh/a.conf": "Match User x\n    X11Forwarding no\n"}),
    ("literal Include not available", {MAIN: GLOBAL_OK + "Include /etc/ssh/missing.conf\n"}),
    ("relative Include resolved under /etc/ssh", {MAIN: GLOBAL_OK + "Include rel.conf\n",
                                                  "/etc/ssh/rel.conf": "Match Address 1.2.3.4\n    PubkeyAuthentication no\n"}),
])
def test_r10_conditional_key_settings_refused(label, files) -> None:
    assert sshd_context_problems(files), label


def test_r10_include_without_match_and_empty_glob_are_fine() -> None:
    files = {MAIN: "Include /etc/ssh/sshd_config.d/*.conf\n" + GLOBAL_OK,
             "/etc/ssh/sshd_config.d/10-x.conf": "X11Forwarding no\nAuthorizedKeysFile .ssh/authorized_keys /etc/ssh/flightctl-keys/%u\n"}
    assert sshd_context_problems(files) == []
    assert sshd_context_problems({MAIN: GLOBAL_OK + "Include /etc/ssh/none.d/*.conf\n"}) == []  # sshd: no match is fine


def test_r10_file_set_is_bound_into_the_digest() -> None:
    files = {MAIN: GLOBAL_OK + "Include /etc/ssh/sshd_config.d/*.conf\n", "/etc/ssh/sshd_config.d/10-x.conf": "X11Forwarding no\n"}
    base = sshd_effective_digest(GLOBAL_T, {"fc-frienda": USER_OK}, files)
    changed = dict(files, **{"/etc/ssh/sshd_config.d/10-x.conf": "X11Forwarding yes\n"})       # any byte of any file
    added = dict(files, **{"/etc/ssh/sshd_config.d/20-y.conf": "Banner none\n"})                # a new included file
    assert sshd_effective_digest(GLOBAL_T, {"fc-frienda": USER_OK}, changed) != base
    assert sshd_effective_digest(GLOBAL_T, {"fc-frienda": USER_OK}, added) != base
    unrelated = dict(files, **{"/etc/ssh/not-included.conf": "anything\n"})                     # outside the Include set
    assert sshd_effective_digest(GLOBAL_T, {"fc-frienda": USER_OK}, unrelated) == base


def test_r10_sshd_T_keyword_case_measured_on_10_5() -> None:
    # measured 2 Oct 2026: OpenSSH 10.5p1 `sshd -T` prints mixed-case keywords
    measured = "Port 22\nAuthorizedKeysCommand none\nAuthorizedKeysCommandUser none\nAuthorizedKeysFile .ssh/authorized_keys /etc/ssh/flightctl-keys/%u\n"
    eff = sshd_effective(measured)
    assert eff["authorizedkeysfile"] == [".ssh/authorized_keys", "/etc/ssh/flightctl-keys/%u"]
    assert sshd_keys_plan(eff) == [".ssh/authorized_keys", "/etc/ssh/flightctl-keys/%u"]


def test_r10_proof_records_the_refusal_check() -> None:
    a, _ = two_hosts()
    inv = copy.deepcopy(examples("inventory")["valid"][0])
    missing = {k: v for k, v in PROOF7.items() if k != "no_conditional_key_settings"}
    assert_invalid(dict(inv, hosts=[dict(a, c9_proof=missing)]), "inventory")
    assert authority_admits_friend_work(site(True), dict(a, c9_proof=missing), STORE, NOW) == (False, "host has no valid C9 runbook proof")
    c9 = section("### C9: ")
    for name in ("review-obligation-stale-copy", "review-obligation-loaded-vs-file", "review-obligation-post-commit",
                 "test_context_dependent_key_settings_refused"):
        assert f"`{name}`" in c9


# ------------------------------------------------------------------ rev 11 (Sol 6 amd1 r10): aliases, runbook sync

from .validation import C9_PROOF_TRUE, SSHD_KEYWORD_ALIASES  # noqa: E402


@pytest.mark.parametrize("alias,canonical", [
    ("ChallengeResponseAuthentication yes", "kbdinteractiveauthentication"),   # Sol 6 amd1 r10
    ("SkeyAuthentication yes", "kbdinteractiveauthentication"),                # measured alias, not in the man page
    ("DSAAuthentication no", "pubkeyauthentication"),
    ("AuthorizedKeysFile2 /etc/ssh/other-keys/%u", "authorizedkeysfile"),      # deprecated; honoured by old releases
])
def test_r11_aliases_refused_directly(alias, canonical) -> None:
    problems = sshd_context_problems({MAIN: GLOBAL_OK + "Match Address 192.0.2.9\n    " + alias + "\n"})
    assert problems and canonical in problems[0], problems  # refused AS the canonical directive


def test_r11_alias_refused_when_include_hidden() -> None:
    files = {MAIN: GLOBAL_OK + "Include /etc/ssh/sshd_config.d/*.conf\n",
             "/etc/ssh/sshd_config.d/60-legacy.conf": "Match Host legacy-*\n    challengeresponseauthentication=yes\n"}
    problems = sshd_context_problems(files)
    assert problems and "kbdinteractiveauthentication" in problems[0]


def test_r11_unknown_keyword_in_match_is_refused_but_known_unrelated_ones_pass() -> None:
    assert sshd_context_problems({MAIN: GLOBAL_OK + "Match Address 192.0.2.9\n    FutureAuthOption yes\n"})  # fails closed
    assert sshd_context_problems({MAIN: GLOBAL_OK + "Match Address 192.0.2.9\n    PubkeyAcceptedKeyTypes ssh-ed25519\n    X11Forwarding no\n"}) == []
    assert sshd_context_problems({MAIN: "FutureAuthOption yes\n" + GLOBAL_OK}) == []  # global unknowns are not this check's business


def test_r11_alias_table_targets_are_real_keywords() -> None:
    for alias, canonical in SSHD_KEYWORD_ALIASES.items():
        assert alias != canonical
        assert canonical in v_match_or_global(), canonical


def v_match_or_global():
    from .validation import SSHD_MATCH_KEYWORDS
    return SSHD_MATCH_KEYWORDS | {"hostkey"}


def test_r11_runbook_matches_the_proof_schema() -> None:
    """Fails if C9 runbook step 8 (or the oracle's outcome list) drifts from what c9_proof's schema requires."""
    proof = json.loads((ROOT / "contracts/v2/inventory.schema.json").read_text(encoding="utf-8"))["$defs"]["host"]["properties"]["c9_proof"]["anyOf"][0]
    outcomes = {k for k, v in proof["properties"].items() if v.get("const") is True}
    assert outcomes == set(C9_PROOF_TRUE)  # schema outcomes == oracle outcomes
    c9 = section("### C9: ")
    step8 = c9.split("\n  8. ", 1)[1]
    for field in proof["required"]:
        assert f"`{field}`" in step8, f"runbook step 8 does not name c9_proof field {field}"
    assert f"all {['ONE', 'TWO', 'THREE', 'FOUR', 'FIVE', 'SIX'][len(outcomes) - 1]} outcomes" in step8
    for name in ("sshd_host_keys_sha256", "sshd_effective_digest", "ALL host public keys", "probe account", "every friend account",
                 "Include-expanded configuration file set"):
        assert name in step8, name
    step3 = c9.split("\n  3. ", 1)[1].split("\n  4. ", 1)[0]
    for name in ("the probe account `probe_account`", "each friend account", "Include-expanded configuration file set",
                 "host public key", "sshd_context_problems"):
        assert name in step3, name
