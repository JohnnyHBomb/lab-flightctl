# ADAPTERS: every fake has a real twin, selected by configuration

The rule (the owner, 2 Oct): any fake built to avoid touching real lanes must have a real twin behind the same
interface. Switching between them is a configuration change: "flip a switch to real lanes". Shipping only
fakes is not acceptable.

## 1. Ports and their twins

`contracts/v2/interfaces.py` defines the ports. A real twin talks to the host through a `CommandRunner`
(local or ssh), so its parsing can be replayed against golden captures.

| Port | Side | Fake (sim, unit tests) | Dry-run twin | Real twin | Conformance module | Owed by |
| --- | --- | --- | --- | --- | --- | --- |
| Clock | every host | SimClock, one per host, distinct boot ids | n/a (read-only) | UTC, `time.monotonic`, `/proc/sys/kernel/random/boot_id` | `test_clock_and_commands.py` | A1 |
| CommandRunner | every host | Replay of recorded captures; an unknown argv is not success | n/a | Local subprocess (no shell), ssh BatchMode with ConnectTimeout | `test_clock_and_commands.py` | A2 |
| ExecutorTransport | authority | In-process executor per SimHost | n/a (pipe) | ssh forced-command stdio; local subprocess | `test_executor_transport.py` | A4 |
| WorkloadRunner | executor | Per-unit, fail-closed FakeRunner | inspect/logs real; start/stop recorded | `systemd-run --user`, `systemctl --user show/stop`, `journalctl --user`; work run for agents or friends goes through `flightctl-helper unit-start` (system manager, `--uid`, DevicePolicy/DeviceAllow) from C7h | `test_workload_runner.py` | A6 |
| InventoryProbe | executor, discovery | Golden captures per card model | n/a | `nvidia-smi --query-gpu=...` and sysfs `numa_node` | `test_occupancy_probe.py` | A3 |
| OccupancyProbe | executor, watcher | Golden captures with noise/tenant/fault cases | n/a | the two `nvidia-smi` queries in `gpu-probe.schema.json`; emptiness includes aggregate memory (emptiness_rule) | `test_occupancy_probe.py` | A3 |
| Inhibitor | executor | In-memory set | list real; hold/release recorded | `systemd-run --user ... systemd-inhibit --what=idle --mode=block sleep infinity` | `test_power.py` | A11 |
| Waker | authority | SimHost sleep model | answer probe real; packet recorded | WoL magic packet ×3 from `send_from_host`, then `ssh host true` | `test_power.py` | A12 |
| PeerIdentity | authority | Scripted map | n/a | `tailscale whois --json <ip:port>` | `test_identity_and_signing.py` | A7 |
| Signer | approver client | Test key, refused by a live authority | n/a | `ssh-keygen -Y sign` with an sk key (touch) | `test_identity_and_signing.py` | C3 |
| HealthProbe | executor | Scripted | n/a | loopback HTTP GET | `test_work_support.py` | C8 |
| ModelCache | executor | In-memory | list real; fetch/evict recorded | rsync over ssh from the store node in a transient unit, plus size/sha verify | `test_work_support.py` | C6a |
| SessionGateway | executor | In-memory | recorded | `flightctl-helper session-open/close` (C7h): user-slice DeviceAllow, managed `authorized_keys` with `expiry-time=` and a forced-command wrapper, close by `loginctl terminate-user` and a slice stop with proof | `test_work_support.py` (R1: refusal while the friend flags are off, close on a seeded session); `test_session_gateway_c9.py` (C9: open/close success, flags on) | C9w (fake and the REFUSING real twin over the real wire, registered in `impl_session_gateway.py`; Amendment 3 rev 2) + C9 (the helper-side bodies behind it, milestone D) |
| Notifier | authority | Outbox | rendered and recorded | SMTP (STARTTLS) to the site relay; Slack incoming webhook | `test_work_support.py` | C10 |
| LegacyObserver | authority (shadow) | Scripted | n/a | `cat` of the legacy lease file over a CommandRunner | `test_work_support.py` | A9 |
| ReleaseBackend | operator tool | Existing file backend | n/a | stage/activate/rollback/backup/restore rehearsal over a CommandRunner | `test_work_support.py` | B5 |

These do **not** earn a port (judgement): the SQLite store, which is always real and tested on a temp
file; the token store, which lives inside the authority store; the watcher, a loop over OccupancyProbe;
the endpoint request-accounting proxy, a component started as a unit; and PowerSignals for tariff and
battery, which is deferred until per-lane power is measured.

## 2. Selection by configuration

The site file `adapters.json` (`contracts/v2/adapters.schema.json`) lives in the site deploy directory,
never in the repo. The repo ships only `config/adapters.json.example`.

- `profile` sets the site default: `sim` (all fakes, one SimClock per host), `shadow` (real reads,
  dry-run writes, legacy authoritative), or `live`.
- `ports` picks `fake | dryrun | real | record` per port. `record` wraps the real twin and writes golden
  captures for the fake.
- `lanes.<lane>.mode` is `off | sim | shadow | live`, and may override the lane-critical ports. Flipping a
  lane to real is a one-line change from `shadow` to `live`. Flipping back is the reverse edit.
- At startup the authority and every executor print the effective selection per port and per lane,
  write an `adapter-config` event with the file's sha256, return it in `snapshot.adapter`, and show it as
  the web UI banner (for example `pilot-lane LIVE; second-lane SHADOW (dry-run executor writes)`).
  No fake is ever silent.
- One code path. The same modules run in every profile. Fakes live under `tests/fakes` and a `sim`
  package and are imported only through the registry.

## 3. Fail-closed rules (validated at startup; executable form: `tests/contracts_v2/validation.py adapters_semantics`)

Round 2 (Sol 6 B3) made required ports depend on enabled features, selected `command_runner` like every other
port, and turned shadow into a write prohibition across every mutating port.

1. Profile `live` refuses any `fake` port not listed in `allow_fake`. The refusal names the port.
2. **Features decide what is required.** `adapters.json.features` declares which capabilities are on.
   `adapters.schema.json` lists the ports each feature needs (`x-required-ports`). In `live`, a port required
   by an enabled feature must be `real`, and `allow_fake` may not list it. A disabled feature's ports may stay
   fake, and its RPC ops refuse with `unavailable`, naming the feature.
   Amendment 1: `friend_sessions` (default and R1 value: false) gates friend SSH sessions and friend-account jobs globally; a host also needs `inventory.hosts[].friend_sessions_enabled` with a recorded `c9_proof` (rev 7); `sessions` may be on only with it. The site file also carries the `tls` block (private-CA certificate files and rotation thresholds) for the A7 listener.
3. `allow_fake` may never list a lane-critical port: `command_runner`, `executor_transport`,
   `workload_runner`, `occupancy_probe`, `inhibitor`, `waker`.
4. A lane in mode `live` needs the site profile `live`. Its **effective** ports must all be `real`, meaning the
   global selection with the lane's own overrides applied: every lane-critical port and every port required
   by an enabled feature (round 3, Sol 6 B3). A lane override cannot reintroduce a fake. The waker is exempt
   when `wake_needed: false` (always-on host).
5. **Shadow is a host-write prohibition.** Every mutating port is lane-scoped: `workload_runner`,
   `inhibitor`, `waker`, `model_cache`, `session_gateway`, `notifier` and `release_backend`.
   - In a `shadow` lane, none of them may be `real` or `record`. `workload_runner`, `inhibitor` and `waker`
     must be `dryrun`, so their read sides still run.
   - `command_runner`, `executor_transport` and `occupancy_probe` must be `real`, so decisions see real state.
   - A site in profile `shadow` applies the same prohibition to every mutating port.
   - **One controlled exception** (round 3, Sol 6 B1): `lanes.<lane>.shadow_real: ["inhibitor"]` makes the
     inhibitor `real` on that shadow lane, so A-ASM can prove it while legacy `lanes.sh` still fences the lane.
     It is valid only on a shadow lane, only for the inhibitor, and only with `legacy_lane` set. It is removed
     before the flip.
   - The authority's own database records shadow decisions. That is not a host write.
   - Conformance check (A9): a recording CommandRunner and registry observe zero mutating calls from a
     shadow lane.
6. `dryrun` is valid only for mutating ports. Read-only ports have no dry-run twin.
7. The `sim` profile refuses `real`, `dryrun` and `record` ports.
8. A config that violates any rule stops startup with exit 3 and an `adapter-refused` event. The process
   never falls back to fakes.



## 4. Failure semantics every implementation shares

- No expected host or transport failure raises. The documented shape comes back with `ok: false` or
  `status: unknown`, plus a typed error that keeps the lowest-level reason.
- Every call has an explicit `timeout_s`. A timeout is the typed error `timeout`. It is never a hang
  and never an empty result.
- Unknown or unparsable output is `unknown`. It is never `empty`, never safely absent, never `free`.
- Idempotent operations stay idempotent: stopping an absent unit succeeds, releasing a released
  inhibitor succeeds, and an `ensure` for a present model starts no new fetch.
- A dry-run reply always carries `dry_run: true`, and the authority never turns it into a grant that
  a client sees as real.

## 5. Conformance suite

- There is one module per port under `tests/conformance/`. Each is parametrised over the
  implementations registered in `tests/conformance/registry.py`. A port with nothing registered is
  skipped with the slice that owes it.
- `fake` cases always run, in hosted CI and in lab-ci. `real` and `dryrun` cases are marked `onlab` and
  run only when `FLIGHTCTL_CONFORMANCE_TARGET=<host>` is set. Otherwise they show a visible skip.
- Real-time cases (`realtime` marker) use real seconds and real processes, never a fake clock. Every
  seam has at least one (lesson 63).
- An onlab run with `FLIGHTCTL_CONFORMANCE_EVIDENCE=<dir>` writes one dated JSON file per (port,
  implementation, host) recording the commit and the result of each case. The gauge cites those files
  (GATES.txt G3).
- Cross-port scenarios (the class of bug G02 was) run in the sim profile with one clock and one boot id
  per host, injected skew, and restarts. The same scenarios then run in shadow and live on the pilot
  lane as acceptance tests T01-T11.

## 6. What "done" means for an adapter (all must hold)

- **D1** The port is defined in `interfaces.py`, with typed results and the failure semantics above.
- **D2** The fake passes its conformance module.
- **D3** The real twin is in a production module and passes the same module on at least one real host
  of every host type it serves. The evidence file is cited by host, date and commit.
- **D4** Every mutating port has a dry-run twin that passes the read-side cases on a real host.
- **D5** The twin is selectable through `adapters.json` with no code change, the banner and event show
  it, and live mode is fail-closed.
- **D6** Errors are typed, map to unknown or quarantine (never to free), and every timeout is bounded.
- **D7** The operator doc says how to flip the switch, how to flip it back, and what to check afterwards.

A packet that adds a fake without its real twin is not done. Its brief must deliver both, or the slice
plan must put the real twin in the very next packet for the same port.
