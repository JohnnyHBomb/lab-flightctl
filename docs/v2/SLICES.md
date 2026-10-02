# lab-flightctl v2 slice plan (replaces the P1-P7 framing)

Contract set: `contracts/v2` (revision 2, after Sol 6's review of 2 Oct 2026). Proof gate: `docs/v2/GATES.txt`.
Traceability: `docs/v2/CONFORMANCE.tsv`. Checked test migration: `docs/v2/migration-map.tsv`. This plan is
portable: site roles below map to real hosts and lanes only in the external site map, kept outside the repo.

| Site role | Meaning |
| --- | --- |
| host C | always-on controller host; also owns the Titan-pair lane T |
| pilot host / lane P | sleeping host with a two-card lane; first to go live |
| store host / lane R | sleeping host with a 48 GB card; also the model store and deploy-directory node for now |
| desktop host / lane D | desktop-shared small card; standby-only for agents, external tenant = yield |
| approver | workstation with the FIDO2 key |

## Why the P1-P7 framing is replaced

P0-P7 built a correct library behind fakes. No package owned a running system (lesson 63), so the real
host seams were never exercised. v2 organises work by **seam to running system**. Every packet:

- delivers a fake **and its real twin**, or names the very next packet that delivers the twin;
- ships at least one real-time, real-process test for the seam it touches;
- is proven by the gauge on a real host before the next packet starts.

Three **assembly packets** (A-ASM, B-ASM, C-ASM) are owned by the lead and are never raced. They own the step
to a running system, which no P-package owned. **Every port an assembly needs is delivered by a packet that
comes before that assembly** (round-2 fix for the A-ASM cycle, Sol 6 B1). `tests/contracts_v2/test_v2_plan.py`
checks this mechanically.

## Rules every brief carries (do not repeat them in a race prompt; the launcher pastes them)

- The launcher pastes `STANDING.md` verbatim on every spawn and resume. The brief follows the lab's
  BRIEF.md shape. Evidence follows EVIDENCE.md.
- `contracts/v2/**` is frozen. A packet that needs a contract change stops and reports it. It never
  patches the contract itself; the lead amends the contract and Sol 6 re-reviews the amendment.
- Size: about 400 changed lines and at most 5 named acceptance tests (lesson 56). **The cap is subordinate
  to complete proof**: a packet that cannot be proven inside the cap is split before it is raced, never
  shipped half-proven. A5b and C7b are pre-split for that reason. Linux only. Python 3.12, stdlib, plus the
  declared test dependencies.
- No lab hostnames, IPs, usernames or paths in repo files. Site data goes in the site deploy directory.
- Every named test must fail on the base commit (G0). Existing tests are frozen, except the rows this
  packet owns in `migration-map.tsv` (G1, G1b).
- The packet passes G0-G5 in GATES.txt. The gauge, not the implementer, runs G2 (lab-ci full) and G3
  (onlab conformance, strict).
- Real-host work during development is read-only unless the brief says otherwise. Anything that starts a
  process on a GPU host runs only under a lane held through the current authority.

Seat shorthand: **O** = Opus (Exemplar reference), **L** = Luna-max, **S6** = Sol 6 (sol6-max), **A** = ASH-27B
Q4/Q8 + MTP, **Q** = Qwen3.8-27B + MTP.
Sizes are guesses: **S** is under one raced day, **M** is one to two days.

## Packet index

The `Ports` column lists the ports whose real twin this packet delivers. The plan check uses it to prove
each assembly's prerequisites are delivered before that assembly runs.

| ID | Packet | Milestone | Size | Depends on | Seats | Ports | Closes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A0a | lab-ci runs the FULL suite; hosted CI full; Linux only | A | S | - | lead | - | G17, Grok 9 |
| A0b | Adapter registry, features, adapters.json loader, fail-closed validation, banner/event | A | S | A0a | L, S6, A | - | ADAPTER-PRINCIPLE s2-3, Sol B3 |
| A0c | Sim rig: per-host clock/boot, in-process transport; strict-xfail repros | A | S | A0b | O, L, S6 | - | makes G02/G03/G27/Grok1/Grok7 failing tests |
| A1 | Clock real twin (/proc boot id) | A | S | A0b | A, Q, L | clock | G27 (boot part) |
| A2 | CommandRunner (local, ssh, replay, record), selected via adapters.json | A | S | A0b | L, A, Q | command_runner | base of all real twins |
| A3 | InventoryProbe + OccupancyProbe real twins; aggregate-memory emptiness; golden captures | A | S | A2 | L, S6, A | inventory_probe, occupancy_probe | G05 probe, Grok 3/4, Sol B2 |
| A3b | Inventory v2 (+device minor) + discovery v2 on the real probe | A | S | A3 | L, A, Q | - | G07, Grok 4 |
| A4 | Executor stdio entry point + local-subprocess and ssh forced-command transports | A | M | A1, A2 | O, L, S6 | executor_transport | G04 |
| A4u | Unit and timer templates, deploy-dir layout, config loader | A | S | A4 | L, A, Q | - | G04 timer, G24 |
| A5a | Executor v2 semantics (executor side), ceiling invariant | A | M | A0c, A4 | O, L, S6 | - | G02, G03 host side, Grok 1/7, Sol N1 |
| A5b1 | Authority executor client: identity before reserve, definite refusal, cause chain | A | M | A5a | O, L, S6 | - | Grok 1/7 |
| A5b2 | Authority beat loop + rolling renew within ceiling + max-end margin | A | M | A5b1 | O, L, S6 | - | G03, Grok 2, Sol N1 |
| A6 | WorkloadRunner real twin (systemd --user) + dryrun + per-unit fake; linger probe | A | S | A2 | L, S6, A | workload_runner | G05 systemd, Grok 5, D-pow-4 |
| A7 | Authority HTTPS listener (TLS terminated in-process) + PeerIdentity real twin | A | M | A5b2 | O, S6, L | peer_identity | G01, Sol B4 |
| A7b | RPC v2 envelope + /v1 adapter + snapshot + events since cursor | A | M | A7 | L, S6, O | - | G26, Grok 7 (RPC) |
| A8 | Per-lane reconcile on restart + quarantine-clear | A | M | A5b2, A7b | O, L, S6 | - | G27 |
| A9 | Shadow mode: evaluation, LegacyObserver, divergence + coverage report | A | M | A7b, A3 | O, S6, L | legacy_observer | shadow (owner 2 Oct), Sol N4 |
| A10 | Legacy shim core (v2 and tee modes) + hex16 tokens | A | S | A7b | L, A, Q | - | G06 (core) |
| A11 | Inhibitor real twin, held from reserve to verified release (was B1a) | A | S | A5a, A6 | L, S6, A | inhibitor | G13 (inhibitor), G14, Sol B1 |
| A12 | Waker real twin + wake-on-acquire (202 waking) + client and shim retry (was B1b) | A | M | A11, A7b, A10 | O, L, S6 | waker | G13, G22, Sol B1 |
| **A-ASM** | Assembly A: pilot lane shadow (coverage + 3 clean days), inhibitor proven under the legacy fence, then live | A | M | A0a-A12 | **lead** | - | milestone A |
| B2 | Holder liveness: heartbeat op, client heartbeat, stale-holder handling | B | S | A-ASM | L, S6, A | - | G28 |
| B3 | Shim at the legacy path for every caller; polling callers to wait; P4 obligations carried over; retire repo roster scripts | B | S | A10, B2 | L, A, Q | - | G06, G11, G12 (roster), Sol N6 |
| B4 | Watcher: collisions (aggregate memory), orphans, thermal refuse-new, incident events | B | S | A3, A7b | S6, L, A | - | G09 |
| B5 | Reduced real release backend + independent backup on host C + restore rehearsal | B | M | A4u | O, L | release_backend | G08 (reduced), G15, D-dep-2 |
| **B-ASM** | Assembly B: pilot lane production cut-over; lanes R and T into shadow | B | M | B2-B5 | **lead** | - | milestone B |
| C1 | Accounts: principals, groups, sponsors, whoami, disable, migration | C | S | B-ASM | L, S6, A | - | identity rows |
| C2 | API tokens bound to allowed peers | C | S | C1 | O, S6, L | - | agent tokens |
| C3 | FIDO2 signer real twin + approval v2 + approval matrix | C | M | C1, A7 | O, S6 | signer | G16 |
| C4a | Operator overrides by public id | C | M | C3 | O, L, S6 | - | G25, T11 |
| C4b | Windows and lane rules (maintenance, drain, quiet hours, class ceilings, yield) | C | S | C1 | L, S6, A | - | G19, drain |
| C5a | Usage records derived from events + usage query (was C5b) | C | S | C1 | L, A, Q | - | accounting, Sol B8 |
| C5b | Quotas in admission, GPU-hours from C5a's usage query (was C5a) | C | S | C5a | L, S6, A | - | Grok 6, G18, Sol B8 |
| C6a | ModelCache real twin + executor `stage`, sha256-verified | C | M | B-ASM | O, L, S6 | model_cache | model store/cache, Sol N2 |
| C6b | Storage document, model ops, cache policy, store-node wake | C | S | C6a | L, A, Q | - | model store/cache |
| C7h | flightctl-helper: the minimal root helper + sudoers rule + friend accounts and per-job DynamicUser isolation (spec and tests; install is an owner action) | C | M | C1 | O, S6 | - | Sol B5, D-run-2 |
| C7a | Templates + resolver + converter from the site model table | C | S | C6b | A, Q, L | - | packaging |
| C7b1 | Batch jobs: submit -> queue -> lease -> stage -> start as the job's own per-job DynamicUser (success path) | C | M | C7a, C7h, A6, C5b | O, L, S6 | - | G20, G12 |
| C7b2 | Batch jobs: failure paths (crash, lost, preempt, timeout, cancel) | C | M | C7b1 | O, L, S6 | - | G20 |
| C7c | Job logs + authenticated output retrieval (job-output-list/get) + notifications hook | C | S | C7b2 | L, A, Q | - | Sol B6 |
| C8 | Served endpoints + request-accounting proxy + health; retire legacy on-demand | C | M | C7a, C7h, A6 | O, L, S6 | health_probe | G11 chat, G21 |
| C9 | Friend SSH sessions: device-cgroup isolation + lease-bound stop (built and tested in R1, **not enabled**: flag off, Amendment 1) | C | M | C7h, C1, C7b1 | O, S6 | session_gateway | Sol B5 |
| C10 | Notifications: email and Slack real twins, subscriptions, warnings, quarantine re-alert | C | S | C1 | L, A, Q | notifier | notifications, D-pow-1 |
| C11a | Web UI, read-only (served by the authority over HTTPS) | C | M | A7b, C1 | O, S6 | - | FRONTEND 1 |
| C11b | Web UI, own actions + operator overrides + WebAuthn | C | M | C11a, C3, C4a | O, S6 | - | FRONTEND 2-3, Sol B4 |
| C12 | MCP server generated from x-ops | C | S | C2, C7c | L, A, Q | - | MCP |
| **C-ASM** | Assembly C: R1 release (friends and agents) | C | M | C1-C12 | **lead** | - | milestone C |

**Assembly prerequisites** (checked mechanically against the `Ports` column):

| Assembly | Needs the real twin of |
| --- | --- |
| A-ASM | clock, command_runner, inventory_probe, occupancy_probe, executor_transport, workload_runner, peer_identity, legacy_observer, inhibitor, waker |
| B-ASM | release_backend, plus everything A-ASM needs |
| C-ASM | signer, model_cache, health_probe, notifier, session_gateway (built and conformance-tested by C9, left disabled in R1 by the friend-sessions flag), plus everything B-ASM needs |

Later, after R1, unchanged from ROADMAP: energy per lease, tariff and battery scheduling, credits and fair
share, a controller move to the dedicated box, containers and Jupyter, reliability scores.

Crosswalk to ROADMAP.tsv. Round 5 (Sol 6 r4 B1 regression): this is a **scope map, not a schedule**. The schedule
is the packet index above: its order and its Milestone column. ROADMAP slot numbers are only labels for scope, and
ROADMAP's own order conflicts with the dependency order in places. For example, ROADMAP put deadline fixes (S01-S02)
before the executor entry point (S04), and wake (S09) after the S08 smoke. Entries below are listed in packet-index
order. `tests/contracts_v2/test_v2_plan.py` checks that order, and checks that every assembly comes after the packets
delivering its ports.

| Packet | ROADMAP scope |
| --- | --- |
| A0a, A0b, A0c | S00 |
| A1 | S01 (boot-id part) |
| A2 | (new: command seam) |
| A3, A3b | S03 |
| A4, A4u | S04 |
| A5a, A5b1, A5b2 | S01-S02 |
| A6 | S05 |
| A7, A7b | S06, S14 |
| A8 | S07 |
| A9 | S08 (shadow) |
| A10 | S10 (shim core) |
| A11, A12 | S09 (now delivered before the A-ASM assembly) |
| A-ASM | S08 (live flip) |
| B2 | S11 |
| B3 | S10 (all callers) |
| B4 | S26 |
| B5 | S12 |
| B-ASM | S13 |
| C1, C2 | S17 |
| C3 | S16 |
| C4a, C4b | S15 |
| C5a, C5b | S18 |
| C6a, C6b | S20 |
| C7h, C7a, C7b1, C7b2, C7c | S20, S21 |
| C8 | S23 |
| C9 | S22 |
| C10 | S24 |
| C11a, C11b | S19 |
| C12 | S25 |
| C-ASM | S27, S28 |



## Live acceptance tests runnable at each assembly (G6; Sol 6 B7)

| Test (ACCEPTANCE-TESTS) | Needs | Runs at |
| --- | --- | --- |
| T01 acquire/release with real tokens | A4-A8, A10 | A-ASM; re-run at B-ASM and per lane at C-ASM |
| T02 FIFO under contention | A7b, A10 | A-ASM; per lane at C-ASM |
| T03 bounded wait | A7b | A-ASM |
| T04 TTL expiry and renewal | A5a, A5b2 | A-ASM; per lane at C-ASM |
| T05 holder crash, current rule (no grant before max_end) | A5b2 | A-ASM |
| T05 holder crash, target rule (reclaim within 120 s) | B2 | B-ASM |
| T06a unreachable host | A7b | A-ASM |
| T06b sleeping host with the wake feature off (stays asleep/unreachable, never free) | A12 (feature switch) | A-ASM |
| T07 wake-on-acquire and inhibitor | A11, A12 | A-ASM; per sleeping lane at C-ASM |
| T08 controller restart | A8 | A-ASM |
| T09a/T09b unmodified callers through the shim at the legacy path | B3 | B-ASM |
| T10 status --all vs live GPU (collision/orphan) | B4 | B-ASM; per lane at C-ASM |
| T11a-c operator overrides | C4a | C-ASM |
| T11d FIDO2 forced preemption | C3, C4a | C-ASM |
| R1 scenarios (below) | C1-C12 | C-ASM |

---

## Briefs

Every brief below has the BRIEF.md fields. The common rules above apply to all of them. PROOF lists
what the gauge runs. `$LABCI` is the lab-ci entry point, `$EVID` is the packet's evidence directory,
`$HOST` is a host of the stated type. G3 always runs in strict mode for the packet's ports
(`FLIGHTCTL_CONFORMANCE_STRICT=1 FLIGHTCTL_CONFORMANCE_PORTS=<ports>`).

### A0a: lab-ci runs the full suite (gate for every packet)
- GOAL: Every packet's head commit can be qualified by lab-ci and hosted CI running the **whole** suite on Linux.
- SCOPE: `.github/workflows/ci.yml` (new job `full`, Linux only); lab-side `lab-ci/lab_ci.py` (generalise: `--workflow`, `--image`, job selection) and a Python 3.12 image definition. Owns nothing else.
- OBLIGATIONS: `full` runs `pytest -o addopts="" -q` over every test directory, then portability, then the external denylist, then the plan check (`tests/contracts_v2/test_v2_plan.py`). A step lab-ci cannot run is reported as skipped, never as passed. No Windows or macOS job.
- ACCEPTANCE: `ci_full_job_lists_every_suite`; lab-ci `full` on head returns exit 0 with a `summary.json` count equal to the local count; a deliberately failing test in a scratch branch makes lab-ci exit non-zero.
- PROOF: `$LABCI <checkout> --workflow .github/workflows/ci.yml --jobs full --out $EVID/labci`; count comparison; hosted run URL.
- SEATS: the lead (infra on lab tooling). Not raced.

### A0b: adapter registry, features and fail-closed selection
- GOAL: Production code selects every port implementation, `command_runner` included, from `adapters.json`. An invalid selection refuses startup and says why.
- SCOPE: `flightctl/adapters.py`, `tests/adapters/**`. CONTRACTS: `adapters.schema.json` (features, lane-scoped mutating ports), ADAPTERS.md s2-3. The executable rules are `tests/contracts_v2/validation.py::adapters_semantics`; production code must reproduce them without importing tests.
- OBLIGATIONS:
  - One registry keyed by (port, lane). Ports required by enabled features are never fake in live.
  - A shadow lane forces every lane-scoped mutating port to dryrun (no host write of any kind).
  - Banner and `adapter-config` event with the file's sha256. Refusal exits 3 and names the port and lane.
- ACCEPTANCE: `test_registry_selects_per_port_and_lane`; `test_feature_required_ports_never_fake_in_live` (EFFECTIVE per live lane, lane overrides included: Sol 6 r2 counterexample); `test_shadow_lane_has_no_real_mutating_port` (except the validated `shadow_real: [inhibitor]` proof exception); `test_banner_and_event_hash`; `test_cli_startup_refusal_exit3` [realtime: real subprocess].
- PROOF: G0-G2, G4. SEATS: L, S6, A.

### A0c: sim rig with one clock per host, and the bugs as failing tests
- GOAL: The measured integration bugs exist as failing tests before anyone fixes them (standing order 2).
- SCOPE: `tests/sim/**` (SimHost, SimClock per host with distinct boot ids and skew, InProcessTransport binding a real `Executor` per host). Migration row: the P6 assembled gate (`migration-map.tsv`).
- OBLIGATIONS: five `xfail(strict=True)` repros naming their fixing packet: cross-host reserve (A5a), heartbeat death (A5b2), renew-never-succeeds (A5b2), release identity mismatch and dropped reason (A5b1), restart freeze (A8).
- ACCEPTANCE: `test_sim_hosts_have_distinct_clocks`; `test_repro_cross_host_reserve`; `test_repro_beat_and_renew`; `test_repro_release_identity_and_reason`; `test_repro_restart_freeze`.
- PROOF: G0, G2. SEATS: O, L, S6.

### A1: Clock real twin
- GOAL: Every process reads its host's real boot id and monotonic clock; the pid boot id is gone.
- SCOPE: `flightctl/clock.py`, `RealClock` call sites in `flightctl/authority.py` (import swap only), tests.
- ACCEPTANCE: conformance clock cases [strict]; `test_boot_id_stable_across_process_restart` [realtime].
- PROOF: G3 on host C and the pilot host. SEATS: A, Q, L.

### A2: CommandRunner
- GOAL: One bounded, no-shell command seam for every real twin, selected through `adapters.json`, with record/replay so the fakes stay honest.
- SCOPE: `flightctl/commands.py`, `tests/fakes/replay.py`, capture format.
- OBLIGATIONS: ssh uses `-o BatchMode=yes -o ConnectTimeout=N` and quotes every argv element once. Exit 255 is the typed error `transport_failed`. A replay of an unknown argv is not success.
- ACCEPTANCE: command_runner conformance [strict]; `test_ssh_unreachable_is_typed_and_bounded` [realtime]; `test_record_then_replay_roundtrip`.
- PROOF: G3 local and over ssh to the pilot host (read-only). SEATS: L, A, Q.

### A3: GPU probes with aggregate-memory emptiness
- GOAL: Real `nvidia-smi` queries filtered by lane UUIDs; emptiness per `gpu-probe.schema.json` `emptiness_rule`. That means no tenants, no lease processes, and unexplained aggregate memory below `lane_noise_mib`. A process with unknown memory is a tenant (Sol B2).
- SCOPE: `flightctl/gpu.py`; golden captures under `tests/fakes/captures/gpu/`, one per lab card model, recorded read-only by the gauge with the A2 record wrapper; tests.
- OBLIGATIONS: Agree case-for-case with the oracle `tests/contracts_v2/validation.py::occupancy_from_capture` on `tests/contracts_v2/captures/gpu-occupancy.json`. Those captures are real, sanitised output from two Turing cards, plus synthetic edge cases.
  - Process rows parse as: first two fields, last field = memory, the name is everything between. Names can contain `, `.
  - Device minor comes from `/proc/driver/nvidia/gpus/<bus>/information`.
- ACCEPTANCE: occupancy and inventory conformance [strict]; `test_expected_uuids_come_from_confirmed_inventory` (round 5, Sol 6 r4 B2: the probe takes expected_uuids from the hash-verified confirmed inventory, never from its own output; a fabricated inventory hash is refused); `test_production_parser_agrees_with_oracle_on_all_captures` (including the process partition: every process is lease, noise or tenant; unknown memory is never noise and never empty; round 4: the observed cards are exactly the lane's `expected_uuids` from the confirmed inventory, none missing, extra or duplicated, and every process is on a lane card); `test_golden_captures_all_card_models`; `test_probe_real_process_timeout` [realtime].
- PROOF: G3 on hosts C, P, R and D (read-only). SEATS: L, S6, A.

### A3b: inventory v2 and discovery v2 on the real probe
- GOAL: Lanes bind to cards by UUID, bus, NUMA node and device minor. Discovery parses real probe output. The AMD path and the private CSV are gone.
- SCOPE: `flightctl/discovery.py`, `flightctl/inventory.py`; the migration rows for A3b.
- ACCEPTANCE: `test_v2_projection_takes_embedded_inventory`; `test_enabled_lane_requires_uuid`; `test_discover_against_replayed_real_captures`; `test_discover_live_readonly` [realtime, onlab].
- PROOF: G3 on the four hosts, read-only; the proposal file is the evidence. SEATS: L, A, Q.

### A4: executor entry point and transports
- GOAL: A real executor process answers the authority over a real pipe, both locally and through a forced-command ssh key.
- SCOPE: `flightctl/executor_stdio.py` (one-shot: JSON in, JSON out, persistent state), `flightctl/transport.py`, tests.
- ACCEPTANCE: executor_transport conformance [strict]; `test_one_shot_invocations_keep_state` [realtime]; `test_wrong_key_denied` [onlab]; `test_garbage_is_unparsable_not_ok`.
- PROOF: G3 local plus ssh to the pilot host (inspect only). SEATS: O, L, S6.

### A4u: units, timers, deploy-dir layout
- GOAL: Installable unit templates and a deterministic config loader, so assembly is copying files, not inventing them.
- SCOPE: `units/host/*` (executor enforcer timer every 60 s; inhibitor unit naming), `units/authority/*` (service plus cert-renewal timer, see A7), `flightctl/siteconfig.py` (hash-verified load from the deploy dir, verified local copy), `docs/v2/DEPLOY-LAYOUT.md`.
- ACCEPTANCE: `test_templates_render_without_site_strings`; `test_config_loader_rejects_hash_mismatch`; `test_local_copy_used_when_store_host_asleep`; `test_timer_unit_runs_one_shot` [realtime, onlab].
- PROOF: G3 on the pilot host. SEATS: L, A, Q.

### A5a: executor v2 semantics, ceiling invariant
- GOAL: The executor anchors relative deadlines on its own clock, keeps the identity fixed at reserve, extends on beat, refuses definitely or uncertainly, and returns typed errors.
- SCOPE: `flightctl/executor.py`, `tests/executor/**` (migration rows for A5a). CONTRACTS: `executor.schema.json`, `unit.schema.json`, `gpu-probe.schema.json`, `common#/$defs/relative_deadline` invariant.
- OBLIGATIONS: Holder mode reports the unit as `absent` and relies on the occupancy emptiness rule. An own-boot change means reconcile. The rest follow the contract invariant:
  - Max-end is set once and never increases. Only `extend` with an approval moves it.
  - A message older than `max_clock_skew_s` is refused as definite `clock_skew`.
  - Reserve takes the inhibitor first, then the fence (D-pow-3).
- ACCEPTANCE: `test_relative_deadline_anchored_on_host_clock`; `test_beat_extends_expiry_never_max_end`; `test_stop_requires_reserve_identity_and_empty_proof`; `test_definite_refusal_leaves_no_fence_and_no_inhibitor`; `test_enforcer_real_seconds` [realtime].
- PROOF: G0, G2, G4. SEATS: O, L, S6.

### A5b1: authority executor client, identity and errors
- GOAL: The authority assigns `run_id` and the unit before reserve, cancels on a definite refusal, keeps executor causes, and stores the lease v2 fields.
- SCOPE: `flightctl/authority.py` (reserve/stop calls, lease v2 fields), `flightctl/store.py` (columns, `token_sha256`).
- ACCEPTANCE: `test_release_identity_matches_reserve`; `test_definite_refusal_cancels_lease_lane_stays_free`; `test_executor_cause_reaches_rpc_error`; `test_reserve_stop_real_executor_process` [realtime]; `test_token_scrub_storage_level` (round 4, D-token-4: raw replay tokens live ONLY in a separate replay store that is never backed up; the main database holds `token_sha256` and token-free responses. On the authority's REAL files, a backup taken WHILE a token is live and examined after its deadline holds no token bytes, and neither do the replay db or its -wal after the scrub. Reference: test_v2_round4.py token tests, including the negative control); `test_grant_withheld_until_ceiling_acknowledged` (round 4, N1: the reserve reply carries `max_end_remaining_s`; no grant until reply_received + remaining <= approved_max_end; with the shorten message dropped the client sees `202 ceiling-unconfirmed` and no grant; round 5: only a reply bound to this reserve's lease_id, generation, host and controller_request_id counts; round 6: the reply must validate as the executor reply WIRE shape with nested `echoed_identity` and `host_boot`, and be `ok: true` and `definite: true`; round 7: a `reserve` reply with `observed_state: reserved` must come first, a `ceiling` reply counts only after it and in a fenced state, and a dry-run never counts; round 8: `test_ceiling_needs_persisted_fence`: the reply must carry the persisted fence for this lane and identity (exactly one fence on the lane, state equal to observed_state, not rebooted) and, where the lane holds an inhibitor, a held inhibitor named for the lane and generation; Sol 6 r7's reply with `fences: []` and `inhibitor: null` never grants); `test_split_store_crash_and_restore` (round 5: replay row committed first, main DB second; a crash between them leaves an orphan replay row that startup deletes and no grant; a main DB restored without its replay store answers a retry inside the window with 409 replay_unavailable and never grants twice; round 6: the token-free replay deadline is in the main record, so after the window a missing replay row is the routine scrub and the retry gets the grant replay with token null; round 7: a duplicate grant passes its real retry time, so after the deadline it gets the null-token replay even while the replay row still exists); `test_replay_checks_request_fingerprint` (round 8, Sol 6 r7: the same request id with a changed payload gets 409 conflict, never the stored response, inside or after the window and with or without the replay row; round 9, Sol 6 r8: idempotency and replay rows are keyed by (principal, request_id), so a second principal using the same request id and the same payload gets a fresh decision, never the first principal's lease or token).
- PROOF: G0, G2, G4; the A0c identity repro turns green. SEATS: O, L, S6.

### A5b2: beat loop and rolling renew
- GOAL: Every live lease is beaten every 60 s. Renew rolls the expiry within the ceiling. Max-end goes out as `approved_max_end - sent_at - margin`.
- SCOPE: `flightctl/authority.py` (beat loop, renew), migration rows for A5b2.
- ACCEPTANCE: `test_renew_rolls_within_ceiling_and_refuses_past_it`; `test_beat_loop_keeps_lease_alive_30min_sim`; `test_slow_round_trip_triggers_shorten_only_ceiling` (round 3, Sol 6 N1: 30 s skew + 60 s transport is detected from the authority's own RTT and corrected by a ceiling message; host max-end never later than approved once a round trip completes within the margin); `test_beat_loop_real_time` [realtime: real authority and executor subprocess, 3 s beats, 20 s].
- PROOF: G0, G2, G4; the A0c beat/renew repro turns green. SEATS: O, L, S6.

### A6: WorkloadRunner real twin and the linger probe
- GOAL: Real transient user units with per-unit identity. The v1 fake's success-by-default semantics are gone. Answer the linger question by experiment before A-ASM goes live (D-pow-4).
- SCOPE: `flightctl/runner.py`, `tests/fakes/runner.py`, migration rows for A6.
- ACCEPTANCE: workload_runner conformance [strict]; `test_fake_runner_per_unit_fail_closed`; `test_dryrun_never_starts` [onlab]; `test_crash_observed_and_cgroup_empty` [realtime, onlab]; `test_unit_and_inhibitor_survive_logout` [onlab; the result decides whether the owner enables linger on lane hosts].
- PROOF: G3 on the pilot host with `sleep` units only. SEATS: L, S6, A.

### A7: authority HTTPS listener with peer identity (Sol B4; Amendment 1: private CA)
- GOAL: A TLS listener bound to the controller's tailnet address only, terminating TLS **in the authority process** with a certificate issued by the site's **private CA** for `adapters.tls.server_name` (Amendment 1, owner Q2). The real socket peer address (`ip:port`) stays visible to the authority, so `tailscale whois` identifies the caller (unchanged). WebAuthn then has a secure origin (C11b), with `rp_id` = `tls.server_name`.
- DESIGN NOTES:
  - Why a private CA and not `tailscale cert` (Amendment 1): names issued by `tailscale cert` appear in public certificate-transparency logs, which would publish the lab's tailnet and host names. A private CA publishes nothing. The contract is CA-agnostic: a site process (a step-ca renewal daemon first; a Vault PKI agent later) writes `tls.cert_file`, `tls.key_file` and `tls.chain_file`; the authority only reads, verifies and reloads them.
  - TRUST ANCHOR (rev 2, Sol 6 amd1): `adapters.tls.trust_anchor.root_ca_files` (and, if set, `pinned_root_sha256`) are the only roots the served chain may terminate at. A chain rooted anywhere else, a public or system CA included, is refused.
  - Verification at start and on every change (oracle `tls_cert_decision`): the key matches the leaf, the chain verifies and terminates at a trust-anchor root (and a pin, if configured), `server_name` is an exact SAN (no wildcard certificates), and the certificate is inside its validity window.
  - What is served (oracle `tls_listener_action`): a good new set replaces the incumbent. A bad new set is refused with an alert, and the listener keeps the incumbent ONLY while the incumbent itself still passes `tls_cert_decision` under the CURRENT config (rev 3, Sol 6 amd1 r2: re-validated against the current `server_name`, trust-anchor root files, pins and time on every file change, every config change and every check interval; a removed root, a removed pin or a renamed server revokes a date-valid incumbent). When the incumbent expires with no valid replacement, the listener STOPS serving TLS (new handshakes refused, fail closed) and alerts; an expired certificate is never served. At start without a good set, the authority does not serve HTTPS.
  - Rotation: the authority checks every `tls.rotation.check_interval_s`; it alerts when remaining validity is at most `alert_before_s` (below `renew_before_s`, so an alert means the site renewer missed its window). Reload never drops the listener or established connections.
  - Clients (CLI, browser, agents) trust the private root: the CLI takes a `ca_file`; browsers and friend devices get the root installed (owner action). The root is never fetched over the connection it secures.
  - Rejected designs (unchanged): `tailscale serve` HTTPS (the backend sees a localhost peer; identity headers only for user-owned nodes, and the lab's nodes are tag-owned) and `tailscale serve` TCP with the PROXY protocol (workable, adds a parser for no benefit).
- SCOPE: `flightctl/server.py` (ssl context, verification, reload), `flightctl/identity.py` (PeerIdentity real twin and fake), `flightctl/client.py` (`ca_file`), tests. Local tests use a throwaway test CA.
- ACCEPTANCE: peer_identity conformance [strict]; `test_forwarding_headers_ignored`; `test_unmapped_peer_denied_no_mutation`; `test_tls_listener_real_socket_peer_reaches_whois` [realtime: real TLS listener on 127.0.0.1 with a test CA; the scripted whois receives the client's real ip:port]; `test_plain_http_refused_on_tls_port`; `test_cert_rotation_reloads_without_dropping_connections` [realtime: replace the files under a live connection; new handshakes get the new leaf]; `test_bad_cert_set_refused_previous_kept` (wrong SAN, expired, key mismatch, broken chain: each refused with an alert, previous certificate still served while valid); rev 2 (Sol 6 amd1 carried): `test_trust_anchor_only_configured_root` and `test_chain_rooted_elsewhere_refused` [realtime: a leaf from a second test CA, and one from a pin-mismatched root, are refused on a live listener]; `test_anchor_change_revalidates_incumbent` [realtime, live listener, rev 3: remove the incumbent's root from `trust_anchor`, then separately change its pin and the `server_name`, while the served certificate is still date-valid; with no valid candidate the listener stops TLS and alerts; with a valid candidate it serves the candidate]; `test_incumbent_expiry_stops_tls_and_alerts` [realtime, live listener: the served certificate expires with only a bad replacement on disk; new handshakes fail, an alert is raised, no expired certificate is ever presented].
- PROOF: G3, covering whois of the lead's workstation, an agent host and an unknown peer, plus an HTTPS request from a second host verifying against the private root only (no system CA). Second gauge: a security pass by Astra or Grok. SEATS: O, S6, L.

### A7b: RPC v2, the /v1 adapter, snapshot and events
- GOAL: `/v2/rpc` with typed results, `/v1/rpc` translated, a one-call snapshot, and an events feed with a since-cursor and long-poll.
- SCOPE: `flightctl/rpc.py`, authority read paths, `flightctl/store.py` (event `seq`), `flightctl/client.py`, tests.
- ACCEPTANCE: `test_every_op_validates_request_and_response`; `test_v1_adapter_translations`; `test_snapshot_redacts_and_never_wakes`; `test_events_since_seq_gapless`; `test_long_poll_returns_on_commit` [realtime].
- PROOF: G2, G4. SEATS: L, S6, O.

### A8: per-lane reconcile and quarantine-clear
- GOAL: After any authority restart, each lane reopens after an inspect-based reconcile. No global freeze. Operators clear quarantines with evidence.
- SCOPE: `flightctl/authority.py`, `flightctl/store.py`; the migration row for A8.
- ACCEPTANCE: `test_restart_reconciles_lane_by_inspect`; `test_unanswering_host_keeps_only_its_lane_unknown`; `test_quarantine_clear_requires_fresh_evidence`; `test_restart_real_process` [realtime].
- PROOF: G0, G2, G4; the A0c restart repro turns green. SEATS: O, L, S6.

### A9: shadow mode with coverage
- GOAL: A shadow lane makes every decision and every read for real and executes no host write. The divergence report counts mirrored decisions per path, and the lane cannot flip without reaching `policy.shadow.min_path_counts` (Sol N4).
- SCOPE: `flightctl/shadow.py`, `flightctl/legacy_observer.py`, the report command, tests.
- ACCEPTANCE: legacy_observer conformance [strict]; `test_shadow_never_calls_mutating_ports` (a recording CommandRunner and registry see zero writes from any mutating port); `test_flip_blocked_until_path_counts_and_no_mirror_errors`; `test_unreadable_legacy_is_not_free`; `test_shadow_watch_real_time` [realtime, onlab].
- PROOF: G3 on the pilot host (read-only). SEATS: O, S6, L.

### A10: legacy shim core
- GOAL: A client byte-compatible with legacy `lanes.sh`, with `legacy`, `tee` and `v2` modes per lane.
- SCOPE: `shim/lanes.sh` (thin) plus `flightctl/shim.py`; hex16 token issuance; vectors `tests/contracts_v2/vectors/legacy-shim.json`.
- ACCEPTANCE: `test_all_legacy_shim_vectors` (real shim process); `test_tee_mirror_failure_keeps_legacy_result`; `test_hex16_token_issued_and_principal_bound`; `test_shim_against_real_authority` [realtime].
- PROOF: G2, G4. SEATS: L, A, Q.

### A11: inhibitor held for exactly the lease (moved before A-ASM; Sol B1)
- GOAL: The executor takes an idle-block inhibitor before writing the fence and releases it only after verified release. A quarantine keeps it.
- SCOPE: `flightctl/power.py` (Inhibitor real twin, dryrun, fake); executor reserve/stop/reconcile hooks.
- ACCEPTANCE: inhibitor conformance [strict]; `test_inhibitor_failure_is_definite_refusal`; `test_reconcile_recreates_missing_and_removes_orphan_inhibitors`; `test_guard_sees_inhibitor` [realtime, onlab: the pilot host's sleep guard status lists it].
- PROOF: G3 on the pilot host. SEATS: L, S6, A.

### A12: wake on acquire (moved before A-ASM; Sol B1)
- GOAL: Acquiring a lane on a sleeping host wakes it and keeps queue order. A host that fails to wake is never reported free. The wake feature can be switched off per site (`features.wake`) for T06b.
- SCOPE: `flightctl/power.py` (Waker real twin, dryrun, fake); the authority's single-flight wake, `202 waking`, timeout to unreachable, and the rule that reads never wake; retries in the client and shim.
- ACCEPTANCE: waker conformance [strict]; `test_waking_is_202_and_fifo_kept`; `test_wake_timeout_unreachable_never_free`; `test_reads_never_wake`; `test_wake_real_pilot_host` [realtime, onlab, T07 shape].
- PROOF: G3 plus the T07 trace. The store host's wake on its routed link is an owner test (G23). SEATS: O, L, S6.

### A-ASM: assembly A (owned by the lead)
- GOAL: Real lanes on the pilot host, first in shadow, then live.
- STEPS:
  1. Set up the site deploy dir and confirm inventory v2 (probe, then owner review). Write `adapters.json` with the pilot lane in `shadow`, so every mutating port is dryrun.
  2. Install the executor (forced-command key, enforcer timer) on the pilot host and the authority on host C, over HTTPS with the private-CA certificate (Amendment 1 rev 7: prerequisite = the site CA has issued the cert, key and chain named in `adapters.tls`, and `tls.trust_anchor` names the root that clients were given). Set the shim to `tee` on host C.
  3. Shadow until `policy.shadow.min_path_counts` are met **and** 3 consecutive clean days have passed, with no mirror errors. Paths that do not occur naturally are exercised by the gauge through legacy `lanes.sh` with a dummy holder.
  4. **Prove the inhibitor while the legacy fence still stands** (round 3, Sol 6 B1: a controlled exception, not a contradiction of the shadow rule).
     - The lead sets `lanes.<pilot>.shadow_real: ["inhibitor"]` in `adapters.json`. The validator allows this exception for the inhibitor only, only on a shadow lane, and only with `legacy_lane` set. The lane stays in shadow: workload, waker and every other mutating port stay dryrun or non-writing.
     - Legacy `lanes.sh` remains authoritative throughout. The lead holds the legacy lease, and mirrored decisions now take and release a real idle inhibitor on the pilot host.
     - Evidence: `systemd-inhibit --list` before, during and after; the sleep guard status shows the inhibitor as protected work; the host suspends after the quiet grace once it is released.
     - Then remove `shadow_real` (back to all-dryrun), take the `adapter-config` event hash, and go to step 5. The exception never coexists with mode `live`.
  5. Answer the linger probe (A6) and apply the owner's action if needed.
  6. Rollback rehearsal.
  7. Flip: `adapters.json` lane to `live`, shim to `v2`, and the legacy writer disabled for the lane.
- ACCEPTANCE (G6): T01, T02, T03, T04, T05 (current rule), T06a, T06b, T07, T08, live on the pilot lane. Also the shadow coverage report and the strict conformance evidence set for every port in the A-ASM prerequisite row.
- OWNER: the lead. Owner actions: the forced-command key line; the private CA's certificate, key, chain and trust anchor for A7 (Amendment 1); linger only if A6 shows it is needed.

### B2: holder liveness
- GOAL: A crashed holder frees the lane within two heartbeats if its GPU is empty, and quarantines it otherwise.
- SCOPE: `flightctl/authority.py` (heartbeat op, stale handling), `flightctl/client.py` (heartbeat thread).
- ACCEPTANCE: `test_stale_holder_empty_gpu_releases`; `test_stale_holder_with_tenant_quarantines`; `test_ttl_only_legacy_unchanged`; `test_kill9_holder_reclaimed_within_120s` [realtime].
- PROOF: G2, G4; T05 target in B-ASM. SEATS: L, S6, A.

### B3: shim at the legacy path for every caller; P4 obligations carried over (Sol N6)
- GOAL: Every existing lab caller works unmodified through the shim. Polling callers move to `wait`. P4's two lasting obligations are re-proven on the shim/job path **before** the repo roster scripts and their tests are deleted:
  - token-bound cleanup: a late cleanup with an old token never releases a successor;
  - visible handoff: a registered batch manifest shows all arms, with successors ineligible.
- SCOPE: shim packaging and install doc; the B3 migration rows (deletion of `roster/*.sh` and `tests/roster/**`); lab-side edits listed and applied in B-ASM.
- ACCEPTANCE: `test_shim_cleanup_releases_only_own_token`; `test_batch_manifest_successors_visible_via_queue`; `test_callers_corpus_regexes`; `test_retired_roster_scripts_absent_and_docs_point_to_shim`; T09a/T09b [realtime, onlab, in B-ASM].
- SEATS: L, A, Q.

### B4: watcher
- GOAL: Continuous comparison of leases against GPUs, using the A3 rules: a collision when tenants exist or unexplained memory is at or above `lane_noise_mib`; an orphan when a lease is held, the lane is idle, and 15 minutes have passed. Also thermal refuse-new and incident events.
- SCOPE: `flightctl/watcher.py` plus a timer template.
- ACCEPTANCE: `test_collision_includes_aggregate_memory`; `test_thermal_refuse_new_never_kills`; `test_noise_never_flags`; `test_watch_real_time` [realtime, onlab].
- PROOF: G3; T10 in B-ASM. SEATS: S6, L, A.

### B5: reduced real release backend with an independent backup (D-dep-2)
- GOAL: Stage, activate and roll back on one host. Back up to **host C**, which is a different machine from the store host that holds the deploy dir. Rehearse a restore. Switch the backup target to the dedicated backup host when it exists.
- SCOPE: `deploy/flightctl_release.py` (backend switch), `deploy/backend_real.py`.
- ACCEPTANCE: release_backend conformance [strict]; `test_backend_selected_by_config`; `test_backup_target_differs_from_store_host`; `test_restore_rehearsal_hashes_match` [realtime, onlab].
- SEATS: O, L.

### B-ASM: assembly B (owned by the lead)
- GOAL: The pilot lane goes to production: the shim at the legacy path in `v2` mode for that lane, the legacy writer disabled, the watcher and backup running. Lanes R and T go into shadow.
- ACCEPTANCE (G6): T05 (target), T09a, T09b, T10, re-runs of T01, T04 and T07 through the shim, a rollback rehearsal, and the CUTOVER-RUNBOOK steps for one lane.

### C1: accounts
- SCOPE: `flightctl/accounts.py` (principal kinds, groups, sponsors, disable), whoami, migration of `identity_mapping`.
- ACCEPTANCE: `test_agent_requires_sponsor`; `test_disable_denies_new_admission_only`; `test_identity_mapping_migration`; `test_whoami_real_peer` [realtime].
- SEATS: L, S6, A.

### C2: API tokens
- SCOPE: token-create, token-revoke and token-list. A bearer token is accepted only from a peer in `allowed_peers`. Stored as a hash and shown once.
- BOUNDARY (round 4, Sol 6 r3 B6; oracle `auth_decision(peer_ids, accounts, token, now)`):
  - The request never carries a principal. It is derived from the socket peer's external ids, or selected by a token presented from an allowed peer.
  - An expired token (now >= expires_at) or a revoked token is denied.
  - A token from a peer not in the principal's allowed_peers or external_ids is denied, and there is no fallback to the peer's own principal for that request.
- ACCEPTANCE:
  - `test_request_cannot_supply_a_principal`: a principal or owner field in the request or headers is rejected or ignored, and the derived principal is used.
  - `test_token_from_unlisted_peer_denied_no_fallback`.
  - `test_expired_token_denied`: the expiry instant itself counts as expired; a revoked token is denied on the next request.
  - `test_scope_enforced_per_op_from_x_ops`.
  - `test_token_never_in_logs_events_replays`.
  - `test_bearer_real_https` [realtime].
- SIZE: six named tests, one over the cap. The cap is waived for proof (D-size-2); if a race entry is too large, split it into C2a (boundary tests) and C2b (lifecycle tests).
- PROOF: second-gauge security pass. SEATS: O, S6, L.

### C3: FIDO2 approvals and the approval matrix
- SCOPE: the Signer real twin (`ssh-keygen -Y sign` with an sk key); the approval v2 domain in `flightctl/auth.py`; evaluation of `policy.approval_matrix` with target and beneficiary class resolution (Sol's D-appr-1 note); `approve` without caller-supplied evidence.
- ACCEPTANCE: signer conformance [strict, onlab: a real touch]; `test_matrix_equal_class_needs_fido2_lower_does_not`; `test_matrix_resolves_target_and_beneficiary_class`; `test_v2_signing_bytes_vectors`; `test_replay_and_changed_target_denied`.
- SEATS: O, S6.

### C4a: operator overrides by public id
- SCOPE: op-stop, op-extend (sends the executor `extend` kind with the consumed approval), preempt by `target_lease_id` and beneficiary, queue-reorder, queue-remove and booking-cancel, checked per `x-ops`.
- ACCEPTANCE: `test_operator_ops_address_public_ids_only`; `test_non_operator_refused`; `test_reorder_equal_class_requires_approval`; `test_op_extend_moves_host_ceiling_only_with_approval`; `test_t11_shape_real_server` [realtime].
- SEATS: O, L, S6.

### C4b: windows and lane rules
- SCOPE: window-set and window-remove; drain and undrain; maintenance and owner-reserved overlap checks; weekly quiet hours; `class_ceiling`; `external_tenant: yield`. The yield rule is proven before the desktop lane is enabled (D-lane-3).
- ACCEPTANCE: `test_booking_cannot_overlap_maintenance`; `test_drain_running_continues_new_refused`; `test_quiet_hours_class_ceiling_local_time`; `test_desktop_lane_yields_to_external_tenant`.
- SEATS: L, S6, A.

### C5a: usage records and query (moved before quotas; Sol B8)
- SCOPE: usage records derived from events on close; a usage query over a rolling window (the source quotas use); the usage op; a weekly report export.
- ACCEPTANCE: `test_usage_reconciles_with_events`; `test_quarantine_time_charged_to_nobody`; `test_rolling_window_query`; `test_report_groupings`.
- SEATS: L, A, Q.

### C5b: quotas in admission (after usage; Sol B8)
- SCOPE: Evaluate quota documents: the minimum over the principal and its groups, sponsor charging, and exempt classes. GPU-hours come from the C5a query. A denial is 403 with the evaluation attached. An explicit quota `max_lease_s` replaces the class default (N3).
- ACCEPTANCE: `test_effective_limit_is_minimum`; `test_agent_charged_to_sponsor_groups`; `test_standby_exempt`; `test_rolling_gpu_hours_from_usage`; `test_quota_max_lease_replaces_class_default`.
- SEATS: L, S6, A.

### C6a: model cache with hash verification (Sol N2)
- SCOPE: the ModelCache real twin (a transient unit running rsync over ssh from the store host into the cache root); the executor `stage` kind. A cache entry becomes `present` only after sha256 verification. Size is progress only.
- ACCEPTANCE: model_cache conformance [strict, onlab: store host to pilot host, one small model]; `test_stage_idempotent_progress`; `test_same_size_changed_file_is_failed_not_present`.
- SEATS: O, L, S6.

### C6b: storage document and cache policy
- SCOPE: the storage document (sha256 required at publication); model-list, model-prefetch and cache-evict; LRU with pin and popularity; store-node wake before a fetch.
- ACCEPTANCE: `test_publication_requires_sha256`; `test_eviction_order_unpinned_unpopular_lru`; `test_in_use_never_evicted`; `test_fetch_waits_for_store_wake`.
- SEATS: L, A, Q.

### C7h: flightctl-helper, the minimal root helper (Sol B5; D-run-2)
- GOAL: A small root-owned helper that does all privileged steps for unit-mode work and sessions. The executor account can call it through ONE sudoers rule, limited to its fixed subcommands. No operator secrets are reachable by any work.
- SUBCOMMANDS (closed set, every argument validated against site config):
  - `unit-start` runs `systemd-run` in the SYSTEM manager with `-p DevicePolicy=closed`, `-p DeviceAllow=` for the lane's `/dev/nvidia<minor>` plus nvidiactl and uvm, and `-p KillMode=control-group`.
    - AGENTS (round 5, Sol 6 r4): `-p DynamicUser=yes -p StateDirectory=flightctl-jobs/<job> -p StateDirectoryMode=0700`. That is one ephemeral UID per job, with its staging under systemd's root-only private parent, so no agent job can read another's staging.
    - FRIENDS: `--account fc-<name> --parent-lease <lease_id>` with `--uid`. The parent must equal the account's claim.
  - `unit-stop`;
  - `session-open --account fc-* --parent-lease <lease_id> --devices ... --pubkey ... --expires ...`: ONE admission rule (round 8): while the account is quarantined, no claim of any kind is admitted (no new parent, no child of the old parent); otherwise a request is admitted only as the account's first parent claim or as a child of the claimed parent; the check and the session setup run under the account's flock; sets `user-<uid>.slice` DevicePolicy/DeviceAllow; writes the managed authorized_keys block;
  - `session-close`: removes the key, then `loginctl terminate-user` and `systemctl stop user-<uid>.slice`, resets DeviceAllow, and reports slice emptiness;
  - `claim-release --account --parent-lease`: the ONLY way a claim is removed. It needs that lease plus a close proof (user slice empty, occupancy empty, keys removed). It is race-safe (round 6): check on an `O_NOFOLLOW` fd, rename to a unique tombstone, delete only if the inode matches, otherwise restore and refuse.
  - `claim-reconcile --active <account=lease,...> --boot <id>`: run during A8 with the authority's active friend parents. It NEVER removes a claim (round 6, Sol 6 r5). A matching claim is kept. A claim the authority omits, for example after a restore that lost a still-running parent, becomes `orphaned-quarantined`; a claim naming another parent is a `conflict`. Both write a quarantine marker (under the account's flock) with an ADMISSION EFFECT (rounds 7-8): see the one admission rule below. The operator is alerted, and only `claim-clear` lifts the marker: a SEPARATE root-only program (`/usr/local/libexec/flightctl-claim-clear`, mode 0700) reached only through the operator's own sudoers rule without NOPASSWD, which checks SUDO_USER/SUDO_UID against `operator_account` and needs the close proof (round 8, Sol 6 r7; helper-config `x-operator-clear`). The executor's helper has no claim-clear. The claim is removed first and the marker last, so a crash leaves the account blocked. Claims live on persistent storage.
  - `output-collect --job <id>`: no account argument (round 6). It resolves the job in the helper's root-owned job registry to the UID recorded from the unit at start: the per-job DynamicUser for agents, `fc-<name>` for friends. It copies only single-link files owned by that UID.
  - `account-check`.
- ACCOUNTS:
  - Agents' unit work runs under systemd `DynamicUser`: a fresh UID per job, no persistent account, no lab secrets (round 5; the shared `fc-svc` account is withdrawn because one shared UID cannot keep agents' staging apart).
  - `fc-<friend>` is one account per friend. Created only after the owner's Q1 answer.
  - Linger is disabled on all of them. System-manager units do not need it.
- TRUST BOUNDARY (round 3, Sol 6):
  - The helper reads ONE fixed root-owned config, `/etc/flightctl/helper.json` (`contracts/v2/helper-config.schema.json`). It takes no config path, no environment and no free argument: account, lane, device minors, unit name and argv root are all allowlisted.
  - sudo grants exactly one rule: `<executor> ALL=(root) NOPASSWD: /usr/local/libexec/flightctl-helper`, with `env_reset`, `!setenv` and a `secure_path` of standard system directories with no empty component (round 7: `\:` separators count too). Example: `config/flightctl-helper-sudoers.example`.
  - Managed SSH keys live in the root-owned `/etc/ssh/flightctl-keys/<account>`, referenced by sshd `AuthorizedKeysFile` (an owner action). They are never written in a friend's home, so no attacker-controlled path or symlink is followed. Writes are `O_NOFOLLOW|O_CREAT|O_EXCL` to a temp file in that directory, then a rename.
  - Per host, ONE admission rule (round 8): while the account is quarantined, no claim of any kind is admitted (no new parent, no child of the old parent); otherwise a request is admitted only as the account's first parent claim or as a child of the claimed parent (B5 rule, defence in depth). Admission and start run under the account's flock, so a quarantine cannot land between them.
  - Amendment 1 (rev 2, Sol 6 amd1): helper.json `friend_sessions` is, per host, the global flag AND that host's inventory `friend_sessions_enabled` with a valid, host-bound `c9_proof` (rev 7/8; off in R1); the helper also carries `friend_sessions_global`, `friend_sessions_host` and `c9_binding` and refuses friend creation unless all agree and its live host facts match the binding. While it is off the helper refuses only the paths that CREATE friend work, `session-open` and `unit-start --account`; cleanup and safety paths stay available for friend work created earlier (`session-close`, `unit-stop`, `output-collect`, `claim-release`, `claim-reconcile`, read-only `account-check`; oracle `helper_subcommand_enabled`). Agent work is unaffected. The friend paths are still built and tested here.
- SCOPE: `helper/flightctl-helper` (Python, root-run, no shell), `helper/sudoers.example`, tests. Installation is an owner action (sudo).
- ACCEPTANCE:
  - `test_helper_rejects_every_argument_outside_site_config` (accounts, lanes, minors, unit names, argv[0] outside `argv_roots`, extra flags, environment);
  - `test_unit_start_argv_is_exact_systemd_run`;
  - `test_session_close_order_key_then_terminate_then_slice_stop`;
  - `test_key_write_never_follows_symlinks` (a symlink planted at the key path or the temp name is refused);
  - `test_agent_jobs_cannot_read_each_others_staging` [realtime, onlab, owner-installed: two concurrent DynamicUser job units A and B; B's attempt to open A's staging fails with EACCES; output-collect accepts only files owned by A's recorded UID]; `test_parent_claim_lifecycle` (a child of the claimed parent is accepted and any other lease refused; release only with the close proof; reconcile NEVER removes a claim; an omitted or conflicting claim quarantines the account, after which every request is refused, children of the old parent included, until an operator `claim-clear` with the close proof; no release by reboot; round 7); `test_install_audit` [onlab, on the INSTALLED artefacts]: run `sudoers_audit` (EFFECTIVE privilege, round 4) on `sudo -l -U <executor>` output and `sudoers_file_audit` on every installed sudoers file. Effective grants include %group and alias expansion: the executor must have exactly one grant, `(root) NOPASSWD: <helper>`. The parser follows the measured `sudo -l` format (sudo 1.9.17p2; fixture tests/contracts_v2/captures/sudo-l.json); run `helper_install_audit` on the stat chain from `/` to the helper, its config and the keys dir (root-owned, not group- or other-writable); the executor account fails to write, rename or replace each of them;
  - round 8 (Sol 6 r7): `test_claim_rollback_unlinks_only_its_own_inode` (a marker appears after link(); an interleaved operator clear and a replacement claim run before the rollback; the rollback must leave the replacement in place and return refused); `test_quarantine_and_start_are_serialised` (children racing claim-reconcile: every started child finished its start before the marker existed, none starts after); `test_claim_clear_requires_operator_sudo_path` (the helper refuses `claim-clear`; the clear program refuses non-root, a missing or different SUDO_USER/SUDO_UID, or another argv[0]); `test_install_audit` also runs `operator_sudo_audit` (no NOPASSWD route; round 9: EFFECTIVE `timestamp_timeout=0` for the program and no `!authenticate`) and `no_clear_route_audit` (executor, every friend), with round-9 route matching for ALL, wildcard paths (`/usr/local/libexec/*`), directory grants (`/usr/local/libexec/`), `^...$` regexes, argument-bearing grants and Cmnd_Alias expansion from the installed sudoers files (unknown alias = route); `test_claim_clear_prompts_every_time` [onlab, owner-installed, round 9: after a successful `sudo` by the operator, an immediate second claim-clear still prompts];
  - CARRIED OBLIGATIONS (freeze, Sol 6 round 9; the reference cases are strict-xfail tests in `tests/contracts_v2/test_v2_freeze.py`, owed by C7h):
    - `test_install_audit` must reject an EFFECTIVE `exempt_group` that contains the operator account (sudo then skips the password prompt despite the rule), and must parse and check the authentication tags (PASSWD/NOPASSWD) and SETENV PER COMMAND in a list, including tags inherited by, or changed after, a comma (for example `opr ALL=(root) /usr/bin/true, NOPASSWD: <claim-clear path>`), in both the effective (`sudo -l`) and the static (installed sudoers) audit. The wildcard, alias and group cases stay in this test;
    - `test_claim_clear_prompts_every_time` [onlab, owner-installed]: on the owner-installed sudoers rule, an immediate second invocation prompts after a successful first invocation;
    - `test_session_probe_bounds` (Amendment 1 rev 4, Sol 6 amd1 r3): the probe program (helper-config `x-operator-session-probe`) works with the flag OFF for `probe_account` only; it is refused for any other account, for a non-operator caller (wrong SUDO_USER/UID, or through the executor's helper), with the flag on, with a TTL over 900 s, and while an unexpired probe key exists; the key carries a Z-suffixed UTC `expiry-time` (rev 5: effective expiry <= issue + 900 s whatever the server's time zone) and is removed on expiry (wall clock OR monotonic clock, and always after a reboot: rev 8, a clock rollback cannot extend it; rev 9: `test_probe_sweep_requires_real_monotonic_clock`, the monotonic value and boot id are required inputs read from CLOCK_MONOTONIC and the kernel boot id, never defaulted), on revoke and on rollback by the root-only, remove-only `--sweep` (works as root without sudo variables; never touches a non-probe key; idempotent), while the add path is refused in that root context; each call is an audit event. `test_install_audit` also runs `operator_sudo_audit` and `no_clear_route_audit` with `program=/usr/local/libexec/flightctl-session-probe`;
    - `test_friend_flag_off_refuses_creation_allows_cleanup` (Amendment 1 rev 2, Sol 6 amd1 carried): with the flag off, session-open and unit-start --account are refused; a friend unit and session created before the flag was turned off can still be stopped, closed, collected and its claim released;
    - traceability acceptance: the status of CONFORMANCE row `sol6r8-B5` (now `specified`) and the two `carried` rows are part of C7h's review; they may become `contract-fixed` only when the cases above pass.
  - `test_device_allow_enforced` [realtime, onlab, owner-installed: inside a `DeviceAllow`-restricted unit, `os.open('/dev/nvidia<other minor>')` fails with EPERM and the lane's minor opens. Device nodes are opened only, no CUDA context, under a lane held through the current authority].
- SEATS: O, S6 (security packet; Astra or Grok as second gauge).

### C7a: templates
- SCOPE: template registry and resolver (whole-token params, no shell); a converter from the site model table.
- ACCEPTANCE: `test_resolver_whole_token_params`; `test_no_shell_metacharacter_expansion`; `test_converter_roundtrip_on_fixture_table`.
- SEATS: A, Q, L.

### C7b1: batch jobs, success path
- SCOPE: job-submit through the state machine to queue, lease, stage and start via `flightctl-helper unit-start` as a per-job DynamicUser (agents) or `fc-<friend>` with `--parent-lease` (after Q1); success, then release after the emptiness proof.
- ACCEPTANCE: `test_job_state_machine_success_transitions`; `test_job_runs_as_its_own_dynamic_user_never_operator_or_shared_uid`; `test_job_end_to_end_real_unit` [realtime, onlab: a CPU-only template on the pilot lane under a held lease].
- SEATS: O, L, S6.

### C7b2: batch jobs, failure paths
- SCOPE: crash leads to `failed` and the lane freed after proof; an uncertain stop leads to `lost` and a quarantine; preempt, timeout and cancel.
- ACCEPTANCE: `test_crash_marks_failed_and_frees_after_proof`; `test_uncertain_stop_marks_lost_and_quarantines`; `test_preempt_and_timeout_paths`; `test_cancel_real_unit` [realtime, onlab].
- SEATS: O, L, S6.

### C7c: job logs and authenticated output retrieval (Sol B6)
- SCOPE:
  - job-logs: the journal through the executor `logs` kind, owner-only.
  - job-output-list and job-output-get: chunked, each file sha256-verified, owner or operator only, path traversal refused, `202 waking` for a sleeping lane host, served through the executor `output` kind.
  - Trusted staging (round 4, Sol 6 r3 hard links):
    - The job writes only into a fresh per-job staging directory owned by the job account: the per-job DynamicUser UID for agents (its 0700 StateDirectory, unreachable by other agent jobs), or `fc-<friend>` for friends.
    - After the unit is stopped and its cgroup is empty, `flightctl-helper output-collect` copies into the executor-owned store only regular files with `st_nlink == 1` and `st_uid ==` the job account. It checks both with fstat on the `O_NOFOLLOW` fd.
    - Hard links, symlinks, devices and foreign files are rejected and listed.
    - Reads serve only those copies. Reference: `stage_outputs`.
  - Round 3 containment (Sol 6): the executor opens each output by walking from a directory fd of the output root with `O_NOFOLLOW` per component and serves regular files only. Absolute paths and empty, `.` or `..` components are refused before the walk (round 4). `get` carries the `expect_sha256` that `list` returned, and the executor refuses a file that changed in between. Reference: `contained_open` in `tests/contracts_v2/validation.py`.
  - Retention and wipe; the notification hook.
  - CLI `job fetch <job_id> [dest]` verifies hashes.
- FRIEND IDENTITY (round 3, Sol 6 B6, accounts contract): a friend is authenticated by their own tailnet identity, through a node share or invite resolved by whois. They need no host account and no shell. A token is optional and only narrows scopes; it is accepted only from the friend's allowed peers.
- ACCEPTANCE: `test_logs_owner_only`; `test_output_fetch_owner_only_and_traversal_refused`; `test_output_symlink_escape_and_list_get_swap_refused` [realtime, on real files: a symlink to a file outside the root, a symlinked directory, a file swapped between list and get, and an absolute path are all refused; a hard link planted in staging is never copied to the store]; `test_friend_client_fetches_outputs_without_shell` [realtime: a friend principal identified only by its tailnet login, with no host account, runs `job submit`, `job status` and `job fetch` against a real server; the files match; the same client from an unmapped peer is refused]; `test_output_retention_wipe`.
- SEATS: L, A, Q.

### C8: served endpoints
- SCOPE: endpoint-load and endpoint-unload (chat-* aliases); the generalised ChatController; the request-accounting proxy unit, run via the helper under a per-unit DynamicUser; the HealthProbe real twin; idle unload at 600 s; eviction drain; retiring the legacy on-demand front (lab side).
- ACCEPTANCE: health_probe conformance [strict]; `test_idle_unload_600s_completed_requests_only`; `test_eviction_drains_then_stops`; `test_endpoint_real_tiny_server` [realtime].
- SEATS: O, L, S6.

### C9: friend SSH sessions (built and tested in R1, NOT enabled; Sol B5; Amendment 1)
- SECURITY REVIEW BEFORE ANY ENABLEMENT (Amendment 1 rev 9): enabling friend sessions on ANY host requires a dedicated security review of the then-current C9 implementation (helper, authority gates, runbook tooling) before activation, independent of this contract review. R1 ships with every friend flag off: the global `features.friend_sessions`, every host's `friend_sessions_enabled` and every helper copy. Rev 10: the review must test the BUILT authority, helper, deployment read-back, running sshd and session behaviour, and must close three NAMED OBLIGATIONS (residuals accepted by Sol 6 amd1 r9, not closed by this contract):
  - `review-obligation-stale-copy`: during a disable, an unreachable host keeps its stale helper copy until reached; show that no direct local helper call can create friend work there, or accept and record the exposure;
  - `review-obligation-loaded-vs-file`: `sshd -T` reads the configuration files, so a running daemon whose loaded configuration differs from its files is not detected; show how the deployment proves the running daemon matches the bound files (for example a reload immediately before the proof and before each enablement);
  - `review-obligation-post-commit`: a configuration change after a creation's commit point affects only later creations; show the effect on already-open sessions is acceptable.
- R1 STATUS (Amendment 1, owner Q1 = (c) for R1): no friend SSH sessions and no friend shell jobs in R1. Friends get what agents get: API-queued jobs under a per-job DynamicUser. C9 is still built and passes its acceptance, but the global flag `features.friend_sessions` stays **false**, and so does every host's `friend_sessions_enabled`. Rev 7 (Sol 6 amd1 r6): enablement is PER HOST. A host offers friend sessions only when the global flag AND its own `inventory.hosts[].friend_sessions_enabled` are true, and the latter may be true only on an `openssh` host with a recorded runbook proof (`c9_proof`: proof id, artefact SHA-256, date, runbook revision, sshd verified, probe login ok, probe key removed). The authority (`authority_admits_friend_work`) and the host's helper both enforce it; the global flag never enables an unproven host. Rev 8 (Sol 6 amd1 r7): the proof is BOUND to its host (host id, sshd host key, machine-id and effective `sshd -T` hashes) and to an evidence artefact held in the authority's artefact store; admission compares the binding with the values the inventory probe last observed, so a copied proof, a re-imaged host or a changed sshd configuration is refused until the runbook is re-run. The helper carries the global flag, the host flag and the binding itself (`friend_sessions_global`, `friend_sessions_host`, `c9_binding`), treats either flag false as off, and re-measures the live host facts before every friend creation. Rev 9 (Sol 6 amd1 r8):
  - DISABLE is fail-closed (`friend_sessions_disable`): the authority refuses friend work at once; it writes every helper's `friend_sessions_global`/`friend_sessions` false and reads back the refusal; a host that cannot be reached stays disable-pending and gets NO work of any kind (`authority_admits_any_work`); the site flag is reported off only when no host is pending. Chosen over an authority-signed enable token because it adds no key material or crypto dependency to the root helper and no cross-host clock dependence. Residual: a pending host's helper keeps its stale copy until reached, exploitable only by a direct local call from the executor account, which the authority no longer drives.
  - The binding hashes ALL host public keys (`sshd_host_keys_sha256`) and the global AND per-relevant-user effective sshd settings (`sshd_effective_digest`: `sshd -T` plus `sshd -T -C user=<u>,host=<h>,addr=<a>` for the probe account and every friend account), so a `Match User`/`Match Group` change for a friend account invalidates it. Any changed bound setting, rotated or added host key, re-image or new friend account requires the FULL C9 runbook again (new artefact, proof, inventory observation and helper binding).
  - Check and creation are ONE helper operation under the account flock (`helper_create_friend_work`): measure, check, create, re-measure at the commit point, undo and refuse on any difference. Residual: `sshd -T` reads the configuration files, so a running daemon whose loaded configuration differs from its files is not detected.
- GOAL: A friend SSHes into the lane host for the lease window only.
  - The friend can open only the lane's GPU device nodes. This is enforced by the device cgroup, not by an environment variable.
  - When the lease ends, every process of that session ends, including established shells, `nohup` and `setsid` children, and the user manager.
  - The lane is freed only after the user slice and the lane's GPUs are proven empty.
  - PARENT and CHILD work (round 4, Sol 6 r3 B5): the parent is the friend's active lease on the host; a child is a session or job naming that lease_id.
    - At most one active parent per friend per host. A second parent, or a child naming another lease, is refused with `409 account_busy`.
    - Children of the active parent are allowed: a session under one's own lease is fine.
    - The authority inserts the parent under a UNIQUE (principal, host) active index in the admission transaction. The helper claims the same parent lease_id per account with `O_CREAT|O_EXCL`.
    - Both layers agree under concurrent requests (oracles `admit_parent` and `helper_claim`, race-tested with real processes).
- SCOPE: the SessionGateway real twin over `flightctl-helper session-open/close`; session-open and session-close; a warning before the end; idle close. `CUDA_VISIBLE_DEVICES` is set by the forced-command wrapper `flightctl-session-shell`. It is a convenience; enforcement is the device cgroup. OpenSSH `environment=` is not used, because `PermitUserEnvironment` is off by default (observed on host C).
- TEST SSHD (Amendment 1): C9's real-SSH acceptance runs against a DEDICATED test sshd started for the run (its own config file and host key, a non-standard port, a transient unit, owner-started because it needs root), never against the host's system sshd, whose configuration C9 does not change in R1. The test sshd uses the same `AuthorizedKeysFile` layout the runbook will install.
- ACCEPTANCE (real SSH against the test sshd, onlab, under a held lane, device nodes opened only):
  - `test_session_shell_cannot_open_other_card`;
  - `test_lease_end_kills_established_shell_and_nohup_child`: an ssh connection with an interactive shell, a `nohup sleep` and a `setsid sleep` all end, the connection drops, and the slice is empty;
  - `test_key_removed_and_relogin_refused`;
  - `test_close_proof_required_before_lane_free`;
  - `test_second_parent_refused_child_of_own_parent_allowed`: no access or termination change to the first item; concurrent parents race and exactly one wins in BOTH the authority and the helper;
  - session_gateway conformance [strict];
  - rev 10 (Sol 6 amd1 r9): `test_context_dependent_key_settings_refused` [onlab, on each host's real configuration file set: a `Match Address`/`Host` block (also one hidden behind `Include`) that sets any `SSHD_KEY_RELEVANT` directive makes the host ineligible; a Match block that changes only unrelated settings such as `X11Forwarding` is accepted; changing any configuration file invalidates the binding];
  - rev 9 (Sol 6 amd1 r8): `test_disable_ordering_fail_closed` (site flag turned off with one host unreachable: that host is disable-pending and receives no work, the site is never reported off while it is pending, and no reachable helper accepts session-open); `test_user_specific_sshd_binding` [onlab: a `Match User` change for a friend account that drops the managed key path makes both the authority (after the inventory probe) and the helper refuse]; `test_all_host_keys_bound` (rotating any one of several host keys invalidates the binding); `test_check_create_reverify_is_one_operation` (a change between the check and the commit undoes the creation);
  - rev 8 (Sol 6 amd1 r7): `test_helper_refuses_without_both_flags_and_live_binding` (helper flag true with the global flag false: session-open refused at the helper; every flag combination); `test_proof_bound_to_host` (host B presenting host A's proof is refused, also with the host id edited); `test_proof_invalidated_by_sshd_change_or_reimage` [onlab: change a harmless sshd setting, or rotate the host key, and both the authority and the helper refuse until the runbook is re-run]; `test_consistency_check_passes_before_activation` (a mixed deployment with only proven hosts on is valid);
  - rev 7 (Sol 6 amd1 r6): `test_per_host_gate_only_proven_host_accepts` (two OpenSSH hosts, global flag on, only one with `friend_sessions_enabled` and a valid `c9_proof`: the authority and that host's helper accept session-open there; the other host is refused by both, including when its helper.json is wrongly set, which the deploy check reports);
  - rev 4 (Sol 6 amd1 r3 carried): `test_authority_refuses_session_open_when_flag_off` (with `features.friend_sessions` false the AUTHORITY refuses a friend session request and a friend-account job before any helper call, naming the feature, on every host); `test_runbook_probe_login_with_flag_off` [onlab, owner-run, per OpenSSH host: step 7's probe login succeeds with the flag off, then the probe key is gone after revoke and, in a second run, after the rollback timer fires];
  - rev 2 (Sol 6 amd1 carried): `test_authority_refuses_friend_session_on_ineligible_host` (the AUTHORITY, not only the oracle, refuses session admission on a host whose inventory `ssh_server` is not `openssh`, with the flag on); `test_runbook_against_effective_sshd_config` [onlab, owner-run, per OpenSSH host before enablement: steps 3-4 run against that host's real `sshd -T` output, the verification passes, and the result is recorded].
- SIZE: six named items, over the 5-test cap; the cap is waived because every item is part of the B5 proof (D-size-2). Split into C9a (open, isolation) and C9b (close, single-active) if a race entry exceeds ~400 lines.
- SEATS: O, S6 (second gauge: security).
- HOSTS (Amendment 1, Q4; rev 7 per-host gate above): friend sessions may be enabled only on hosts whose system sshd answers (`inventory.hosts[].ssh_server == "openssh"`, oracle `c9_host_eligible`). Tailscale SSH ignores `authorized_keys` and authorizes by tailnet ACL, so the managed-keys design does not apply there. A Tailscale-SSH path (per-lease ACL `ssh` rules for `fc-<name>`, added and removed through the tailnet API with the same lease-bound stop and close proof) is NOT specified for R1 and needs its own reviewed amendment before any such host offers friend sessions. Hosts with `ssh_server` absent or `unknown` never offer them.
- PRE-ENABLE RUNBOOK (Amendment 1, Q4; owner-run; per host; before `friend_sessions` is turned on). The sshd change must not lock anyone out:
  1. Precondition: the host is `openssh`; C9 acceptance passed against the test sshd; the operator has console access to the host (local or out-of-band) confirmed working NOW, and a SECOND root session is open and stays open until step 7.
  2. Back up the current sshd configuration (the main file and every drop-in) to a dated root-owned copy.
  3. DISCOVER the effective configuration (rev 2, Sol 6 amd1): record `sshd -T` and, for every relevant user (the operator, the executor account, root, the probe account `probe_account`, and each friend account), `sshd -T -C user=<u>,host=<client name>,addr=<client tailnet address>`; also collect the Include-expanded configuration file set (every file reached from `/etc/ssh/sshd_config` through `Include`) and every host public key named by the effective `hostkey` lines. Keep `authorizedkeysfile`, `authorizedkeyscommand` and `authorizedkeyscommanduser` from each (oracle `sshd_effective`). Never assume `.ssh/authorized_keys`: the default effective value is `.ssh/authorized_keys .ssh/authorized_keys2`, and a host may use e.g. `/etc/ssh/keys/%u`. STOP if: a `Match` block gives any relevant user a different `AuthorizedKeysFile` or `AuthorizedKeysCommand` than the global one (Match blocks are not edited by this runbook); or the effective value is `none` (keys only via `AuthorizedKeysCommand`); either case needs its own review. Rev 10 (Sol 6 amd1 r9): sampling one client context per user cannot see a `Match Address`/`Host`/`LocalAddress`/`LocalPort`/`RDomain` block for ANOTHER permitted client (measured with OpenSSH 10.5p1 `sshd -T -C` on a scratch config: the sampled address kept the managed path while another address got a different `AuthorizedKeysFile` through an Include-hidden Match). So the host is REFUSED, not enumerated: STOP unless `sshd_context_problems` on that file set returns nothing (keyword aliases are canonicalised first, e.g. `ChallengeResponseAuthentication`/`SkeyAuthentication` are `KbdInteractiveAuthentication`, and any keyword in a Match context that is neither Match-permitted nor a known alias is refused; rev 11), i.e. no directive from `SSHD_KEY_RELEVANT` (key sources, principal/CA authorisation, `PubkeyAuthentication`, `RevokedKeys`, and the bypassing methods `AuthenticationMethods`, `PasswordAuthentication`, `KbdInteractiveAuthentication`, `PermitEmptyPasswords`, `PAMServiceName`, hostbased, GSSAPI, Kerberos) appears after any `Match` line or in a file included from a conditional context; an unresolvable literal `Include` also stops.
  4. WRITE a drop-in whose `AuthorizedKeysFile` is every previously effective path, in the same order, followed by the managed directory `/etc/ssh/flightctl-keys/%u` (oracle `sshd_keys_plan`). Never remove an existing path or change `AuthorizedKeysCommand`. Do not touch `PermitRootLogin`, `PasswordAuthentication` or any `Match` block. Run `sshd -t`, re-run `sshd_context_problems` on the new file set (the drop-in itself must land in a global context: if the `Include` that loads it comes after a `Match`, the scan refuses), then repeat the step-3 `sshd -T` commands and VERIFY (oracle `sshd_change_verified`) that for the global result and every relevant user the previous paths are still all present in order, the managed directory was added, and the `AuthorizedKeysCommand` settings are unchanged (sshd uses the FIRST value it reads, so a drop-in included after an existing `AuthorizedKeysFile` silently has no effect). On any failure, delete the drop-in and stop.
  5. Arm an automatic rollback BEFORE reloading: a transient timer (for example `systemd-run --on-active=10min --unit=flightctl-sshd-rollback ...`) that restores the backup, runs `sshd -t`, reloads sshd and removes any probe key with the ROOT-ONLY, remove-only `/usr/local/libexec/flightctl-session-probe --sweep --all` (rev 5: a root unit has no sudo context, so it cannot use the operator-only add path), unless it is cancelled. The per-minute `flightctl-session-probe-sweep.timer` (`--sweep --expired`) also stays active.
  6. RELOAD, never restart: `systemctl reload` of the sshd unit (existing sessions survive).
  7. PROBE LOGIN with the flag still OFF (rev 4, Sol 6 amd1 r3): `session-open` is refused while `friend_sessions` is off, so the managed-key proof uses the bounded probe: as the operator, `sudo /usr/local/libexec/flightctl-session-probe --account <probe_account> --pubkey-file <test key> --ttl 600` (sudo prompts; one key, `probe_account` only, expires by itself). From a NEW connection, log in as the operator with the existing key, then as `probe_account` with the probe key (the forced command answers `flightctl-probe-ok`). Only if both work: revoke the probe (`sudo /usr/local/libexec/flightctl-session-probe --sweep --all`) and cancel the rollback timer. Otherwise let the timer fire (it restores sshd and removes the probe key) and investigate from the still-open root session.
  8. Record the result (date, host, `sshd -T` excerpt, test logins, the probe's audit events) in the evidence directory, and confirm the probe key is gone. Only then, for THIS host only:
     (a) MEASURE the binding after the reload, with the reference digest functions and these inputs: `host_id`; `sshd_host_key_sha256` = `sshd_host_keys_sha256` over ALL host public keys named by the effective `hostkey` lines; `machine_id_sha256` = SHA-256 of /etc/machine-id; `sshd_effective_sha256` = `sshd_effective_digest` over the global `sshd -T` output, the `sshd -T -C user=<u>,host=<h>,addr=<a>` output for the probe account and every friend account, and the Include-expanded configuration file set;
     (b) store the evidence artefact in the authority's artefact store under a new `proof_id` and record its `artefact_sha256`;
     (c) set `inventory.hosts[<host>].friend_sessions_enabled: true` with its `c9_proof`: `proof_id`, `artefact_sha256`, `recorded_at`, `runbook_revision`, the binding (`host_id`, `sshd_host_key_sha256`, `machine_id_sha256`, `sshd_effective_sha256`) and all FOUR outcomes, each true: `sshd_verified`, `probe_login_ok`, `probe_key_removed`, `no_conditional_key_settings`;
     (d) write that host's helper.json `friend_sessions_global` (= the site flag), `friend_sessions_host: true`, `friend_sessions` (= both) and `c9_binding`;
     (e) run `friend_sessions_consistent` for every host: it MUST PASS before activation.
     The global `features.friend_sessions` is a separate owner decision and enables no host by itself (rev 7). Any later sshd configuration change or re-image invalidates the proof (both layers refuse) until this runbook is re-run (rev 8). This step's lists are checked against the `c9_proof` schema by `test_r11_runbook_matches_the_proof_schema` (rev 11).

### C10: notifications
- SCOPE: the Notifier real twins (SMTP to the site relay; Slack incoming webhook); subscriptions; lease-expiring and booking-reminder warnings; a quarantine re-alert every `policy.power.quarantine_realert_s` while a quarantined lane holds its host awake (D-pow-1); rate limits.
- ACCEPTANCE: notifier conformance [strict, onlab: one real email and one real Slack message to the owner]; `test_bodies_never_contain_secrets`; `test_delivery_failure_never_blocks_transition`; `test_quarantine_realert_interval`.
- SEATS: L, A, Q.

### C11a: web UI, read-only
- SCOPE: static pages served by the authority on the same HTTPS listener, with identity from the browser's socket peer: lanes, queue, bookings, events (long-poll), GPU observation and the adapter banner. Ideas from TensorHive only (DONOR-NOTES).
- ACCEPTANCE: `test_pages_render_snapshot_without_tokens`; `test_banner_shows_shadow_lanes`; `test_ui_served_same_tls_listener` [realtime].
- SEATS: O, S6.

### C11b: web UI actions and WebAuthn (secure origin from A7)
- SCOPE: own actions; operator overrides through C4a; the WebAuthn approval flow with `rp_id` = `adapters.tls.server_name` (the private-CA name, Amendment 1), verified by the existing `auth.py` P-256 path.
- ACCEPTANCE: `test_own_actions_only_for_owner`; `test_override_requires_webauthn_when_matrix_says`; `test_webauthn_assertion_verifies` (a fixture, plus one onlab run in a real browser against the HTTPS origin by the gauge).
- SEATS: O, S6.

### C12: MCP server
- SCOPE: an MCP server whose tools are generated 1:1 from the `x-ops` rows with `mcp=true` (job-output-list and job-output-get included). Each tool calls the RPC with the agent's token and has no extra privilege.
- ACCEPTANCE: `test_tools_equal_x_ops_mcp_rows`; `test_tool_call_uses_agent_identity`; `test_mcp_roundtrip_real_server` [realtime].
- SEATS: L, A, Q.

### C-ASM: assembly C, the R1 release (owned by the lead)
- GOAL: Friends and agents use timeslots.
  - Lanes R and T go live after clean, covered shadow periods. The Titan pair flips only in an idle window.
  - Lane D is enabled as standby-only for agents, after the yield proof.
  - Friend onboarding: the tailnet share and ACL, accounts, quotas, AUP.
  - Amendment 1 (Q1 = (c) for R1): friends use API-queued jobs under a per-job DynamicUser, exactly like agents. No friend SSH session or friend-account job in R1: `features.friend_sessions` is false, every host's `friend_sessions_enabled` is false, and C-ASM runs `friend_sessions_consistent` for every host (rev 7/8).
- ACCEPTANCE (G6):
  - On every newly live lane: T01, T02, T04, T07 (sleeping lanes) and T10.
  - T11a-d.
  - The R1 scenarios:
    - a friend books a slot, runs a job, fetches its output with `job fetch`, sees their usage, (no SSH session in R1: Amendment 1);
    - an agent does the same through MCP under its token;
    - the owner overrides from the HTTPS web UI with WebAuthn where the matrix requires it;
    - a sleeping host wakes for a booking;
    - a quota denial names its limit.
