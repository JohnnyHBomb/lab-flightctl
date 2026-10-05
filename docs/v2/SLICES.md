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
  declared test dependencies. Amendment 4: the cap is a hard gate for race entries; only a harvested merge may
  exceed it, with the count and each harvested item's cost reported (GATES G1, LINE CAP).
- Test directories (Amendment 4, lesson 71): every packet's SCOPE implicitly includes one change outside it. A
  packet that adds a new `tests/<dir>` holding `test_*.py` appends exactly the token `tests/<dir>` to the suite
  step of the `full` job in `.github/workflows/ci.yml`, and changes nothing else there (GATES G1, CI APPEND;
  the race gate's CIYML check enforces it).
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
| A3 | Part 1 (Amendment 5): OccupancyProbe real twin; aggregate-memory emptiness, noise by identity | A | S | A2 | L, S6, A | occupancy_probe | G05 probe, Grok 3, Sol B2 |
| A3i | Part 2 (Amendment 5): InventoryProbe real twin + golden captures per card model + replay-backed probe fakes | A | S | A3 | L, A, Q | inventory_probe | Grok 4, G05 fixtures |
| A3b | Part 1 (Amendment 9): inventory v2 (+device minor) + discovery v2 on the real probe, added beside v1 | A | S | A3i | L, A, Q | - | G07 |
| A3c | Part 2 (Amendment 9): retire v1 discovery (AMD path, private CSV, v1 proposal) | A | S | A3b | lead | - | Grok 4, D-amd-1 |
| A4 | Executor stdio entry point + local-subprocess and ssh forced-command transports | A | M | A1, A2 | O, L, S6 | - | G04 |
| A4u | Part 1 (Amendment 9): hash-verified site config loader + verified local copy + confirmed-inventory lane binding; deploy-dir layout | A | S | A4, A3 | L, A, Q | - | G24 |
| A5a | Part 1 (Amendment 9): executor v2 semantics in holder mode, in process (ExecutorV2: relative deadlines on the host clock, identity fixed at reserve, beat never moves max-end, emptiness proof before free, host enforcer); the v1 cross-host reserve fix | A | M | A0c, A4 | O, L, S6 | - | G02, G03 host side, Grok 1 |
| A5a2 | Part 2 (Amendment 9): inhibitor-first reserve and its definite/uncertain refusal (D-pow-3), request validation, inspect / ceiling / extend | A | S | A5a | O, L, S6 | - | Grok 7, Sol N1, D-pow-3 |
| A5a3 | Part 3 (Amendment 9): the v2 executor on the wire: the stdio entry point serves v2 through ExecutorV2, the enforcer one-shot, executor_transport conformance, the A4 test rows | A | S | A5a2 | O, L, S6 | executor_transport | G04 (v2 wire) |
| A4ub | Part 2 of A4u (Amendment 9): unit and timer templates (executor enforcer timer every 60 s; authority service and cert-renewal timer) + renderer | A | S | A4u, A5a3 | L, A, Q | - | G04 timer |
| A5b1 | Authority executor client: identity before reserve, definite refusal, cause chain | A | M | A5a3 | O, L, S6 | - | Grok 1/7 |
| A5b2 | Authority beat loop + rolling renew within ceiling + max-end margin | A | M | A5b1 | O, L, S6 | - | G03, Grok 2, Sol N1 |
| A5r | Retire executor v1 (Amendment 9): the v1 executor tests, their package-local systemd adapter and the v1 executor path | A | S | A5b2 | lead | - | MIGRATION (executor v1 rows) |
| A6 | Part 1 (Amendment 7): WorkloadRunner real twin (systemd --user) + linger probe | A | S | A2 | L, S6, A | workload_runner | G05 systemd, Grok 5, D-pow-4 |
| A6b | Part 2 (Amendment 7): WorkloadRunner dryrun twin + per-unit fail-closed FakeRunner + A6 migration rows | A | S | A6 | L, S6, A | - | Grok 5 |
| A7 | Authority HTTPS listener (TLS terminated in-process) + PeerIdentity real twin | A | M | A5b2 | O, S6, L | peer_identity | G01, Sol B4 |
| A7b | RPC v2 envelope + /v1 adapter + snapshot + events since cursor | A | M | A7 | L, S6, O | - | G26, Grok 7 (RPC) |
| A8 | Per-lane reconcile on restart + quarantine-clear | A | M | A5b2, A7b | O, L, S6 | - | G27 |
| A9 | Shadow mode: evaluation, LegacyObserver, divergence + coverage report | A | M | A7b, A3 | O, S6, L | legacy_observer | shadow (owner 2 Oct), Sol N4 |
| A10 | Legacy shim core (v2 and tee modes) + hex16 tokens | A | S | A7b | L, A, Q | - | G06 (core) |
| A11 | Inhibitor real twin, held from reserve to verified release (was B1a) | A | S | A5a2, A6 | L, S6, A | inhibitor | G13 (inhibitor), G14, Sol B1 |
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
| C10 | Notifications: email and Slack real twins, subscriptions, warnings, quarantine re-alert | C | S | C1 | L, A, Q | notifier | notifications, D-pow-1 |
| C11a | Web UI, read-only (served by the authority over HTTPS) | C | M | A7b, C1 | O, S6 | - | FRONTEND 1 |
| C11b | Web UI, own actions + operator overrides + WebAuthn | C | M | C11a, C3, C4a | O, S6 | - | FRONTEND 2-3, Sol B4 |
| C12 | MCP server generated from x-ops | C | S | C2, C7c | L, A, Q | - | MCP |
| C9w | Session PATHWAYS for C9, all refusing while the friend flags are off: SessionGateway real twin + fake, session RPC ops, key enrolment, executor session lifecycle, session attribution, authority friend gates, helper dispatch points, probe sweep, disable ordering, C9 seams with verified loading (Amendment 3, rev 2) | C | M | C7h, A7b, C1 | O, S6 | session_gateway | Amendment 3 (John's C9 decision) |
| **C-ASM** | Assembly C: R1 release (friends and agents) | C | M | C1-C12, C9w | **lead** | - | milestone C |
| C9 | Friend SSH sessions: the BODIES behind C9w's pathways, their on-lab proofs and the pre-enablement security review (session-enablement milestone D, NOT in R1; Amendment 3) | D | M | C9w, C-ASM | O, S6 | - | Sol B5, Amendment 3 |

**Assembly prerequisites** (checked mechanically against the `Ports` column):

| Assembly | Needs the real twin of |
| --- | --- |
| A-ASM | clock, command_runner, inventory_probe, occupancy_probe, executor_transport, workload_runner, peer_identity, legacy_observer, inhibitor, waker |
| B-ASM | release_backend, plus everything A-ASM needs |
| C-ASM | signer, model_cache, health_probe, notifier, session_gateway (the REFUSING real twin from C9w; Amendment 3: C9's working body comes after R1, in milestone D), plus everything B-ASM needs |

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
| A3, A3i, A3b, A3c | S03 |
| A4, A4u | S04 |
| A5a, A5a2, A5a3 | S01-S02 |
| A4ub | S04 (units and timers; after A5a3, Amendment 9) |
| A5b1, A5b2, A5r | S01-S02 |
| A6, A6b | S05 |
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
| C10 | S24 |
| C11a, C11b | S19 |
| C12 | S25 |
| C9w | S22 (pathways only, Amendment 3) |
| C-ASM | S27, S28 |
| C9 | S22 (bodies; milestone D, Amendment 3) |



## Live acceptance tests runnable at each assembly (G6; Sol 6 B7)

| Test (ACCEPTANCE-TESTS) | Needs | Runs at |
| --- | --- | --- |
| T01 acquire/release with real tokens | A4-A8, A10 | A-ASM; re-run at B-ASM and per lane at C-ASM |
| T02 FIFO under contention | A7b, A10 | A-ASM; per lane at C-ASM |
| T03 bounded wait | A7b | A-ASM |
| T04a TTL expiry and renewal, HOLDER variant (Amendment 2: D1, D2 and D3 as holder leases; D3's standby expiry ends the lease and no successor is granted before the occupancy emptiness proof; there is no unit to stop in holder mode) | A5a, A5b2 | A-ASM; re-run at B-ASM through the shim; per lane at C-ASM |
| T04b TTL expiry, MANAGED-UNIT variant (Amendment 2: D3 as a managed job unit; the executor stops the unit at max_end + grace and frees the lane only after the cgroup is gone and the emptiness proof) | C7b1, A5a | C7b1 (onlab); per lane at C-ASM |
| T05 holder crash, current rule (no grant before max_end) | A5b2 | A-ASM |
| T05 holder crash, target rule (reclaim within 120 s) | B2 | B-ASM |
| T06a unreachable host | A7b | A-ASM |
| T06b sleeping host with the wake feature off (stays asleep/unreachable, never free) | A12 (feature switch) | A-ASM |
| T07 wake-on-acquire and inhibitor | A11, A12 | A-ASM; re-run at B-ASM through the shim; per sleeping lane at C-ASM |
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
- Amendment 4 rev 2 (the lead's amendment on A0a's CI surface; migration-map rows owned by `contracts-v2`, the lead's contract stream that `test_b7_migration_gate_on_this_branchs_real_diff` gates): the `pr` job's Semgrep step scans `flightctl deploy tests` with a repo `.semgrepignore` that keeps `tests/` in (semgrep's built-in default list skipped it). The edited tests are covered by these collected nodes: `tests/integration/test_p6_scaffold.py::test_ci_security_configuration`, `tests/contracts_v2/test_v2_amendment3_boundary.py::test_seam_loader_rejects_before_import` and `tests/roster/test_roster_scripts.py::test_arm_fresh_lifecycle_uses_stateful_owner_and_injected_hooks`.

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
- OBLIGATIONS: five `xfail(strict=True)` repros naming their fixing packet: cross-host reserve (A5a), heartbeat death (A5b2), renew-never-succeeds (A5b2), release identity mismatch and dropped reason (A5b1), restart freeze (A8). Amendment 2 (Sol 6.1 P1-4): one file per acceptance test, exactly `tests/sim/test_repro_cross_host_reserve.py`, `tests/sim/test_repro_beat_and_renew.py`, `tests/sim/test_repro_release_identity_and_reason.py` and `tests/sim/test_repro_restart_freeze.py`, so the migration-map rows that let each fixing packet remove its mark (G1b) name them exactly.
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

### A3: GPU probes part 1: the OccupancyProbe real twin (Amendment 5 split, owner's approval 4 Oct)
- GOAL: Real `nvidia-smi` queries filtered by lane UUIDs; emptiness per `gpu-probe.schema.json` `emptiness_rule`. That means no tenants, no lease processes, allow-listed noise within `noise_cap_mib`, and unexplained aggregate memory below `lane_noise_mib`. A process with unknown memory is a tenant (Sol B2). Amendment 2 (Sol 6.1 cold review P1-1): noise is by IDENTITY, never by size: owner uid of `/proc/<pid>`, argv[0] of `/proc/<pid>/cmdline` and the `nvidia-smi -q -d PIDS -i <uuid>` context type (G or C+G) must match the lane's `noise_allowlist`; any other process is a tenant whatever its memory (T10's ~300 MiB stray CUDA context blocks admission).
- SCOPE: `flightctl/gpu.py` (the OccupancyProbe real twin), its real-only conformance registration, tests. Part 2 (A3i) owns the InventoryProbe, the golden captures and the fakes.
- OBLIGATIONS: Agree case-for-case with the oracle `tests/contracts_v2/validation.py::occupancy_from_capture` on `tests/contracts_v2/captures/gpu-occupancy.json`. Those captures are real, sanitised output from two Turing cards, plus synthetic edge cases.
  - Process rows parse as: first two fields, last field = memory, the name is everything between. Names can contain `, `.
  - Amendment 2 rev 2 (Sol 6.1 amd2): `test_desktop_user_legacy_jobs_remain_tenants` [onlab, read-only on a held lane; run by the gauge at G3 after A3i]: the desktop user's real legacy GPU jobs (type C, including the multi-card ones) are tenants even though they run as the allow-listed uid. TRUST BOUNDARY: an allow-list entry trusts that uid; no isolation is claimed from arbitrary code running as the trusted uid (it can forge argv[0] and hold a C+G context below the cap).
  - Amendment 2: process identity comes from `stat /proc/<pid>` (uid), `/proc/<pid>/cmdline` (argv[0] = the text before the first NUL) and `nvidia-smi -q -d PIDS -i <uuid>` (Type, per device entry; Amendment 5: the per-card form, because plain `-q -d PIDS` keys its sections by PCI bus id, which the occupancy query does not carry); an unreadable identity is null and never noise.
  - Amendment 5: an allow-list entry matches when argv0 equals it OR begins with it followed by a space (`argv0_matches`): a program that rewrites its command line into one string (a browser GPU process, measured) still matches its entry, and an executable path may contain spaces. Values nvidia-smi does not print (fractional, NaN or out-of-range numbers, pid 0) make the observation unknown; a context type outside C, G, C+G is null.
- ACCEPTANCE (part 1, four named tests): occupancy conformance [strict, real twin; G3 evidence needs a `status: ok` observation from the real host]; `test_production_parser_agrees_with_oracle_on_all_captures` (including the process partition: every process is lease, noise or tenant; unknown memory is never noise and never empty; round 4: the observed cards are exactly the `uuids` the caller passes, none missing, extra or duplicated, and every process is on a lane card; the probe never derives them from its own output); `test_small_unlisted_cuda_process_is_a_tenant`; `test_noise_needs_allowlisted_identity_and_cap`; `test_probe_real_process_timeout` [realtime]. Moved by Amendment 5: `test_golden_captures_all_card_models` to A3i; `test_expected_uuids_come_from_confirmed_inventory` to A4u (it needs the hash-verified confirmed inventory, `flightctl/siteconfig.py`, which A4u delivers).
- PROOF: G3 on hosts C, P, R and D (read-only). SEATS: L, S6, A.

### A3i: GPU probes part 2: the InventoryProbe real twin, golden captures and the probe fakes (Amendment 5)
- GOAL: The InventoryProbe real twin (x-real-commands `inventory`, `numa`, `device_minor`) and the replay-backed fakes of BOTH probes, built on golden captures recorded from real cards.
- SCOPE: the InventoryProbe in `flightctl/gpu.py`; golden captures under `tests/fakes/captures/gpu/`, one per lab card model, RECORDED read-only by the gauge with the A2 record wrapper (Titan RTX and RTX 8000 now; the T4 capture when its host is free, on the owner's word); the replay-backed probe fakes (`script_next`, `golden_captures`, `parse_capture`) and their conformance registration; the migration rows for `tests/fakes/fixtures/*.json`; tests.
- OBLIGATIONS: Device minor comes from `/proc/driver/nvidia/gpus/<bus>/information`; pci.bus_id is normalised to a 4-digit lowercase domain; NUMA from sysfs. A real capture of all three from host C is in the lab's research notes (2 Oct).
- ACCEPTANCE: inventory conformance [strict, real twin; G3 evidence needs a `status: ok` observation]; occupancy conformance on the fake (the fake_only cases); `test_golden_captures_all_card_models`; `test_desktop_user_legacy_jobs_remain_tenants` stays with the gauge at G3.
- PROOF: G3 on hosts C, P, R and D (read-only). SEATS: L, A, Q.

### A3b: inventory v2 and discovery v2 on the real probe (Amendment 9: part 1, measured in R-A3b staging; retiring v1 discovery is A3c)
- GOAL: Lanes bind to cards by UUID, bus, NUMA node and device minor. Discovery v2 builds a draft inventory v2 proposal from the A3i InventoryProbe's real observations and keeps a lane enabled only while every card it names is observed again with the same binding. v1 discovery stays until A3c.
- SCOPE: `flightctl/inventory.py` (new), additions to `flightctl/discovery.py` (no v1 line changes), `tests/discovery/conftest.py`, `tests/discovery/test_a3b_discovery.py`. No migration row.
- ACCEPTANCE: `test_v2_projection_takes_embedded_inventory`; `test_enabled_lane_requires_uuid`; `test_discover_against_replayed_real_captures`; `test_discover_live_readonly` [realtime, onlab].
- PROOF: G0-G2; the on-lab test read-only on every host type a lane can be held for; the proposal file is the evidence. SEATS: L, A, Q.

### A3c: retire v1 discovery: the AMD path, the private CSV and the v1 proposal (Amendment 9: part 2 of A3b; owned by the lead, not raced)
- GOAL: The AMD path and the private CSV are gone; the only discovery is v2 (A3b).
- SCOPE: `flightctl/discovery.py` (delete the v1 code), `tests/discovery/test_discovery.py`, `tests/fakes/fixtures/amd.json`; the migration rows for A3c. Its two rewrite rows name their replacements as the exact nodes `tests/discovery/test_a3b_discovery.py::test_discover_against_replayed_real_captures` and `tests/discovery/test_a3b_discovery.py::test_v2_projection_takes_embedded_inventory`, because a bare name counts only in a test file the same packet adds or modifies (`tools/migration_gate.py`, round 5). Production wiring of `flightctl discover` to `propose_v2` if the lead wants it here.
- PRECONDITION: Amendment 9, change 4 (`tests/contracts_v2/test_v2_round3.py::test_b7_migration_gate_on_this_branchs_real_diff` rescoped); before it, no packet could use its own rows on a test file that existed at `59bdd7f`.
- PROOF: G0-G2. SEATS: the lead (deletions dictated by the migration map: no design freedom to race, and LINES counts deletions: 196 + 71 + up to 333 removed test lines, measured).

### A4: executor entry point and transports
- GOAL: A real executor process answers the authority over a real pipe, both locally and through a forced-command ssh key.
- SCOPE: `flightctl/executor_stdio.py` (one-shot: JSON in, JSON out, persistent state), `flightctl/transport.py`, tests in `tests/transport/test_a4_transport.py` (Amendment 6: that exact path, so A5a's migration rows have a defined target).
- ACCEPTANCE: `test_one_shot_invocations_keep_state` [realtime]; `test_wrong_key_denied` [onlab]; `test_garbage_is_unparsable_not_ok`; `test_transport_failures_are_typed_and_bounded` [realtime]. Amendment 6: executor_transport conformance [strict] moved to A5a (the frozen cases assert v2 executor replies, which A5a delivers); Amendment 9: to its part 3, A5a3.
- PROOF: G3 local plus ssh to the pilot host (inspect only). SEATS: O, L, S6.

### A4u: site deploy directory and config loader (Amendment 9: part 1; the unit and timer templates are A4ub)
- GOAL: A deterministic config loader, so assembly is copying files, not inventing them: a host uses the site files only when their sha256 matches the published manifest, and keeps a verified local copy so it starts while the store host is asleep.
- SCOPE: `flightctl/siteconfig.py` (hash-verified load from the deploy dir, verified local copy, the confirmed-inventory lane binding), `docs/v2/DEPLOY-LAYOUT.md`, tests in `tests/siteconfig/`.
- ACCEPTANCE: `test_config_loader_rejects_hash_mismatch` [realtime]; `test_local_copy_used_when_store_host_asleep`; Amendment 5 (moved from A3): `test_expected_uuids_come_from_confirmed_inventory` (the lane's card UUIDs handed to the occupancy probe come from the hash-verified confirmed inventory this packet loads, never from the probe's own output; a fabricated inventory hash is refused; prerequisite A3: the probe takes `uuids` from its caller).
- PROOF: G0-G2, G4. SEATS: L, A, Q.

### A5a: executor v2 semantics, ceiling invariant (part 1 of 3, Amendment 9)
- GOAL: The executor anchors relative deadlines on its own clock, keeps the identity fixed at reserve, extends on beat but never moves max-end, frees a lane only on an emptiness proof, refuses definitely or uncertainly, and returns typed errors, in process (`ExecutorV2`). Amendment 9: the inhibitor, request validation, inspect/ceiling/extend (A5a2) and the wire (A5a3) are later parts.
- SCOPE: `flightctl/executor.py` (new class `ExecutorV2` beside the v1 `Executor`, which stays until A5r retires it; the one v1 change: the cross-host boot-id refusal in `_reserve` is deleted), new files under `tests/executor/`, the mark of `tests/sim/test_repro_cross_host_reserve.py` (migration row). CONTRACTS: `executor.schema.json`, `unit.schema.json`, `gpu-probe.schema.json`, `common#/$defs/relative_deadline` invariant.
- OBLIGATIONS: Holder mode relies on the occupancy emptiness rule. An own-boot change means reconcile. Max-end is set once at reserve and a beat never moves it. A message older than `max_clock_skew_s` is refused as definite `clock_skew`. `tests/executor/test_mutations.py`'s 13 v1 lines stay exactly once.
- ACCEPTANCE: `test_relative_deadline_anchored_on_host_clock`; `test_beat_extends_expiry_never_max_end`; `test_stop_requires_reserve_identity_and_empty_proof`; `test_enforcer_real_seconds` [realtime]; the A0c repro `test_repro_cross_host_reserve` turns green (mark removed, assertions kept).
- PROOF: G0, G2, G4. SEATS: O, L, S6.

### A5a2: executor v2 part 2: the inhibitor, request validation, inspect/ceiling/extend (Amendment 9)
- GOAL: Reserve takes the inhibitor first, then the fence (D-pow-3); a definite refusal leaves no fence and no inhibitor, and a failed release makes it uncertain. Every request is validated (definite `invalid`, nothing written). Inspect reports the holder-mode unit as absent and the lane's occupancy; `ceiling` only shortens max-end; only `extend` with an approval moves it later.
- SCOPE: `ExecutorV2` in `flightctl/executor.py` (an injected Inhibitor port; validation; inspect, ceiling, extend), new files under `tests/executor/`.
- ACCEPTANCE: `test_definite_refusal_leaves_no_fence_and_no_inhibitor`; `test_ceiling_shortens_only_and_extend_needs_approval`; `test_invalid_requests_are_definite_and_write_nothing`; `test_inspect_reports_holder_unit_absent`.
- PROOF: G0, G2, G4. SEATS: O, L, S6.

### A5a3: executor v2 part 3: the wire and executor_transport conformance (Amendment 9; Amendment 6's conformance moves here)
- GOAL: A real executor process answers v2 requests over A4's transports: the stdio entry point routes schema_version 2 to `ExecutorV2` (the host id, the lane cards from the confirmed inventory, the occupancy real twin), the enforcer one-shot that A4ub's timer runs exists, and the frozen executor_transport cases pass strict.
- SCOPE: `flightctl/executor_stdio.py`, new `tests/conformance/impl_executor_transport.py` (fake: in-process `ExecutorV2` per SimHost with `sim-` boot ids; real: `LocalSubprocessTransport` over the entry point, ssh to the target), `tests/transport/test_a4_transport.py` (the four Amendment 6 rows: requests to v2), new files under `tests/executor/`.
- ACCEPTANCE: executor_transport conformance [strict]; `test_stdio_serves_v2_through_executor_v2` [realtime]; `test_enforcer_one_shot_entry_point` [realtime].
- PROOF: G0, G2, G3 (host-local and ssh to the pilot host; the stop's emptiness proof reads the lane's cards read-only under a held lane), G4. SEATS: O, L, S6.

### A4ub: units and timers (Amendment 9: part 2 of A4u)
- GOAL: Installable unit templates, so assembly is copying files, not inventing them. The enforcer timer's service runs the executor's enforcer one-shot, which A5a3 delivers (Amendment 9: no packet owned that command before), so A4ub follows A4u and A5a3.
- SCOPE: `units/host/*` (executor enforcer timer every 60 s; the P2 executor template becomes its one-shot service; inhibitor unit naming documented), `units/authority/*` (service plus cert-renewal timer, see A7), a template renderer in `flightctl/siteconfig.py`, the unit section of `docs/v2/DEPLOY-LAYOUT.md`, tests in `tests/siteconfig/`.
- ACCEPTANCE: `test_templates_render_without_site_strings`; `test_timer_unit_runs_one_shot` [realtime, onlab].
- PROOF: G3 on the pilot host (the on-lab timer test with a harmless one shot). SEATS: L, A, Q.

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

### A5r: retire executor v1 (Amendment 9; owned by the lead, not raced; the owner may fold it into the end of A5b2 instead)
- GOAL: Once the authority speaks v2 to the executor (A5b1, A5b2), the v1 executor's tests, their package-local systemd adapter and the v1 executor path go, under the A5r migration rows.
- SCOPE: `flightctl/executor.py` (the v1 path only), `tests/executor/**` under the A5r rows: moved from A5a, the five `tests/executor/test_executor.py` rewrites, the `tests/executor/systemd_adapter.py` delete and the `tests/executor/test_p01.py` and `tests/executor/test_review3.py` rewrites; new, the `tests/executor/test_review4.py` delete (it imports `test_executor` and `test_p01`, measured) and the `tests/executor/test_mutations.py` delete (its 13 mutants target the v1 executor and its v1 tests). The rewrite rows name their replacements as exact A5a nodes, because A5r adds none of them: `tests/executor/test_a5a_executor_v2.py::test_stop_requires_reserve_identity_and_empty_proof`, `tests/executor/test_a5a_executor_v2.py::test_relative_deadline_anchored_on_host_clock` and `tests/executor/test_a5a_executor_v2.py::test_enforcer_real_seconds`. Any other test that still drives the v1 executor gets its row (by amendment) before A5r starts.
- PROOF: G0-G2 (G1b over the A5r rows). SEATS: the lead (deletion-dominated, like A3c: LINES counts deletions).

### A6: WorkloadRunner real twin and the linger probe
- GOAL: Real transient user units with per-unit identity. Answer the linger question by experiment before A-ASM goes live (D-pow-4). Amendment 7: this is PART 1 (the real twin); the dryrun twin, the per-unit FakeRunner and the v1 fake's retirement are A6b.
- SCOPE: `flightctl/runner.py` (the real twin), its real-only conformance registration, tests in `tests/runner/`.
- ACCEPTANCE: workload_runner conformance [strict, real twin]; `test_real_twin_reads_systemd_captures`; `test_crash_observed_and_cgroup_empty` [realtime, onlab]; `test_unit_and_inhibitor_survive_logout` [onlab; the result decides whether the owner enables linger on lane hosts; Amendment 8: its loginctl reads are fixed in A6b, so the decision waits for A6b's G3].
- PROOF: G3 on the pilot host with `sleep` units only, plus the one `sh -c "exit 3"` unit of the frozen conformance case `test_units_are_isolated_and_crash_is_observed` (Amendment 7: allowed by the owner, 4 Oct 2026); the named on-lab tests start only `sleep` units and one `systemd-inhibit --what=idle ... sleep` unit (the linger probe). SEATS: L, S6, A.

### A6b: WorkloadRunner part 2: the dryrun twin, the per-unit FakeRunner and the v1 fake's retirement (Amendment 7)
- GOAL: The v1 fake's success-by-default semantics are gone: a per-unit, fail-closed FakeRunner with `script_next`, and a dryrun twin that never starts anything.
- SCOPE: the dryrun twin (in `flightctl/runner.py`), `tests/fakes/runner.py`, their conformance registrations, tests, and the A6b migration rows (`tests/executor/test_executor.py::test_fake_isolation_guard` delete; `tests/contracts/test_fakes.py::test_fake_interfaces` rewrite; Amendment 8: `tests/runner/test_a6_runner.py::test_unit_and_inhibitor_survive_logout` rewrite, the linger probe reading `loginctl show-user <uid> -p Linger` and counting every session class except `manager`/`manager-early`, from one `loginctl list-sessions --json=short` snapshot before and one after the wait). Amendment 9: the A6b race leaves the first two rows to A6b's lead merge, which executes them once Amendment 9 is on main; they stay A6b's rows (until then `tests/contracts_v2/test_v2_round3.py::test_b7_migration_gate_on_this_branchs_real_diff` failed whenever a packet's own row touched a test file that existed at `59bdd7f`, measured in R-A6b staging).
- ACCEPTANCE: workload_runner conformance on the fake (the fake_only cases) and the dryrun twin; `test_fake_runner_per_unit_fail_closed`; `test_dryrun_never_starts` [onlab]; `test_unit_and_inhibitor_survive_logout` [onlab; Amendment 8: fails as `unreadable` when the Linger value or a snapshot is unreadable, `inconclusive` when sessions change during the wait].
- PROOF: G0, G2, G4; G3 for the linger probe on a lane host with no other login session (Amendment 8). SEATS: L, S6, A.

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
  5. Answer the linger probe (A6b, Amendment 8) and apply the owner's action if needed.
  6. Rollback rehearsal.
  7. Flip: `adapters.json` lane to `live`, shim to `v2`, and the legacy writer disabled for the lane.
- ACCEPTANCE (G6): T01, T02, T03, T04a (holder variant), T05 (current rule), T06a, T06b, T07, T08, live on the pilot lane.
- SPLIT NOTE (G6, Amendment 2, Sol 6.1 P1-5): T04 is SPLIT by name. T04a (holder variant) runs here; the managed-unit variant T04b runs at C7b1 and C-ASM because managed execution arrives with C7b1. This is a named split, not a silent substitution. Also the shadow coverage report and the strict conformance evidence set for every port in the A-ASM prerequisite row.
- OWNER: the lead. Owner actions: the forced-command key line; the private CA's certificate, key, chain and trust anchor for A7 (Amendment 1); linger only if A6b's probe shows it is needed (Amendment 8).

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
- GOAL: Continuous comparison of leases against GPUs, using the A3 rules: a collision when tenants exist (Amendment 2: any process not lease and not an allow-listed desktop identity, whatever its size), when allow-listed noise exceeds `noise_cap_mib`, or when unexplained memory is at or above `lane_noise_mib`; an orphan when a lease is held, the lane is idle, and 15 minutes have passed. Also thermal refuse-new and incident events.
- SCOPE: `flightctl/watcher.py` plus a timer template.
- ACCEPTANCE: `test_collision_includes_aggregate_memory`; `test_thermal_refuse_new_never_kills`; `test_noise_never_flags` (allow-listed identities within the cap only; Amendment 2); `test_desktop_user_legacy_jobs_remain_tenants` (Amendment 2 rev 2: the desktop user's real legacy jobs, multi-card included, flag as collisions; no isolation from arbitrary code running as the trusted uid is claimed); `test_watch_real_time` [realtime, onlab].
- PROOF: G3; T10 in B-ASM. SEATS: S6, L, A.

### B5: reduced real release backend with an independent backup (D-dep-2)
- GOAL: Stage, activate and roll back on one host. Back up to **host C**, which is a different machine from the store host that holds the deploy dir. Rehearse a restore. Switch the backup target to the dedicated backup host when it exists.
- SCOPE: `deploy/flightctl_release.py` (backend switch), `deploy/backend_real.py`.
- ACCEPTANCE: release_backend conformance [strict]; `test_backend_selected_by_config`; `test_backup_target_differs_from_store_host`; `test_restore_rehearsal_hashes_match` [realtime, onlab].
- SEATS: O, L.

### B-ASM: assembly B (owned by the lead)
- GOAL: The pilot lane goes to production: the shim at the legacy path in `v2` mode for that lane, the legacy writer disabled, the watcher and backup running. Lanes R and T go into shadow.
- ACCEPTANCE (G6): T05 (target), T09a, T09b, T10, re-runs of T01, T04a (the holder variant) and T07 through the shim, a rollback rehearsal, and the CUTOVER-RUNBOOK steps for one lane.
- SPLIT NOTE (G6, Amendment 2 rev 2, Sol 6.1 amd2): B-ASM re-runs T04a only; the managed-unit variant T04b needs C7b1 and runs at C7b1 and C-ASM.

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
  - `output-collect --job <id>`: no account argument (round 6). It resolves the job in the helper's root-owned job registry to the UID recorded from the unit at start: the per-job DynamicUser for agents, `fc-<name>` for friends. It copies only single-link files owned by the EXPECTED owner: Amendment 2, namespace-aware, the host-visible owner of the job's StateDirectory root when that is the recorded UID or the kernel overflow uid (id-mapped DynamicUser StateDirectory); otherwise nothing (`collect_job_outputs`).
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
- SCOPE: `helper/flightctl-helper` (Python, root-run, no shell), `helper/sudoers.example`, the production install audit `flightctl/install_audit.py` (`operator_sudo_audit` with the reference oracle's signature; Amendment 2: the carried cases `test_carried_exempt_group_defeats_fresh_auth` and `test_carried_per_command_tags_after_a_comma` in `tests/contracts_v2/test_v2_freeze.py` assert against it and C7h removes their strict-xfail marks under its migration-map rows and flips CONFORMANCE sol6r9-new (both) and sol6r8-B5 to contract-fixed, the completion state `test_carried_obligations_are_recorded` accepts only while the production audit passes both cases; Amendment 2 rev 2), tests. Installation is an owner action (sudo).
- ACCEPTANCE:
  - `test_helper_rejects_every_argument_outside_site_config` (accounts, lanes, minors, unit names, argv[0] outside `argv_roots`, extra flags, environment);
  - `test_unit_start_argv_is_exact_systemd_run`;
  - `test_session_close_order_key_then_terminate_then_slice_stop`;
  - `test_key_write_never_follows_symlinks` (a symlink planted at the key path or the temp name is refused);
  - `test_agent_jobs_cannot_read_each_others_staging` [realtime, onlab, owner-installed: two concurrent DynamicUser job units A and B; B's attempt to open A's staging fails with EACCES; output-collect accepts only files owned by A's expected owner (Amendment 2: the namespace-aware owner of A's own StateDirectory, recorded uid or overflow uid) and only from A's registry-bound StateDirectory]; `test_install_audit` [onlab, on the INSTALLED artefacts]: run `sudoers_audit` (EFFECTIVE privilege, round 4) on `sudo -l -U <executor>` output and `sudoers_file_audit` on every installed sudoers file. Effective grants include %group and alias expansion: the executor must have exactly one grant, `(root) NOPASSWD: <helper>`. The parser follows the measured `sudo -l` format (sudo 1.9.17p2; fixture tests/contracts_v2/captures/sudo-l.json); run `helper_install_audit` on the stat chain from `/` to the helper, its config and the keys dir (root-owned, not group- or other-writable); the executor account fails to write, rename or replace each of them;
  - round 8 (Sol 6 r7): `test_install_audit` also runs `operator_sudo_audit` (no NOPASSWD route; round 9: EFFECTIVE `timestamp_timeout=0` for the program and no `!authenticate`) and `no_clear_route_audit` (executor, every friend), with round-9 route matching for ALL, wildcard paths (`/usr/local/libexec/*`), directory grants (`/usr/local/libexec/`), `^...$` regexes, argument-bearing grants and Cmnd_Alias expansion from the installed sudoers files (unknown alias = route)
  - Amendment 3 rev 2 (Sol 6.1 amd3 P1-3; the lead's preferred route): C7h installs `/usr/local/libexec/flightctl-claim-clear` and `/usr/local/libexec/flightctl-session-probe` in R1 as REFUSING STUB programs (mode 0700 root:root; after sudo has authenticated the operator, every invocation exits with a typed refusal and changes nothing; C9w later routes them through the seams, still refusing by default), together with the operator's two authenticated sudoers rules (`config/flightctl-claim-clear-sudoers.example`, `config/flightctl-session-probe-sudoers.example`: no NOPASSWD, `timestamp_timeout=0`). So `test_install_audit` runs `operator_sudo_audit` and `no_clear_route_audit` for BOTH programs on the installed R1 host, and C9 needs no sudoers change. Safe against a stub: the rule grants the operator a root program that only refuses
  - CARRIED OBLIGATIONS (freeze, Sol 6 round 9; the reference cases are strict-xfail tests in `tests/contracts_v2/test_v2_freeze.py`, owed by C7h):
    - `test_install_audit` must reject an EFFECTIVE `exempt_group` that contains the operator account (sudo then skips the password prompt despite the rule), and must parse and check the authentication tags (PASSWD/NOPASSWD) and SETENV PER COMMAND in a list, including tags inherited by, or changed after, a comma (for example `opr ALL=(root) /usr/bin/true, NOPASSWD: <claim-clear path>`), in both the effective (`sudo -l`) and the static (installed sudoers) audit. The wildcard, alias and group cases stay in this test;
    - `test_claim_clear_prompts_every_time` [onlab, owner-installed; Amendment 3 rev 2: stays in C7h, against the installed refusing stub]: on the installed operator rule, after a successful `sudo` by the operator, an immediate second invocation still prompts (the stub's typed refusal is the expected outcome of both runs); the same for `flightctl-session-probe`;
    - `test_session_probe_bounds`: MOVED to C9 (Amendment 3: the probe ADD path is a C9 body); `test_probe_sweep_requires_real_monotonic_clock`: R1, in C9w (Amendment 3 rev 2, Sol 6.1 amd3: R1 ships the real `--sweep`);
    - `test_friend_flag_off_refuses_creation_allows_cleanup` (Amendment 1 rev 2, Sol 6 amd1 carried): with the flag off, session-open and unit-start --account are refused; a friend unit and session created before the flag was turned off can still be stopped, closed, collected and its claim released;
    - traceability acceptance: the status of CONFORMANCE row `sol6r8-B5` (now `specified`) and the two `carried` rows are part of C7h's review; they may become `contract-fixed` only when the cases above pass.
  - Amendment 3 (John's C9 decision): `test_friend_subcommands_registered_and_refuse_while_off` (session-open, friend unit-start, claim-clear and session-probe are registered dispatch points / installed programs; with the friend flags off, which is always in R1, each refuses with a typed error and no side effect; session-close, claim-release, claim-reconcile and the probe `--sweep` run as cleanup paths). The friend-WORKING tests `test_parent_claim_lifecycle` (creation side), `test_claim_rollback_unlinks_only_its_own_inode`, `test_quarantine_and_start_are_serialised`, `test_claim_clear_requires_operator_sudo_path` and `test_session_probe_bounds` moved to C9 (milestone D);
  - Amendment 3 rev 2 (Sol 6.1 amd3: these need no creation body, so they stay in R1), each on SEEDED state (claims, quarantine markers, managed keys and user-slice processes placed by the test as if created before the flags went off): `test_seeded_claim_release_and_reconcile` (release only with the claimed lease plus the close proof, inode-checked; reconcile NEVER removes a claim; an omitted claim becomes `orphaned-quarantined` and a foreign one a `conflict`, each writing a marker); `test_quarantine_persists_until_operator_clear` (a marker survives a helper restart and a reboot (new boot id); while it exists every session-open, friend unit-start and claim is refused, children of the old parent included; nothing but `flightctl-claim-clear` removes it, and in R1 that program is the refusing stub); `test_seeded_session_close_proof` (session-close on a seeded session removes the key, terminates the user, stops the slice, resets DeviceAllow and reports user_slice_empty and key_removed; then claim-release succeeds with that proof; repeated close is idempotent);
  - `test_device_allow_enforced` [realtime, onlab, owner-installed: inside a `DeviceAllow`-restricted unit, `os.open('/dev/nvidia<other minor>')` fails with EPERM and the lane's minor opens. Device nodes are opened only, no CUDA context, under a lane held through the current authority].
- SEATS: O, S6 (security packet; Astra or Grok as second gauge).

### C7a: templates
- SCOPE: template registry and resolver (whole-token params, no shell); a converter from the site model table.
- ACCEPTANCE: `test_resolver_whole_token_params`; `test_no_shell_metacharacter_expansion`; `test_converter_roundtrip_on_fixture_table`.
- SEATS: A, Q, L.

### C7b1: batch jobs, success path
- SCOPE: job-submit through the state machine to queue, lease, stage and start via `flightctl-helper unit-start` as a per-job DynamicUser (agents) or `fc-<friend>` with `--parent-lease` (after Q1); success, then release after the emptiness proof.
- ACCEPTANCE: `test_job_state_machine_success_transitions`; `test_job_runs_as_its_own_dynamic_user_never_operator_or_shared_uid`; `test_job_end_to_end_real_unit` [realtime, onlab: a CPU-only template on the pilot lane under a held lease]; `test_dynamicuser_write_stop_collect` [realtime, onlab, owner-installed; Amendment 2 rev 2, Sol 6.1 amd2: a real DynamicUser job writes a file into its StateDirectory, the unit stops, and output-collect copies it into the executor-owned store; the test RECORDS the actual owners of the StateDirectory root and of the file and verifies the overflow value against `/proc/sys/kernel/overflowuid`; the binding to THIS job comes from the helper's root-owned job registry and the exact private StateDirectory path for the job id (systemd's private state root + `flightctl-jobs/<job id>`, reached by safe O_NOFOLLOW traversal), never from the numeric owner alone; a planted foreign-owned file and a hard link are rejected; the registry entry and the collected file's hash are recorded. The authenticated fetch is proven at C7c (`test_dynamicuser_output_authenticated_fetch`) and end to end at C-ASM (`test_job_output_end_to_end`)]; `T04b` (managed-unit TTL expiry, Amendment 2); `test_cuda_job_production_path` [realtime, onlab, per enabled lane; Amendment 2, Sol 6.1 P2; rev 2: a small CUDA workload template submitted through the production job API runs via the helper under its DynamicUser, READS STAGED MODEL DATA from the model cache, its GPU process is attributed to the lease (not a tenant), and its output is collected into the executor store; the fetch is proven at C7c (`test_cuda_job_output_fetched`)].
- SEATS: O, L, S6.

### C7b2: batch jobs, failure paths
- SCOPE: crash leads to `failed` and the lane freed after proof; an uncertain stop leads to `lost` and a quarantine; preempt, timeout and cancel.
- ACCEPTANCE: `test_crash_marks_failed_and_frees_after_proof`; `test_uncertain_stop_marks_lost_and_quarantines`; `test_preempt_and_timeout_paths`; `test_cancel_real_unit` [realtime, onlab]; Amendment 2 (Sol 6.1 P2), each [realtime, onlab, per enabled lane, with GPU memory allocated by a small CUDA job]: `test_cancel_and_preempt_with_gpu_memory_allocated` (the lane is freed only after the unit's cgroup is gone and the occupancy emptiness proof); `test_controller_loss_during_gpu_job` (the authority is stopped while the job holds GPU memory; after restart A8's reconcile keeps exclusion until inspect and emptiness agree); `test_no_successor_grant_before_teardown_verified` (a queued successor is not granted until teardown is verified); `test_active_host_loss_during_gpu_job` (Amendment 2 rev 2, a SEPARATE obligation from controller loss: the lane host itself is lost (network cut or power) while the job holds GPU memory; the lane stays excluded (unknown/quarantined, never free) until the host is back and inspect plus the emptiness proof agree).
- SEATS: O, L, S6.

### C7c: job logs and authenticated output retrieval (Sol B6)
- SCOPE:
  - job-logs: the journal through the executor `logs` kind, owner-only.
  - job-output-list and job-output-get: chunked, each file sha256-verified, owner or operator only, path traversal refused, `202 waking` for a sleeping lane host, served through the executor `output` kind.
  - Trusted staging (round 4, Sol 6 r3 hard links):
    - The job writes only into a fresh per-job staging directory owned by the job account (as the host sees it: the recorded uid, or the overflow uid when id-mapped, Amendment 2): the per-job DynamicUser UID for agents (its 0700 StateDirectory, unreachable by other agent jobs), or `fc-<friend>` for friends.
    - After the unit is stopped and its cgroup is empty, `flightctl-helper output-collect` copies into the executor-owned store only regular files with `st_nlink == 1` and `st_uid ==` the expected owner (Amendment 2: namespace-aware, `output_owner_expected`; foreign files and hard links stay rejected). It checks both with fstat on the `O_NOFOLLOW` fd.
    - Hard links, symlinks, devices and foreign files are rejected and listed.
    - Reads serve only those copies. Reference: `stage_outputs`.
  - Round 3 containment (Sol 6): the executor opens each output by walking from a directory fd of the output root with `O_NOFOLLOW` per component and serves regular files only. Absolute paths and empty, `.` or `..` components are refused before the walk (round 4). `get` carries the `expect_sha256` that `list` returned, and the executor refuses a file that changed in between. Reference: `contained_open` in `tests/contracts_v2/validation.py`.
  - Retention and wipe; the notification hook.
  - CLI `job fetch <job_id> [dest]` verifies hashes.
- FRIEND IDENTITY (round 3, Sol 6 B6, accounts contract): a friend is authenticated by their own tailnet identity, through a node share or invite resolved by whois. They need no host account and no shell. A token is optional and only narrows scopes; it is accepted only from the friend's allowed peers.
- ACCEPTANCE: `test_logs_owner_only`; `test_output_fetch_owner_only_and_traversal_refused`; `test_output_symlink_escape_and_list_get_swap_refused` [realtime, on real files: a symlink to a file outside the root, a symlinked directory, a file swapped between list and get, and an absolute path are all refused; a hard link planted in staging is never copied to the store]; `test_friend_client_fetches_outputs_without_shell` [realtime: a friend principal identified only by its tailnet login, with no host account, runs `job submit`, `job status` and `job fetch` against a real server; the files match; the same client from an unmapped peer is refused]; `test_output_retention_wipe`.; Amendment 2 rev 2 (moved from C7b1, which cannot use job-output-get before C7c): `test_dynamicuser_output_authenticated_fetch` [realtime, onlab: the file a real DynamicUser job wrote and C7b1's output-collect stored is fetched by its owner through job-output-list/get, and refused to anyone else] and `test_cuda_job_output_fetched` [realtime, onlab, per enabled lane: the CUDA job's output is fetched the same way]
- SEATS: L, A, Q.

### C8: served endpoints
- SCOPE: endpoint-load and endpoint-unload (chat-* aliases); the generalised ChatController; the request-accounting proxy unit, run via the helper under a per-unit DynamicUser; the HealthProbe real twin; idle unload at 600 s; eviction drain; retiring the legacy on-demand front (lab side).
- ACCEPTANCE: health_probe conformance [strict]; `test_idle_unload_600s_completed_requests_only`; `test_eviction_drains_then_stops`; `test_endpoint_real_tiny_server` [realtime].
- SEATS: O, L, S6.

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

### C9w: session pathways for C9 (R1; Amendment 3, John's C9 decision; rev 2, Sol 6.1 amd3)
- GOAL: Deliver in R1 every pathway C9 will use, wired end to end and REFUSING while the friend flags are off, so that C9 (milestone D) is a self-contained build of bodies behind these pathways that edits no R1 file except the named extension points (owner's words: "C9 can be an entirely self-contained build that merely uses pre-built pathways").
- SCOPE (all in R1):
  - SessionGateway: the fake twin and the REAL twin over `flightctl-helper session-open/close`, with explicit `parent_lease_id` and `lane_id`; with the flags off the real twin's call crosses the real wire (sudo helper) and comes back refused (typed `unavailable`, no side effect). Registered by C9w in a NEW file `tests/conformance/impl_session_gateway.py` (the conformance conftest imports every `impl_*.py`).
  - Session RPC ops `session-open` and `session-close` (rpc-ops x-ops, token scope `session`, CLI `session open/close`): wired to `authority_admits_friend_work` and the global, per-host and conjunction flag checks, then the unique (principal, host) parent admission transaction (`admit_parent`); while off they refuse with `unavailable` naming the feature (ADAPTERS rule 2). The web UI has no session surface.
  - Key enrolment (rev 2, Sol 6.1 amd3 P1-4): an account-scoped key registry in the authority store (`session.schema.json` `enrolled_key`) with the RPC ops `session-key-enrol` (refused with `unavailable` while `features.friend_sessions` is off: it creates friend state), `session-key-list` and `session-key-revoke` (both work while off), scope `session`, CLI `session key add/list/revoke`; only bare OpenSSH keys of the listed types (`parse_session_key`). session-open resolves its `key_fingerprint` with `resolve_session_key` (the caller's own unrevoked key; otherwise `not_found`) AFTER the friend gates and passes the key bytes on.
  - Executor session lifecycle (rev 2, P1-4): executor kind `session` (`executor.schema.json` `session_request`): `register` calls SessionGateway.open and creates an executor session-registry entry only from an ok reply (refused while off: ok=false, definite, `unavailable`, nothing recorded); `close`; the executor's own `local-expiry` and `controller-loss` closes; `reconcile` (sent on every executor (re)connection and A8 pass). Oracle `executor_session_closes`.
  - Session-to-parent attribution (rev 2, P1-4): the executor and watcher attribute a GPU process to a friend's parent lease only through `session_attribution`: its cgroup lies inside the `user-<uid>.slice` that an OPEN registry entry for this lane records (the slice the helper's session-open configured), never by uid alone; anything else stays `external`. With no entry (always in R1) the branch attributes nothing.
  - Disable ordering `friend_sessions_disable` (a safety path that runs with the flags off) and `friend_sessions_consistent` in the deploy and C-ASM checks.
  - Helper: registered dispatch points `session-open`, `session-close`, friend `unit-start --account`, `claim-release`, `claim-reconcile`; creation dispatches to the seams and refuses while off. The programs `flightctl-claim-clear` and `flightctl-session-probe` (installed by C7h as refusing stubs with the operator's sudoers rules) dispatch to the seams `claim_clear_body` and `session_probe_body`, refusing by default; the probe's root-only, remove-only `--sweep` and the per-minute `flightctl-session-probe-sweep.timer` are real here.
  - CLEANUP BODIES ARE REAL, NOT STUBS: `session-close`, `claim-release`, `claim-reconcile` (C7h) and the probe `--sweep` (C9w), each proven on seeded state by a named R1 test (`test_seeded_session_close_proof`, `test_seeded_claim_release_and_reconcile`, `test_probe_sweep_requires_real_monotonic_clock`, and the conformance case `test_session_close_on_seeded_session_proves_cleanup`).
  - C9 seams `flightctl/c9_seams.py` (and `helper/c9_seams.py` for the helper): the hooks `session_open_body`, `friend_unit_start_body`, `claim_create_body`, `claim_clear_body`, `session_probe_body` and `binding_measure`, each with a REFUSING default. R1 code reaches friend behaviour only through these hooks.
  - Seam loading (rev 2, Sol 6.1 amd3 P1-5): the R1 release manifest never contains C9's two package directories (the oracle's `C9_PACKAGES`, the packages `flightctl.c9` and `helper.c9`); the install audit asserts their absence (`c9_absent_audit`) and the release backend's activate removes every installed file the new manifest does not list (`release_stale_removals`), so a stale C9 package cannot survive a rollback or an R1 install. Cleanup paths never call the loader; the probe `--sweep` does not depend on it.
  - Loader STATES (rev 3, Sol 6.1 amd3r2 P1: the flags-off probe had no working state). The loader state is explicit, not binary (oracle `c9_loader_state`, reference module `tests/contracts_v2/c9_loader.py`): `off` (every friend flag false, no valid window), `window` (every friend flag false and a valid ENABLEMENT WINDOW on this host) and `on` (the helper's three friend flags all true). A mixed flag state is `off` (fail closed). The ENABLEMENT WINDOW is per host, operator-opened and time-bounded: `sudo /usr/local/libexec/flightctl-session-probe --window-open --ttl <s>` (the operator's existing re-authenticating route; ttl at most 3600 s; refused while any friend flag is true, for any other caller, and while a valid window exists), recorded in the helper's root-owned state directory with its wall and monotonic deadlines and the boot id (`c9_window_open`); it counts only on its own host, in the same boot, before BOTH deadlines (`c9_window_valid`), and it closes on `--window-close --result success|failure`, once it is no longer valid at the per-minute `--sweep --expired`, and on the rollback unit's `--sweep --all --close-window` (`c9_window_close`, `c9_window_sweep`); rev 4 (Sol 6.1 amd3r3): the operator's step-7 `--sweep --all` removes the probe key and leaves a valid window open for step 8(a) (an already-invalid window record is removed too; it refuses measurement anyway); every call is an audit event. The body table (`c9_body_allowed`; every other cell is the refusing default; checked against every runbook step before enablement and against the probe ADD policy, which refuses while `friend_sessions` is on):

    | Body | off | window | on |
    | --- | --- | --- | --- |
    | `session_open_body` | refused | refused | allowed |
    | `friend_unit_start_body` | refused | refused | allowed |
    | `claim_create_body` | refused | refused | allowed |
    | `claim_clear_body` | refused | refused | allowed |
    | `session_probe_body` | refused | allowed | refused |
    | `binding_measure` | refused | allowed | allowed |

    So the probe ADD (runbook step 7) works in exactly one state, the window, and the binding measurement (step 8(a), `flightctl-session-probe --measure`) uses the same `binding_measure` the helper re-measures with when `on`. `claim_clear_body` is `on` only: no runbook step needs it, and lifting a quarantine while friend creation is refused changes nothing (the marker is the fail-safe state). Friend creation stays refused in the window.
  - Trusted startup and import boundary (rev 3, Sol 6.1 amd3r2 P1, measured: `python3 -I` still runs `site` and keeps site-packages on `sys.path`; `python3 -I -S -B` has no `site` and only the stdlib entries). The helper and the session-probe program run `python3 -I -S -B`; before any C9 import `sys.path` is set to those stdlib entries plus the verified prefix only, and the meta-path guard `C9ImportGuard` sits first on `sys.meta_path`: it resolves every import itself and refuses any module that is neither builtin/frozen, nor exact stdlib, nor inside the verified prefix. Rev 4 (Sol 6.1 amd3r3, measured: this interpreter's site-packages `/usr/lib/python3.14/site-packages` is NESTED inside its stdlib directory): exact stdlib (`stdlib_trusted`) means under one of the strict-startup stdlib entries (the stdlib zip, the stdlib directory, lib-dynload), under neither of the interpreter's third-party locations (sysconfig `purelib`/`platlib`), and with no `site-packages` or `dist-packages` directory between that entry and the file. The prefix is canonicalised once and both verification and every import use that canonical path (an alias spelling is never imported through, so repointing it cannot substitute a tree), and before any import every module already in `sys.modules` with a file origin must be exact stdlib or inside the canonical prefix (`preloaded_outside_modules`), because a cached module is returned without consulting any finder. A third-party dependency a C9 body needs is VENDORED inside the prefix and listed in the release manifest; no bytecode is written (`-B`) and any cache file, like any file not in the manifest, refuses. `c9_seam_load` (rev 3): (1) only the hooks the state allows are candidates, none means nothing is read or imported; (2) cached `sys.modules` entries for the package are purged; (3) the prefix itself and every ancestor directory up to `/` must be root-owned directories that are not group- or other-writable, and every entry under the prefix a non-symlink, root-owned, not group/other-writable and listed in the release manifest with its sha256; any filesystem error refuses; (4) only then the import, under the guard; ANY BaseException at import time (SystemExit and KeyboardInterrupt included: the helper is non-interactive and refusing is the outcome either way, so the loader never raises) refuses and purges the package; (5) every candidate hook must exist and be callable.
  - Claim store: C7h's on-disk store (claims directory, quarantine markers) with read, reconcile and release; C9w adds only the `claim_create_body` seam call on the creation path.
  - Config fields already in the contracts: inventory `friend_sessions_enabled`, `c9_proof` and the observed hashes; helper.json `friend_sessions*`, `c9_binding`, `probe_account`.
  - Conformance: the R1 session cases `test_session_open_refused_while_friend_flags_off` and `test_session_close_on_seeded_session_proves_cleanup` are already in `tests/conformance/test_work_support.py` (contract, Amendment 3 rev 2), so C9w edits no pre-existing test file; C9 adds its success proof as a new conformance file in its own touch set (`friend_flags("on")`).
- ACCEPTANCE (R1, flags off unless stated): session_gateway conformance [strict, refusing real twin, FLIGHTCTL_CONFORMANCE_FRIEND_FLAGS=off: `test_session_open_refused_while_friend_flags_off`, `test_session_close_on_seeded_session_proves_cleanup`]; `test_authority_refuses_session_open_when_flag_off`; `test_authority_refuses_friend_session_on_ineligible_host`; `test_helper_refuses_without_both_flags_and_live_binding`; `test_disable_ordering_fail_closed`; `test_consistency_check_passes_before_activation`; `test_c9_seams_refuse_by_default` (every seam's default refuses; a test-only `flightctl.c9` providing one body is reached by the unchanged R1 caller);
  - rev 2 (Sol 6.1 amd3): `test_session_key_enrol_refused_while_off_list_and_revoke_work`; `test_session_key_parse_refuses_options_line_breaks_and_type_mismatch`; `test_session_open_resolves_only_own_unrevoked_key`; `test_session_pathway_end_to_end_through_r1_callers` [sim, the test configuration turns the flags on with the FAKE SessionGateway: the unchanged R1 session-open RPC runs the friend gates, the unique parent admission (a concurrent second parent gets `409 account_busy`), key resolution and the executor `session` register; the fake gateway receives the enrolled key bytes, parent lease and lane; the executor records the entry; the watcher attributes a process in that slice to the parent; local expiry closes it];
  - `test_executor_session_register_refused_while_off_records_nothing`; `test_executor_closes_seeded_sessions_on_local_expiry_controller_loss_and_reconcile`; `test_session_attribution_by_registered_slice_never_by_uid` (through the unchanged R1 watcher and executor callers, on a seeded registry);
  - `test_probe_sweep_requires_real_monotonic_clock` (the sweep: root-only, remove-only, idempotent, never touches a non-probe key, every outcome an audit event, monotonic value and boot id required inputs; with no probe key present in R1 it is a recorded no-op);
  - `test_r1_install_has_no_c9_packages` [onlab, on the INSTALLED R1 tree: `c9_absent_audit`]; `test_release_removes_stale_c9_package` (a release without the package removes a planted one on activate and on rollback); `test_seam_loader_policy` (present but no body allowed: not imported; a stale package, a tampered file, an extra or cache file, a symlink, a writable file, a writable prefix or ancestor, a filesystem error, a cached `sys.modules` entry without a package, or any import-time BaseException: refusing defaults; cleanup still works);
  - rev 3 (Sol 6.1 amd3r2): `test_enablement_window_lifecycle` (operator-only open, refused while any flag is on, ttl bound, no extension, closes on success, failure, expiry, reboot and the rollback sweep, every call audited); `test_loader_state_table_every_cell` (each body in each of `off`, `window`, `on` through the unchanged R1 dispatchers); `test_probe_add_only_in_the_window` [onlab, owner-run: the installed probe program adds the probe key only inside an open window, never with the flags off and no window, never with them on]; `test_trusted_startup_and_import_guard` [on the installed helper's interpreter: `-I -S -B`, `sys.path` stdlib plus prefix only, a module outside both refused even when its directory is put on `sys.path`].
- SEATS: O, S6 (security packet).

### C-ASM: assembly C, the R1 release (owned by the lead)
- GOAL: Friends and agents use timeslots.
  - Lanes R and T go live after clean, covered shadow periods. The Titan pair flips only in an idle window.
  - Lane D is enabled as standby-only for agents, after the yield proof.
  - Friend onboarding: the tailnet share and ACL, accounts, quotas, AUP.
  - Amendment 1 (Q1 = (c) for R1): friends use API-queued jobs under a per-job DynamicUser, exactly like agents. No friend SSH session or friend-account job in R1: `features.friend_sessions` is false, every host's `friend_sessions_enabled` is false, and C-ASM runs `friend_sessions_consistent` for every host (rev 7/8). Amendment 3 rev 2: C-ASM tags the R1 release commit `v2-r1` (the R1 BASE against which C9's new paths are checked absent) and runs `c9_absent_audit` on every installed tree.
- ACCEPTANCE (G6):
  - On every newly live lane: T01, T02, T04a, T04b, T07 (sleeping lanes) and T10; and the Amendment 2 GPU job proofs `test_cuda_job_production_path`, `test_cancel_and_preempt_with_gpu_memory_allocated`, `test_controller_loss_during_gpu_job`, `test_active_host_loss_during_gpu_job` and `test_no_successor_grant_before_teardown_verified`; and the NON-DROPPABLE end-to-end obligation `test_job_output_end_to_end` (Amendment 2 rev 2: a real DynamicUser job writes, stops, is collected and its owner fetches the output through the authenticated API, plus the same for the CUDA job, on every newly live lane; it cannot be waived by C7b1's or C7c's partial proofs).
  - T11a-d.
  - The R1 scenarios:
    - a friend books a slot, runs a job, fetches its output with `job fetch`, sees their usage, (no SSH session in R1: Amendment 1);
    - an agent does the same through MCP under its token;
    - the owner overrides from the HTTPS web UI with WebAuthn where the matrix requires it;
    - a sleeping host wakes for a booking;
    - a quota denial names its limit.

### C9: friend SSH sessions: the bodies behind C9w's pathways (session-enablement milestone D, NOT in R1; Sol B5; Amendments 1 and 3)
- MILESTONE (Amendment 3, owner's decision): C9 moves out of R1 to the session-enablement milestone D. It BUILDS ONLY the bodies behind the R1 pathways (account creation, claim creation and persistence, the claim-clear program body, the session-probe program body, the sshd runbook tooling and binding measurement), their on-lab proofs and the pre-enablement security review. It must not edit any R1 file except the extension points below.
- R1 PATHWAYS USED (Amendment 3; rev 2/3: 21 distinct capabilities, PINNED in `tests/contracts_v2/test_v2_amendment3.py`; each resolves to an R1 packet that names the pathway; checked by `test_a3_every_c9_capability_resolves_to_an_r1_pathway`):

  | Capability | R1 deliverer | Pathway |
  | --- | --- | --- |
  | session RPC ops | C9w | `session-open` |
  | key enrolment and fingerprint resolution | C9w | `resolve_session_key` |
  | SessionGateway port and real wire | C9w | `tests/conformance/impl_session_gateway.py` |
  | executor session lifecycle | C9w | `executor_session_closes` |
  | session-to-parent attribution | C9w | `session_attribution` |
  | authority friend gates | C9w | `authority_admits_friend_work` |
  | global / per-host / conjunction flags | C9w | `friend_sessions_consistent` |
  | disable ordering | C9w | `friend_sessions_disable` |
  | helper dispatch points | C9w | registered dispatch points |
  | probe sweep | C9w | `flightctl-session-probe-sweep.timer` |
  | behaviour seams | C9w | `flightctl/c9_seams.py` |
  | verified seam loading | C9w | `c9_seam_load` |
  | enablement window (probe and measurement before enablement) | C9w | `c9_window_open` |
  | C9 package absence and stale removal | C9w | `release_stale_removals` |
  | claim store (read, reconcile, release) | C7h | `claim-reconcile` |
  | claim-clear program and operator rule | C7h | `flightctl-claim-clear` |
  | session-probe program and operator rule | C7h | `flightctl-session-probe` |
  | production install audit | C7h | `flightctl/install_audit.py` |
  | device isolation | C7h | DeviceAllow |
  | friend unit-start branch | C7h | `--parent-lease` |
  | job-output collection for friend jobs | C7h | `output-collect` |

- TOUCH SET (Amendment 3; rev 2: PINNED in `tests/contracts_v2/test_v2_amendment3.py`; the new paths must be ABSENT at the R1 base, the release tag `v2-r1`, and may exist only as C9's additions after it; checked by `test_a3_c9_touch_set_*` and by `tools/touch_set_gate.py` on C9's diff, which also checks the extension scopes line by line):
  - new: `flightctl/c9/`
  - new: `helper/c9/`
  - new: `tools/c9_runbook/`
  - new: `tests/c9/`
  - new: `tests/conformance/test_session_gateway_c9.py`
  - extension: `docs/v2/CONFORMANCE.tsv` (only the status and note of rows homed at C9)
  - extension: `docs/v2/migration-map.tsv` (append rows owned by C9; earlier rows unchanged)
  - extension: `docs/v2/SLICES.md` (the C9 brief only, except its TOUCH SET and R1 PATHWAYS USED blocks)
- KNOWN R1 TOUCH POINTS that could not be pre-wired: NONE in R1 code. What enablement changes is site data and host configuration, not repo files: the flags, `c9_proof`, helper.json, the friend accounts and the sshd drop-in (owner actions, via the runbook). Rev 2: the operator's claim-clear and session-probe sudoers rules are installed in R1 by C7h (against refusing stubs), so enablement changes no sudoers file. FROZEN LITERALS (rev 3, Sol 6.1 amd3r2): C9 may edit its own brief only in ways that keep every literal the frozen tests assert there (for example `features.friend_sessions`, the acceptance test names, the runbook commands and the `c9_proof` field names), not only their meaning; the extension point is narrower than arbitrary accurate prose. BOUNDED (rev 2): the touch set, the pathway table, the extension scopes and the cleanup-body declaration are checked mechanically; whether C9's prose elsewhere in its brief, in CONFORMANCE notes or in migration reasons is accurate is review, not a check.
- ACCEPTANCE MOVED FROM C7h (Amendment 3; friend-WORKING behaviour):
  - `test_parent_claim_lifecycle` (creation side; rev 2: the release, reconcile and quarantine-persistence parts are R1, C7h's seeded tests): a child of the claimed parent is accepted and any other lease refused; after a quarantine, an operator `claim-clear` with the close proof lifts it and admission resumes; round 7;
  - round 8 (Sol 6 r7): `test_claim_rollback_unlinks_only_its_own_inode` (a marker appears after link(); an interleaved operator clear and a replacement claim run before the rollback; the rollback must leave the replacement in place and return refused);
  - round 8 (Sol 6 r7): `test_quarantine_and_start_are_serialised` (children racing claim-reconcile: every started child finished its start before the marker existed, none starts after);
  - round 8 (Sol 6 r7): `test_claim_clear_requires_operator_sudo_path` (the helper refuses `claim-clear`; the clear program refuses non-root, a missing or different SUDO_USER/SUDO_UID, or another argv[0]);
  - `test_session_probe_bounds` (Amendment 1 rev 4, Sol 6 amd1 r3): the probe program (helper-config `x-operator-session-probe`) works with the flag OFF for `probe_account` only; it is refused for any other account, for a non-operator caller (wrong SUDO_USER/UID, or through the executor's helper), with the flag on, with a TTL over 900 s, and while an unexpired probe key exists; the key carries a Z-suffixed UTC `expiry-time` (rev 5: effective expiry <= issue + 900 s whatever the server's time zone) and is removed on expiry by C9w's real `--sweep` (rev 2: the sweep itself, its clocks and its audit events are R1, proven by C9w's sweep test; here the add path records `expires_mono` and the boot id that the sweep reads), on revoke and on rollback, while the add path is refused in the sweep's root context; each call is an audit event. (`test_install_audit` runs the probe program's operator audits in R1, C7h.)
- ACCEPTANCE MOVED TO C9w (Amendment 3; refusal and safety paths that R1 must prove): `test_authority_refuses_session_open_when_flag_off`, `test_authority_refuses_friend_session_on_ineligible_host`, `test_helper_refuses_without_both_flags_and_live_binding`, `test_disable_ordering_fail_closed`, `test_consistency_check_passes_before_activation` and the refusing-twin session_gateway conformance are R1 acceptance in C9w; C9 re-runs them against its working bodies. Rev 2 (Sol 6.1 amd3): the key-enrolment, executor-lifecycle, attribution, seam-loading and probe-sweep tests are R1 in C9w; the seeded claim, quarantine and close-proof tests and `test_claim_clear_prompts_every_time` are R1 in C7h.
- SECURITY REVIEW BEFORE ANY ENABLEMENT (Amendment 1 rev 9): enabling friend sessions on ANY host requires a dedicated security review of the then-current C9 implementation (helper, authority gates, runbook tooling) before activation, independent of this contract review. R1 ships with every friend flag off: the global `features.friend_sessions`, every host's `friend_sessions_enabled` and every helper copy. Rev 10: the review must test the BUILT authority, helper, deployment read-back, running sshd and session behaviour, and must close three NAMED OBLIGATIONS (residuals accepted by Sol 6 amd1 r9, not closed by this contract):
  - `review-obligation-stale-copy`: during a disable, an unreachable host keeps its stale helper copy until reached; show that no direct local helper call can create friend work there, or accept and record the exposure;
  - `review-obligation-loaded-vs-file`: `sshd -T` reads the configuration files, so a running daemon whose loaded configuration differs from its files is not detected; show how the deployment proves the running daemon matches the bound files (for example a reload immediately before the proof and before each enablement);
  - `review-obligation-post-commit`: a configuration change after a creation's commit point affects only later creations; show the effect on already-open sessions is acceptable;
  - `review-obligation-no-credentials-outside-window` (Amendment 2, Sol 6.1): prove that a friend account cannot authenticate outside the managed session window through any other credential source: preserved key files in the friend's home (the runbook PRESERVES existing AuthorizedKeysFile paths, so the friend accounts' own paths must hold no keys and must not be writable by the friend), password or keyboard-interactive authentication, PAM, host-based or GSSAPI/Kerberos methods, or any other global credential source.
- R1 STATUS (Amendment 1, owner Q1 = (c) for R1): no friend SSH sessions and no friend shell jobs in R1. Friends get what agents get: API-queued jobs under a per-job DynamicUser. Amendment 3 (John's C9 decision): C9 is NOT built in R1; C9w delivers its pathways in R1 and the global flag `features.friend_sessions` stays **false**, and so does every host's `friend_sessions_enabled`. Rev 7 (Sol 6 amd1 r6): enablement is PER HOST. A host offers friend sessions only when the global flag AND its own `inventory.hosts[].friend_sessions_enabled` are true, and the latter may be true only on an `openssh` host with a recorded runbook proof (`c9_proof`: proof id, artefact SHA-256, date, runbook revision, sshd verified, probe login ok, probe key removed). The authority (`authority_admits_friend_work`) and the host's helper both enforce it; the global flag never enables an unproven host. Rev 8 (Sol 6 amd1 r7): the proof is BOUND to its host (host id, sshd host key, machine-id and effective `sshd -T` hashes) and to an evidence artefact held in the authority's artefact store; admission compares the binding with the values the inventory probe last observed, so a copied proof, a re-imaged host or a changed sshd configuration is refused until the runbook is re-run. The helper carries the global flag, the host flag and the binding itself (`friend_sessions_global`, `friend_sessions_host`, `c9_binding`), treats either flag false as off, and re-measures the live host facts before every friend creation. Rev 9 (Sol 6 amd1 r8):
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
  - session_gateway conformance [strict, FLIGHTCTL_CONFORMANCE_FRIEND_FLAGS=on on the enabled test host]: `tests/conformance/test_session_gateway_c9.py::test_session_window_opens_and_closes` (Amendment 3 rev 2: the successful proof moved out of the R1 module; it asserts `friend_flags_on()` is True, open returns ok, close returns ok with the key gone, `user_slice_empty` and `key_removed`), plus the R1 cases on a flags-off host;
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
  5. Arm an automatic rollback BEFORE reloading: a transient timer (for example `systemd-run --on-active=10min --unit=flightctl-sshd-rollback ...`) that restores the backup, runs `sshd -t`, reloads sshd and removes any probe key and closes the enablement window with the ROOT-ONLY, remove-only `/usr/local/libexec/flightctl-session-probe --sweep --all --close-window` (rev 5: a root unit has no sudo context, so it cannot use the operator-only add path; Amendment 3 rev 4: `--close-window` is what closes the window, so the operator's plain `--sweep --all` in step 7 does not), unless it is cancelled. The per-minute `flightctl-session-probe-sweep.timer` (`--sweep --expired`) also stays active; it also closes a window that is no longer valid (expired, another boot).
  6. RELOAD, never restart: `systemctl reload` of the sshd unit (existing sessions survive).
  7. PROBE LOGIN with the flag still OFF (rev 4, Sol 6 amd1 r3): `session-open` is refused while `friend_sessions` is off, so the managed-key proof uses the bounded probe: as the operator, first open this host's enablement window, `sudo /usr/local/libexec/flightctl-session-probe --window-open --ttl 3600` (Amendment 3 rev 3: only the probe ADD and the binding measurement can load in it; friend creation stays refused), then `sudo /usr/local/libexec/flightctl-session-probe --account <probe_account> --pubkey-file <test key> --ttl 600` (sudo prompts; one key, `probe_account` only, expires by itself). From a NEW connection, log in as the operator with the existing key, then as `probe_account` with the probe key (the forced command answers `flightctl-probe-ok`). Only if both work: revoke the probe (`sudo /usr/local/libexec/flightctl-session-probe --sweep --all`; it removes the probe key only, and the window stays open for step 8(a)) and cancel the rollback timer. Otherwise close the window (`sudo /usr/local/libexec/flightctl-session-probe --window-close --result failure`), let the timer fire (it restores sshd and removes the probe key) and investigate from the still-open root session.
  8. Record the result (date, host, `sshd -T` excerpt, test logins, the probe's audit events) in the evidence directory, and confirm the probe key is gone. Only then, for THIS host only:
     (a) MEASURE the binding after the reload, inside the window (rev 4: if it is no longer valid, because it expired or the host rebooted since step 7, reopen it first with `sudo /usr/local/libexec/flightctl-session-probe --window-open --ttl 3600`; `--measure` is refused outside a window; if the measurement cannot be completed, close the window with `--window-close --result failure` and stop, flags untouched), with `sudo /usr/local/libexec/flightctl-session-probe --measure` (Amendment 3 rev 3: the helper's own `binding_measure`, the same code that re-measures before every friend creation), which applies the reference digest functions to these inputs: `host_id`; `sshd_host_key_sha256` = `sshd_host_keys_sha256` over ALL host public keys named by the effective `hostkey` lines; `machine_id_sha256` = SHA-256 of /etc/machine-id; `sshd_effective_sha256` = `sshd_effective_digest` over the global `sshd -T` output, the `sshd -T -C user=<u>,host=<h>,addr=<a>` output for the probe account and every friend account, and the Include-expanded configuration file set; then close the window (`sudo /usr/local/libexec/flightctl-session-probe --window-close --result success`) before any flag is written;
     (b) store the evidence artefact in the authority's artefact store under a new `proof_id` and record its `artefact_sha256`;
     (c) set `inventory.hosts[<host>].friend_sessions_enabled: true` with its `c9_proof`: `proof_id`, `artefact_sha256`, `recorded_at`, `runbook_revision`, the binding (`host_id`, `sshd_host_key_sha256`, `machine_id_sha256`, `sshd_effective_sha256`) and all FOUR outcomes, each true: `sshd_verified`, `probe_login_ok`, `probe_key_removed`, `no_conditional_key_settings`;
     (d) write that host's helper.json `friend_sessions_global` (= the site flag), `friend_sessions_host: true`, `friend_sessions` (= both) and `c9_binding`;
     (e) run `friend_sessions_consistent` for every host: it MUST PASS before activation.
     The global `features.friend_sessions` is a separate owner decision and enables no host by itself (rev 7). Any later sshd configuration change or re-image invalidates the proof (both layers refuse) until this runbook is re-run (rev 8). This step's lists are checked against the `c9_proof` schema by `test_r11_runbook_matches_the_proof_schema` (rev 11).
