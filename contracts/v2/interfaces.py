"""Contract set v2: ports (typing.Protocol only, no behaviour).

Every port below has three implementations behind the SAME signature, chosen by the site adapters
file (contracts/v2/adapters.schema.json, ADAPTERS.md):

    fake    deterministic, in-memory, scriptable failures; sim profile and unit tests
    dryrun  REQUIRED for mutating ports: performs every real READ, validates the mutation, records the
            exact argv/target it would run as an event, returns the success-shaped reply with
            dry_run=True; never presented as a real effect
    real    talks to the host (subprocess, ssh, systemd, nvidia-smi, tailscale, SMTP/HTTP)

and one conformance module under tests/conformance/<port>/ that runs against fake always and against
dryrun/real when FLIGHTCTL_CONFORMANCE_TARGET names a host (marker onlab).

Failure semantics shared by every port (ADAPTERS.md section 4):
  * no method raises for an expected host/transport failure; it returns the documented result shape
    with ok=False / status 'unknown' and a typed error (common.schema.json#/$defs/typed_error) that
    keeps the lowest-level reason (Grok finding 7);
  * every call is bounded by an explicit timeout_s; a timeout is a typed 'timeout' error, never a
    hang and never an empty/free result (Amendment 2: this now holds for EVERY port method, Waker.wake and the
    ReleaseBackend methods included; the only exemption is Clock, whose reads are in-process with no I/O);
  * no hidden adapter state (Amendment 2, Sol 6.1 cold review P1-3): every identity a real twin needs to act
    on the right object (work id, lease id, lane id, parent lease) is an EXPLICIT argument and travels on the
    wire; an adapter never derives one identity from another through state it keeps itself;
  * unknown or unparsable output maps to unknown, never to empty, absent-and-safe or free.

Result shapes are JSON objects defined by the named schema definitions; Mapping[str, object] is used
so implementations can return plain dicts that validate against them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Mapping, Protocol, Sequence, TypedDict

Result = Mapping[str, object]


class CommandResult(TypedDict):
    """Outcome of one bounded command. returncode is None when the process did not finish."""

    argv: list[str]
    host_id: str | None  # None = local
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_s: float


# ---------------------------------------------------------------------------------------------- time

class Clock(Protocol):
    """Per-HOST clock. Real: datetime.now(UTC), time.monotonic(), /proc/sys/kernel/random/boot_id
    (never the process id: v1 RealClock bug, G27). Fake: SimClock; the sim profile gives EACH host
    its own SimClock with its own boot_id and skew so cross-host assumptions (G02) fail in tests.
    Conformance: tests/conformance/clock."""

    def utc(self) -> datetime: ...

    def monotonic(self) -> float: ...

    def boot_id(self) -> str: ...


# ------------------------------------------------------------------------------ commands and transport

class CommandRunner(Protocol):
    """The single seam through which real twins touch a host. Real: LocalCommandRunner (subprocess,
    no shell, argv list) and SshCommandRunner (ssh -o BatchMode=yes -o ConnectTimeout=N host -- argv,
    each element shell-quoted once). Fake: ReplayCommandRunner (golden captures recorded by the
    'record' wrapper on real hosts; unknown argv -> returncode None + 'no capture' stderr, never a
    default success). Conformance: tests/conformance/command_runner."""

    def run(self, argv: Sequence[str], *, timeout_s: float, stdin: bytes | None = None, host_id: str | None = None) -> CommandResult: ...


class ExecutorTransport(Protocol):
    """Authority -> per-host executor, one request/one reply (executor.schema.json).
    Real: SshForcedCommandTransport (dedicated key whose authorized_keys line forces the executor
    stdio entry point; JSON on stdin, JSON on stdout) and LocalSubprocessTransport (same entry point,
    same host). Fake: InProcessTransport binding an Executor object per host with its own SimClock.
    Returns {'status': 'ok'|'timeout'|'lost'|'denied'|'unparsable'|'failed', 'reply': executor reply or
    None, 'error': typed_error or None}. Only status 'ok' carries a reply; anything else is UNCERTAIN
    for mutating kinds (authority keeps exclusion). Conformance: tests/conformance/executor_transport."""

    def call(self, host_id: str, request: Result, *, timeout_s: float) -> Result: ...


# ------------------------------------------------------------------------------------------ workload

class WorkloadRunner(Protocol):
    """Transient user units on the lane host (unit.schema.json). Real: SystemdUserRunner over a
    local CommandRunner (systemd-run --user / systemctl --user show|stop / journalctl --user).
    Dryrun: inspect/logs real; start/stop return dry_run=True and record the argv. Fake: FakeRunner
    keyed PER UNIT, fail-closed on anything unscripted (Grok finding 5); a never-started unit is
    'absent' with an empty cgroup, exactly as real systemd reports LoadState=not-found.
    Round 2 (D-run-2): work that runs on behalf of agents or friends never runs as the executor's or the
    owner's account; the real twin for that case is HelperRunner ('sudo flightctl-helper unit-start' =
    systemd-run in the SYSTEM manager: agents under a per-job DynamicUser (round 5), friends with --uid=fc-<name>
    and --parent-lease; DevicePolicy=closed and
    DeviceAllow for the lane's cards). The user-manager twin remains for holder-mode bookkeeping and tests.
    Conformance: tests/conformance/workload_runner."""

    def start(self, unit: str, run_id: str, argv: Sequence[str], *, work_id: str, lease_id: str, lane_id: str,
              parent_lease_id: str | None, env: Mapping[str, str], run_as: str, workdir: str, cards: Sequence[str],
              grace_s: int, timeout_s: float) -> Result: ...
    # Amendment 2: work_id = the authority's public job id (helper unit-start --job), lease_id/lane_id = the lease the
    # work runs under, parent_lease_id = the friend's claimed parent lease (helper --parent-lease; None for agents).

    def stop(self, unit: str, invocation_id: str | None, *, timeout_s: float) -> Result: ...

    def inspect(self, unit: str, run_id: str | None, *, timeout_s: float) -> Result: ...

    def logs(self, unit: str, after_cursor: str | None, limit: int, *, timeout_s: float) -> Result: ...


# ------------------------------------------------------------------------------------------- probes

class InventoryProbe(Protocol):
    """What cards a host has (gpu-probe.schema.json#/$defs/inventory_observation). Real: nvidia-smi
    --query-gpu=index,uuid,pci.bus_id,name,memory.total,driver_version --format=csv,noheader,nounits
    plus /sys/bus/pci/devices/<bus>/numa_node, over a CommandRunner. Read-only: no dryrun twin.
    Fake: golden captures per card model. Conformance: tests/conformance/inventory_probe."""

    def inventory(self, host_id: str, *, timeout_s: float) -> Result: ...


class OccupancyProbe(Protocol):
    """Who is on a lane's cards now (gpu-probe.schema.json#/$defs/occupancy_observation), filtered to
    the lane's UUIDs, with the lane's noise rule applied (Amendment 2: only allow-listed desktop identities
    with a G or C+G context go to 'noise', within noise_cap_mib; every other process is a tenant whatever its size). Attribution to the lease is the executor's job. Real: the two
    nvidia-smi queries in gpu-probe.schema.json x-real-commands. Read-only: no dryrun twin.
    Conformance: tests/conformance/occupancy_probe."""

    def occupancy(self, host_id: str, lane_id: str, uuids: Sequence[str], *, noise_allowlist: Sequence[Mapping[str, object]],
                  noise_cap_mib: int, lane_noise_mib: int, timeout_s: float) -> Result: ...


class HealthProbe(Protocol):
    """HTTP health of a served endpoint, called by the executor on the lane host (loopback).
    Real: urllib GET with timeout. Fake: scripted. Read-only. Conformance: tests/conformance/health_probe."""

    def check(self, port: int, path: str, *, timeout_s: float) -> Result: ...


# --------------------------------------------------------------------------------------------- power

class Waker(Protocol):
    """Authority side. wake() sends the wake packet (single-flight per host is the authority's job)
    and returns a wake-attempt record (power.schema.json) immediately; answered() runs the cheap
    answer probe. Real: Wake-on-LAN UDP magic packet x3 from the configured send_from_host (the
    packet is a LAN broadcast) + 'ssh host true' with ConnectTimeout. Dryrun: answered() real,
    wake() records 'would send' and returns dry_run=True. Fake: SimHost sleep model with scripted
    wake latency / never-wakes. Conformance: tests/conformance/waker."""

    def wake(self, host_id: str, profile_ref: str, *, reason: str, timeout_s: float) -> Result: ...

    def answered(self, host_id: str, *, timeout_s: float) -> bool | None: ...


class Inhibitor(Protocol):
    """Executor side: keep this host awake for exactly the lease. Real:
    systemd-run --user --unit=flightctl-awake-<lane>-g<gen> --collect systemd-inhibit --what=idle
    --mode=block --who=flightctl --why=... sleep infinity (idle, not sleep: measured 2 Oct, polkit
    refuses sleep inhibitors over non-interactive ssh). Dryrun: list() real, hold/release recorded.
    Fake: in-memory set. Returns {'held': bool, 'unit': str, 'error': typed_error|None}.
    Conformance: tests/conformance/inhibitor (includes the linger check: does the inhibitor survive
    logout of the desktop session? WAKE-BRIEF Q3)."""

    def hold(self, lane_id: str, generation: int, *, why: str, timeout_s: float) -> Result: ...

    def release(self, lane_id: str, generation: int, *, timeout_s: float) -> Result: ...

    def list(self, *, timeout_s: float) -> Result: ...


# ------------------------------------------------------------------------------------------ identity

class PeerIdentity(Protocol):
    """Authority side: socket peer -> external ids. Real: 'tailscale whois --json <ip>:<port>'
    (Node.Name, Node.Tags, UserProfile.LoginName) over a local CommandRunner, cached for <= 60 s.
    Fake: scripted map; unmapped -> {'ok': False, error not_found}. Read-only.
    Conformance: tests/conformance/peer_identity."""

    def resolve(self, peer_addr: str, *, timeout_s: float) -> Result: ...


class Signer(Protocol):
    """Client side (approver workstation): produce an approval proof over the canonical signing
    bytes. Real: 'ssh-keygen -Y sign -n <domain> -f <sk key>' (FIDO2 touch). The browser path uses
    WebAuthn directly and is not a Python port. Fake: test-only software key, never accepted by a
    live authority (key ids from the fake are rejected unless profile is sim).
    Conformance: tests/conformance/signer."""

    def sign(self, signing_bytes: bytes, *, key_ref: str, namespace: str, timeout_s: float) -> Result: ...


# ------------------------------------------------------------------------------------- work support

class ModelCache(Protocol):
    """Lane-host cache of model files pulled from the central model store (storage.schema.json).
    ensure() is idempotent and non-blocking: it starts or polls a fetch and returns a cache-entry.
    Real: transient unit flightctl-stage-<lane>-g<gen> running rsync over ssh from the storage node
    into the cache root, then size (+sha256) verification; eviction LRU, never pinned or in-use files.
    Dryrun: list real, ensure/evict recorded. Fake: in-memory with scripted latency/failure.
    Conformance: tests/conformance/model_cache."""

    def ensure(self, host_id: str, model: Result, *, timeout_s: float) -> Result: ...

    def evict(self, host_id: str, model: Result, *, timeout_s: float) -> Result: ...

    def list(self, host_id: str, *, timeout_s: float) -> Result: ...


class SessionGateway(Protocol):
    """Open/close an interactive ssh session window for a friend's dedicated account on a lane host
    (session.schema.json; round 2, Sol 6 B5). Real: 'sudo flightctl-helper session-open|session-close'
    (slice C7h). Open sets user-<uid>.slice DevicePolicy=closed + DeviceAllow for exactly the lane's
    /dev/nvidia<minor> (device_minors) plus nvidiactl/uvm, then writes a managed authorized_keys block
    with expiry-time=, restrict, pty and a forced-command wrapper that sets CUDA_VISIBLE_DEVICES
    (convenience only; isolation is the device cgroup; authorized_keys environment= is not used because
    sshd ignores it without PermitUserEnvironment). Close removes the key, runs loginctl terminate-user
    and systemctl stop user-<uid>.slice, resets DeviceAllow, and returns a close_proof (user slice empty,
    key removed); the executor adds occupancy emptiness before the lane is freed. Gated on the owner's
    Q1 answer (OPEN-QUESTIONS). Conformance: tests/conformance/test_work_support.py session cases.
    Amendment 3 rev 2 (Sol 6.1 amd3): while the friend flags are off, open crosses the real wire and returns a typed
    'unavailable' refusal with no side effect, and close still works on an existing session. public_key is the
    canonical enrolled key that the authority resolved from the caller's fingerprint (resolve_session_key). An ok open
    reports the slice it configured (user-<uid>.slice). The executor records that slice against parent_lease_id and
    lane_id in its session registry, which is the only input to session attribution (session_attribution)."""

    def open(self, host_id: str, unix_user: str, public_key: str, *, parent_lease_id: str, lane_id: str, expires_at: datetime,
             device_minors: Sequence[int], cards: Sequence[str], timeout_s: float) -> Result: ...
    # Amendment 2: parent_lease_id is the helper's --parent-lease; two opens that differ only in it are different calls.

    def close(self, host_id: str, unix_user: str, key_fingerprint: str, *, parent_lease_id: str, timeout_s: float) -> Result: ...


class Notifier(Protocol):
    """Deliver one notification message (notification.schema.json). Real: SmtpNotifier (smtplib to
    the site relay, STARTTLS) and SlackWebhookNotifier (HTTPS POST to the site webhook). Dryrun:
    renders and records, sends nothing. Fake: outbox list. Conformance: tests/conformance/notifier."""

    def send(self, message: Result, *, timeout_s: float) -> Result: ...


# ------------------------------------------------------------------------------- shadow and release

class LegacyObserver(Protocol):
    """Shadow mode only: read the legacy lanes.sh lease file of a lane on its host, read-only and
    without taking its flock for longer than the read. Returns {'state': 'free'|'held'|'expired'|
    'unreadable'|'unknown', 'owner_label', 'purpose', 'expires_at', 'token_hint', 'error'}.
    'unreadable' is never 'free' (lesson 50c). Real: CommandRunner 'cat' of the state file.
    Fake: scripted. Conformance: tests/conformance/legacy_observer."""

    def read_lane(self, host_id: str, legacy_lane: str, *, timeout_s: float) -> Result: ...


class ReleaseBackend(Protocol):
    """Reduced v2 release backend (CONTRACT-RECOMMENDATION s5: 5 methods, not 15). Real:
    stage/activate/rollback of hashed artifacts on one host over a CommandRunner, backup copy to the
    configured backup target, restore rehearsal into a scratch root. Fake: the existing file backend.
    Conformance: tests/conformance/release_backend."""

    def stage(self, manifest: Result, *, timeout_s: float) -> Result: ...

    def activate(self, release_id: str, host_id: str, *, timeout_s: float) -> Result: ...

    def rollback(self, release_id: str, host_id: str, *, timeout_s: float) -> Result: ...

    def backup(self, release_id: str, *, timeout_s: float) -> Result: ...

    def restore_rehearsal(self, release_id: str, scratch_root: str, *, timeout_s: float) -> Result: ...
