# Flightctl contract set v2

Status: **frozen for review** (2 Oct 2026). Sol 6 reviews this set before any slice starts. The v1 set in
`contracts/*.schema.json` stays byte-identical and keeps its tests; v1 is retired slice by slice as
MIGRATION.md describes.

This set exists because the v1 library passed 487 fake-only tests but could not run on real hosts. Five
bugs were measured when the real classes were wired together: cross-host boot ids, no beat and a renew
that never succeeds, a freeze after restart, a release-identity mismatch, and dropped error reasons.
Every one of them traces to a contract that let the fakes encode an assumption the real topology breaks.
v2 fixes those contracts, adds the nouns the target model needs (accounts, quotas, usage, work, power,
storage, windows, notifications), and makes "fake with a real twin behind the same port, selected by
configuration" a contract (`adapters.schema.json`, `interfaces.py`, `ADAPTERS.md`).

Normative documents in this directory:

| File | What it fixes |
| --- | --- |
| `*.schema.json` | Wire and record shapes (JSON Schema 2020-12, local `$id` base `https://flightctl.local/contracts/v2/`). |
| `interfaces.py` | Ports as `typing.Protocol`. Each port has a fake, a dry-run twin where it mutates, and a real twin. |
| `ADAPTERS.md` | Selection by configuration, the sim/shadow/live profiles, fail-closed rules, and what "done" means for an adapter. |
| `LEGACY-COMPAT.md` | The lanes.sh shim, so existing callers keep working. |
| `MIGRATION.md` | The v1 to v2 path and which v1 tests become obsolete. |

## Keep / amend / split / add

| v1 contract | v2 decision | v2 home | Why |
| --- | --- | --- | --- |
| `common` | **Split**. Federation hooks stay frozen and dormant in v1. | `common` (v2 core), `policy`, `../common.schema.json` (hooks) | The policy object was closed and could not hold quotas (Grok 6). The hooks are off the friends path. |
| `inventory-v1` | **Amend** (v2) | `inventory` | Adds card UUID, PCI bus id and NUMA node (G07), host roles and power profile, `asleep` (G22), and enforced lane rules (G19). Moves `identity_mapping` out. |
| `rpc-envelope-v1` | **Amend** (v2) | `rpc-envelope` | Adds `202 waking`, typed cause chains (Grok 7), quota details, result kinds for the new families, and a scrubbed token replay window. |
| `rpc-ops-v1` | **Amend** (v2). Every v1 op keeps its name. | `rpc-ops` | Adds operator ops by public id (G25), snapshot and events since a cursor (G26), heartbeat (G28), and job, session, endpoint, token, quota, window, power and cache ops. Renew is rolling within a ceiling (Grok 2). Identity is never caller-supplied. |
| `lease-v1` | **Amend** (v2) | `lease` | Uses the public `lease_id`, `token_sha256` at rest, `hex16` tokens for the shim, rolling `expires_at` plus `approved_max_end`, holder binding and wake record. |
| `executor-v1` | **Semantic v2** | `executor` | Deadlines are durations anchored by the host (G02). Beat carries renewals (G03). The full identity is set before reserve (Grok 1). Refusals are definite, errors are typed, work runs in holder or unit mode, and stage/logs kinds are added. |
| `occupant-v1` | **Rename/generalise** | `endpoint` | A served endpoint is work from a template. Request accounting is unchanged (ref to v1). |
| `event-v1` | **Amend** (v2) | `event` | Adds a gap-free `seq` cursor, public-id refs, preserved errors, and shadow-divergence and adapter-config kinds. |
| `approval-v1` | **Keep**, plus **add** override approvals | `../approval-v1`, `approval` | The v1 actions are unchanged. v2 adds operator overrides by public id with target and beneficiary, so the approval matrix can apply the owner's equal-priority rule. |
| `booking-v1`, `queue-v1`, `pipeline-v1` | **Keep unchanged** | v1 files (referenced) | Their invariants already pass tests. The new links (job to queue, window to booking) live in the new records. |
| `discovery-v1` | **Amend** (v2) | `discovery` | Embeds an inventory v2 draft, uses the real InventoryProbe shape (Grok 4), and drops the imagined AMD/sysfs text. |
| GPUProbe (one protocol, two shapes) | **Split** | `gpu-probe` | InventoryProbe and OccupancyProbe, each with the real `nvidia-smi` query (Grok 3, 4). |
| Systemd fake semantics | **New contract** | `unit` | Per-unit, fail-closed. A never-started unit is `absent` with an empty cgroup, as real systemd reports (Grok 5). |
| (none) | **Add** | `accounts`, `quota`, `usage`, `job`, `session`, `template`, `storage`, `power`, `window`, `notification`, `adapters`, `snapshot`, `legacy-compat` | The target model R1 needs these (TARGET-MODEL, GAP-MATRIX). |

## Invariants carried forward unchanged (decided, plan r4)

- One authority (SQLite) is the only writer of lane state. Admission, reservation and dequeue are atomic. Request ids are durable for idempotency.
- Lane lifecycle is `free -> (waking) -> starting -> running -> stopping -> free`, with `quarantined` whenever the state is uncertain. Expiry never proves an empty GPU.
- Class order is `operator > booked > batch > service > resident > standby`, FIFO within a class. operator, booked and batch may evict service, resident and standby, and nothing else. Grace periods are 120/120/300 s.
- Protected overruns quarantine and are never killed automatically. A stop applies only to the exact identity, after proof that the cgroup and GPU are empty.
- Executor beat every 60 s. Stale thresholds are 600 s (protected) and 180 s (preemptible). Clock skew above 30 s freezes calendar actions. UTC drives the calendar; host monotonic time drives deadlines.
- Booking rules: horizon, minimum length, early claim, check-in and no-show, warning and completion times as in `policy.timing`.
- Tailnet-only access. Identity comes from the server-side socket peer. Forwarding headers and caller identity claims are ignored.
- Tokens never appear in reads, events, logs or executor messages.

## Changed semantics (read these before implementing)

1. **Deadlines between hosts are durations** (`common#/$defs/relative_deadline`). The receiver anchors a deadline on its own monotonic clock and stores it as a `local_deadline`. A changed `boot_id` on read means the host rebooted (reconcile). It never means the deadline is foreign.
2. **Lease time**: `expires_at` is rolling (renew sets `min(now + ttl, approved_max_end)`). `approved_max_end` is the hard ceiling, computed as the minimum of the lane, quota and class ceilings. Only an approved `op-extend` moves it. A renew inside the ceiling **succeeds**.
3. **Liveness has three layers.**
   - Executor liveness: the authority beats every live lease every 60 s, carrying renewed durations.
   - Holder liveness: a client heartbeat, a unit, or TTL only (legacy). A stale client heartbeat triggers an occupancy check. An empty GPU releases the lease; a tenant quarantines it.
   - Host deadlines are enforced on the host even when the authority is gone.
4. **Identity**: `lease_id`, `run_id` and the unit name `flightctl-<lane>-g<gen>.service` are assigned **before** reserve and never change. `invocation_id` is systemd's own value, observed at start. Stop checks the full identity.
5. **Refusals are definite or uncertain.** A reserve rejected with `definite: true` wrote no fence, so the lease is cancelled and the lane stays free. An uncertain reply keeps exclusion.
6. **Errors** keep their cause chain (`typed_error.cause`) from executor to authority to RPC to CLI. A wrapper never replaces a lower reason with a generic string.
7. **Restart and reconcile are per lane.** The authority's own restart reconciles each lane by `inspect` before that lane admits again. No global freeze, and no reconcile stub that can never succeed.
8. **Power**: reads never wake a host. An acquire that needs a sleeping host returns `202 pending, reason waking`. A host that does not wake within `wake_timeout_s` becomes `unreachable` and is never `free`. The executor holds an `idle` inhibitor from reserve until verified release.
9. **Work**: a lease runs in `holder` mode (the caller runs its own process, as legacy did) or in `unit` mode (jobs, endpoints). Unit mode starts templated argv as a transient user unit. There is no shell and no image digest in R1.
10. **Shadow**: a lane in shadow mode makes every decision for real and executes no write. The legacy shim mirrors each call with `admission.shadow_of`, and the authority records a `shadow-decision` or `shadow-divergence` event for each mirrored call.

### Revision 2 (Sol 6 review, 2 Oct 2026; per-item record in `docs/v2/RESPONSE-TO-SOL6.tsv`)

11. **GPU emptiness includes aggregate memory** (B2). A lane is empty only if all of these hold:
    - the observation status is `ok`;
    - there are no tenants and no lease processes;
    - unexplained memory, meaning the lane's `memory.used` minus noise and lease processes, is below `lane_noise_mib`.

    A process whose memory reads unknown is a tenant. The executor's stop success requires `occupancy.empty: true`. Golden captures of real cards fix the rule.
12. **Features decide which ports are required; shadow writes nothing** (B3). `command_runner` is selected like every other port. Every mutating port is lane-scoped and is forced non-writing on a shadow lane.
13. **HTTPS at the peer-aware listener** (B4). The authority terminates TLS itself, with a certificate from the site's private CA (Amendment 1; the original plan's tailnet certificate would publish names in certificate-transparency logs), so the socket peer stays available to `tailscale whois`. The browser gets a secure origin for WebAuthn.
14. **Friends' sessions and units run under a minimal root helper** (B5).
    - Isolation comes from the device cgroup (`DeviceAllow`), not from an environment variable.
    - A session ends with `terminate-user` plus a slice stop, and the close is proven (`close_proof`).
    - Agents' unit work runs as a per-job systemd DynamicUser, never as the operator and never under a shared UID (D-run-2, D-iso-1).
    - Friend accounts wait for the owner's Q1 answer.
15. **Job outputs** are retrievable with `job-output-list` and `job-output-get` (chunked, sha256 per file, owner only) (B6).
16. **Ceiling invariant** (N1). The host sets max-end once, at reserve, with a skew margin, and it never moves later. Only the `extend` kind, carrying a consumed approval, can move it.
17. **Model files carry a required sha256** (N2). A cache entry is `present` only after hash verification.
18. **An explicit quota `max_lease_s` replaces the class default ceiling** (N3). It stays bounded by the lane cap and by 12 h.
19. **Shadow flips need path coverage and zero mirror errors** (N4), not only clean days.

### Revision 3 (Sol 6 round 2; each counterexample is a regression test in `tests/contracts_v2/test_v2_round3.py`)

20. **The inhibitor is proven in shadow through one validated exception**, `shadow_real: ["inhibitor"]`, under the legacy fence. That is the controlled proof path (B1).
21. **Occupancy processes are partitioned** (B2): every process is lease, noise or tenant. `tenants` and `noise` are exactly those partitions. Unknown memory is never noise and never empty.
22. **Live lanes are checked on their effective ports** (B3): required ports are evaluated with lane overrides applied.
23. **At most one active work item per friend per host** (B5): `409 account_busy`, enforced by the authority and again by the helper.
24. **One identity rule for everyone** (B6): the peer authenticates, and a token only selects or narrows from an allowed peer. Friends use their own tailnet identity and need no host account.
25. **Strict conformance and the migration gate are executable** (B7): a missing real twin is a setup error, never an xfail, and `tools/migration_gate.py` checks the real diff.
26. **The ceiling bound uses the authority's own round-trip time** (N1). A slow round trip triggers a shorten-only `ceiling` message, because clock skew cannot bound transport delay.
27. **Job-output reads are contained** (new): each path is walked from a directory fd with `O_NOFOLLOW`, regular files only, and an `expect_sha256` check refuses a file swapped between list and get.
28. **The helper's trust boundary is data** (`helper-config.schema.json`) plus an install audit of the real sudoers rule and stat chain (new).
29. **Token scrubbing is proven at storage level** (new, D-token-3): `secure_delete`, then a WAL checkpoint, then a `VACUUM INTO` backup. Measured: without `secure_delete` the bytes stay in the file.

### Revision 4 (Sol 6 round 3; counterexamples are regression tests in `tests/contracts_v2/test_v2_round4.py`)

30. **Occupancy names its expected lane cards** (B2). The observed cards must be exactly `expected_uuids`: none missing, extra or duplicated. A zero-GPU observation is never empty.
31. **Parent and child work** (B5). One active parent lease per friend per host. Sessions and jobs under that lease are children and take no extra slot. The parent is admitted atomically under a unique index, and the helper claims the same lease_id; both layers are race-tested with real processes.
32. **The principal always comes from the peer and is never supplied; token expiry and revocation are enforced** (B6). C2 names the boundary tests.
33. **The migration gate counts only replacement tests that pytest actually collects** (B7). It is tested end to end on a real git repo with the comment counterexample.
34. **No grant until the host acknowledges a ceiling within the approval** (N1). Replies carry `max_end_remaining_s`. If the correction is lost, the response is `202 ceiling-unconfirmed` and no grant is issued.
35. **Outputs go through trusted staging** (new). Executor-made copies are taken only of files with a single link that the job account owns, so hard links are never copied. Absolute paths are refused.
36. **The effective sudo audit runs on real `sudo -l` output**, so group and alias grants are visible (new).
37. **Raw replay tokens live only in a separate store that is never backed up** (D-token-4). A backup taken during the replay window holds no token.

### Revision 5 (Sol 6 round 4; regression tests in `tests/contracts_v2/test_v2_round5.py`)

38. **The helper's lease-bearing commands carry `--parent-lease`** (B5). The claim is released only after a proven close, and stale claims are reconciled from the authority's active parents, never by time or reboot alone. Claims live on persistent storage.
39. **Each agent job runs as its own systemd `DynamicUser`**, with a 0700 `StateDirectory`. Agents no longer share a UID, so one agent job cannot read another's staging. The shared `fc-svc` account is withdrawn. The real-host proof belongs to C7h.
40. **The two stores commit in a fixed order** (replay row first, then the main DB). Startup deletes orphan replay rows. A main DB restored without its replay store answers a retry with `409 replay_unavailable`; the lease goes through holder-lost handling and is never granted twice.
41. **Timestamps are compared as parsed instants, never as text** (B6).
42. **The migration gate's replacement must be collected in the packet's own test files**, or be the exact `path::name` the map names (B7).
43. **Ceiling acknowledgements are bound** to the reserve's lease, generation, host and request id (N1).
44. **The sudo audit requires `env_reset`, `!setenv` and `secure_path`.**
45. **The ROADMAP crosswalk is a scope map in schedule order.** The plan tests check its order and the milestone column (B1 regression).

### Revision 6 (Sol 6 round 5; regression tests in `tests/contracts_v2/test_v2_round6.py`)

46. **A friend's claim is removed only by `claim-release` with a close proof** (B5). Removal is race-safe: an inode-checked tombstone rename never deletes a replaced claim. Reconcile never frees a claim. A claim the authority omits becomes `orphaned-quarantined` and the operator is alerted.
47. **The replay deadline is kept in the main record** (token-free). A missing replay row inside the window returns `409 replay_unavailable`; after the window it gets the null-token grant replay.
48. **Ceiling grants require a reply in the executor wire shape**, with `ok: true` and `definite: true`, and a nested `echoed_identity` bound to the reserve.
49. **`output-collect` takes only the job id** and resolves it to the UID recorded at unit start. No service account is involved.
50. **The sudo audit requires `secure_path`** to be a non-empty list of standard root-owned system directories.

### Revision 7 (Sol 6 round 6; regression and self-adversarial tests in `tests/contracts_v2/test_v2_round7.py`)

51. **Quarantine has an admission effect** (B5). An `orphaned-quarantined` or `conflict` claim writes a quarantine marker. While the marker exists, every new parent and every child is refused, including children of the old parent. Only an operator `claim-clear` with the close proof lifts the marker. It removes the claim first and the marker last, so a crash leaves the account blocked.
52. **A ceiling can only be confirmed after a real reserve** (N1). The first counted reply must be a `reserve` with `observed_state: reserved`. A later `ceiling` reply counts only in a fenced state (`reserved`, `starting`, `running`). A dry-run, any other kind, or a free or unknown state never confirms.
53. **A duplicate `grant` passes the real retry time to the replay.** After the deadline, a retry gets the null-token replay even if the replay row has not been scrubbed yet.
54. **`secure_path` may not contain empty components.** The audit splits on both `:` and `\:`, so leading, trailing and doubled separators all fail.
55. **Statuses (N5).** The round-5 rows superseded by these fixes are marked `superseded`. Agent isolation stays `specified` until the root-only on-lab test runs.

### Revision 8 (Sol 6 round 7; regression tests in `tests/contracts_v2/test_v2_round8.py`)

56. **One admission rule** (B5). While the account is quarantined, no claim of any kind is admitted. Otherwise a request is admitted only as the account's first parent claim, or as a child of the claimed parent. The same sentence appears in SLICES C7h, `session-open` and `unit-start`. Admission and start, release and clear, and the reconcile marker write all share one per-account flock, so a quarantine cannot land between a child's admission and its start.
57. **claim-clear is the operator's own root-only program** (`/usr/local/libexec/flightctl-claim-clear`, mode 0700).
    - It is reached only through the operator's sudoers rule, which must re-authenticate (no NOPASSWD).
    - It checks SUDO_USER and SUDO_UID against `operator_account`.
    - The executor's helper has no claim-clear, and the install audit proves that the executor and friends have no route to it.
58. **A claim rollback deletes only its own inode.** It uses the tombstone check, so an interleaved clear and replacement is never deleted.
59. **A ceiling needs persisted fence evidence** (N1). A successful reserve reply must report its fence. It counts only with exactly one fence on the lane that carries the echoed identity, a state equal to `observed_state`, no reboot, and, where the lane requires one, a held inhibitor named for the lane and generation.
60. **Replay checks `request_fingerprint` first.** A mismatch is `409 conflict` at any time and never returns the stored response.
61. **Statuses (N5).** The round-6 B5 and N1 rows are superseded. Self-adversarial records use the new status `probes`.

### Revision 9 (Sol 6 round 8; regression tests in `tests/contracts_v2/test_v2_round9.py`)

62. **claim-clear needs fresh authentication on every invocation.** A password rule alone is not enough, because sudo accepts a cached credential for 5 minutes by default. The operator's rule must set `Defaults!<claim-clear path> timestamp_timeout=0`, and `!authenticate` is forbidden. The audit computes the EFFECTIVE value in the documented sudoers(5) precedence: matching Defaults, then runas-specific, then command-specific. The real-host proof is the on-lab C7h test `test_claim_clear_prompts_every_time`.
63. **Route matching in the C7h audit follows sudoers(5).**
    - It covers ALL, shell wildcards (which never match `/`), directory grants, `^...$` regexes and argument-bearing grants.
    - Cmnd_Alias is expanded from the installed sudoers text. An unknown alias, or a line it cannot parse, counts as a route (fail closed).
    - A negation never cancels a route.
64. **Idempotency and replay rows are keyed by (principal, request_id).** Another principal reusing a request id gets a fresh decision, never this principal's lease or token.
65. **Statuses (N5).** The round-7 B5 and request-fingerprint rows are superseded by round-8 rows.

### Amendment 1 (owner decisions of 2 Oct 2026; second commit after the freeze; see `docs/v2/AMENDMENT-1.md`)

66. **Friend sessions are flag-gated and off in R1** (Q1). `features.friend_sessions` is false, and so is every host's `friend_sessions_enabled` (rev 7/8: a host needs the global flag AND its own per-host flag with a recorded runbook proof bound to that host's identity, sshd configuration and evidence artefact; the helper sees both flags and re-checks the binding itself): friends get API-queued jobs under a per-job DynamicUser, like agents. The infrastructure for per-friend accounts, the helper's claims and quarantine, the session contract and C9 stays specified, built and tested.
67. **A7 uses a private-CA certificate** (Q2). The new `adapters.tls` block holds the cert, key and chain files and the rotation thresholds. It is CA-agnostic. The `tailscale cert` dependency is removed. Peer identity is unchanged. Revision 2: `tls.trust_anchor` lists the only roots a chain may terminate at. When the served certificate expires with no valid replacement, the listener stops serving TLS (fail closed).
68. **The sshd change is deferred to C9 enablement** (Q4). It happens through a lockout-safe runbook and only on `ssh_server: openssh` hosts. C9 tests against a dedicated test sshd. Revision 2: the runbook discovers the effective key settings with `sshd -T` and preserves them. Flag off refuses only the paths that create friend work; cleanup stays available.

## Lifecycle tables

Lease (`lease.state`): `waking -> starting -> running -> stopping -> closed`. Any state can become `quarantined` on uncertainty. `quarantined -> closed` happens only through `quarantine-clear`, with fresh inspect evidence.
Job: see `job.schema.json` `x-transitions`. A crash ends the job as `failed` and frees the lane after the emptiness proof. An uncertain stop leaves the job `lost` and the lane `quarantined`.
Endpoint: `loading -> ready -> draining -> unloaded`, with `quarantined` or `failed` when unload is uncertain. Only an explicit load reloads.
Session: `requested -> open -> warned -> closing -> closed`.

## Status codes and exits (unchanged)

`200` complete (exit 0), `202` pending (exit 5; `waking`, `release-confirming`, `staging`, `starting`, `stopping`), `403` denied (exit 2), `409` conflict or busy (exit 1), `503` unknown or unavailable (exit 3). Explicit yield exits 4.

## Authorisation (x-ops in `rpc-ops.schema.json`)

Each op has a role (member, holder, owner, operator), a token scope, and an approval rule. The approval rule is evaluated from `policy.approval_matrix`, which is data. The shipped default encodes the owner's 2 Oct decision: a FIDO2 touch is required only when someone else's work of the **same class** is pre-empted or overtaken. Other overrides need the operator role. Operator status is never self-asserted: it comes from the accounts document, via whois or a bearer token. MCP tools are exactly the rows with `mcp=true`, with no extra privilege.
