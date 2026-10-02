# Amendment 2: Sol 6.1 cold review

- **Date:** 2 Oct 2026.
- **Basis:** the Sol 6.1 cold review of the whole set, `0b0938e` plus Amendment 1 at `ea14534`. It returned REQUEST CHANGES with five P1 contract defects and one P2 proof gap.
- **Form:** a new commit on top of Amendment 1. The frozen commit and Amendment 1 are unchanged.
- **Review:** this needs its own review before the set is used, as `FROZEN.md` requires for any contract change.

Each finding was reproduced on `ea14534` before it was fixed (measured, scratch `repro_amd2.py`). Each is recorded in CONFORMANCE as `sol61-*`. The tests are in `tests/contracts_v2/test_v2_amendment2.py`.

## P1-1: a memory threshold cannot establish exclusion

**Reproduced.** One stray 300 MiB CUDA process, and eight such processes totalling 2,400 MiB, both gave `empty=true` with no tenants. This contradicts T10(a).

**Measured on this controller** (driver 610.57.04, read-only `nvidia-smi`, no lane acquired, no GPU process started):
- `nvidia-smi -q -d PIDS` gives each process a `Type` of C, G or C+G.
- The desktop browser's GPU process holds a 5 MiB C+G context on the compute Titan, so it appears in `--query-compute-apps`.
- The compositor, Xwayland and terminal are G-only, so they do not appear there. The display card's 531 MiB shows in `memory.used` with no compute row.

**Fix.**
- Noise is an explicit per-lane allow-list of desktop IDENTITIES (`noise_allowlist`: `{argv0, uid}`), with an aggregate cap (`noise_cap_mib`), carried in the inventory's lane `external_tenant`.
- A process is noise only if its owner uid (from `/proc/<pid>`, set by the kernel), its argv[0] (from `/proc/<pid>/cmdline`, which only that uid's own processes can forge, so an entry trusts that uid) and its context type (G or C+G) match an entry.
- Root and DynamicUser uids are never noise.
- Every other process is a tenant WHATEVER its size. An unreadable identity is a tenant.
- Graphics-only memory with no compute row stays under `lane_noise_mib`.
- T10 agrees: (a) the ~300 MiB stray blocks admission; (c) the measured 5 MiB browser context on host C is noise because host C's lane lists it.
- The size threshold `process_noise_mib` is removed from the gpu-probe, inventory and executor contracts. T10's "thresholds as legacy README" pass line (lab ACCEPTANCE-TESTS) is superseded by this rule.

## P1-2: DynamicUser output ownership

**Reproduced.** A regular single-link file, as the host sees it under an id-mapped StateDirectory (owner = overflow uid), was rejected as "not owned by the job account".

**Fix: namespace-aware verification** (`output_owner_expected`, `collect_job_outputs`).
- The expected owner is the HOST-VISIBLE owner of the job's StateDirectory root, accepted only if it is the recorded runtime uid (no id-mapping) or the kernel overflow uid (id-mapped; `/proc/sys/kernel/overflowuid`, measured 65534 here). Any other owner collects nothing.
- Regular files only, `st_nlink == 1` and `O_NOFOLLOW` are kept, so foreign files and hard links stay rejected.
- The id-mapped behaviour comes from systemd's documentation (v261). It was NOT measured here, because a DynamicUser unit needs the system manager (root).
- The on-lab test `test_dynamicuser_write_stop_collect_fetch` (C7b1) records which owner the real hosts show, and proves write → stop → collect → authenticated fetch.

## P1-3: execution identity across ports and wire

**Reproduced.**
- `WorkloadRunner.start` had no work, lease or lane identity.
- `SessionGateway.open` had no parent lease.
- `Waker.wake` and the `ReleaseBackend` methods had no `timeout_s`.
- The executor workload had no work id.

**Fix.**
- `WorkloadRunner.start(…, work_id, lease_id, lane_id, parent_lease_id, …)`.
- `SessionGateway.open/close(…, parent_lease_id, lane_id …)`.
- Executor `workload.work_id` (required).
- Helper `unit-start --job <id> --lease <lease_id> --lane <lane>`, and `session-close --parent-lease`.
- A "no hidden adapter state" rule: an adapter never maps between identities it was not given.
- `timeout_s` on EVERY port method. Clock alone is exempt, because its reads are in-process.
- `OccupancyProbe.occupancy` takes the allow-list and cap.
- The conformance call sites are updated, and the registry documents the new fixture attributes.

## P1-4: frozen-test deadlock

**Reproduced.** The real gate checker refused a C7h edit of `test_v2_freeze.py`: "no migration-map row for C7h".

**Fix** (the lead's preference).
- The carried cases now assert against the PRODUCTION audit interface through an `install_audit` fixture: `flightctl.install_audit`, which C7h delivers (named in the C7h scope), with the frozen oracle standing in until then.
- They remain strict-xfail.
- Migration-map rows let C7h remove the two marks. The assertions never change.
- A0c's repros are pinned to four exact files, `tests/sim/test_repro_*.py`, with rows letting their fixing packets (A5a, A5b2, A5b1, A8) remove their marks.

**Proof (measured, scratch `c7h_gate_sim.py`).** A copy of the tree was given a simulated C7h change: a scratch production audit, and the two marks removed. On that diff:
- The REAL `tools/migration_gate.py` `check()` with its real pytest `collect()` passed.
- With the new rows removed, it failed with exactly Sol's message.
- The freeze tests pass in the simulated tree.
- A simulated A5a removal of an A0c repro mark also passes.
- The only substitution: a file comparison replaced the `git diff` step, so no git operations ran outside the worktree.

## P1-5: A-ASM T04 standby case

**Chosen: the smaller option, an explicit split** (no new execution path before A-ASM).
- **T04a, holder variant**, at A-ASM: D1, D2 and D3 as holder leases. D3's standby expiry ends the lease, and no successor is granted before the occupancy emptiness proof. Holder mode has no unit to stop.
- **T04b, managed-unit variant**, at C7b1 and per lane at C-ASM: D3 runs as a managed job unit. It is stopped at max_end plus grace, and the lane is freed only after the cgroup is gone and the emptiness proof.
- A-ASM's G6 list names the split, so it is not a silent substitution.

## P2: GPU job-path proof

**Per enabled lane, on-lab:**
- `test_cuda_job_production_path` (C7b1): a small CUDA template through the production job API and the helper under its DynamicUser; its GPU process is attributed to the lease; output fetched.
- `test_cancel_and_preempt_with_gpu_memory_allocated`, `test_controller_loss_during_gpu_job` and `test_no_successor_grant_before_teardown_verified` (C7b2).
- All of them are re-run per newly live lane at C-ASM.
- C9's security review gains `review-obligation-no-credentials-outside-window`: no authentication outside the managed window through preserved home key files, passwords, PAM, host-based, GSSAPI or Kerberos, or any other global credential source.

## Mutation checks (scratch `vac_amd2.py`, 11 of 11 caught)

- P1-1: size-based noise reintroduced; noise cap ignored; context type ignored; root/DynamicUser exclusion removed.
- P1-2: id-mapped branch removed; foreign directory accepted.
- P1-3: `Waker.wake` without a timeout; `start` without `work_id`; `open` without `parent_lease_id`.
- P1-5: A-ASM back to plain T04.
- P2: the controller-loss proof dropped.
- P1-4's negative control runs inside the test, and in the measured gate simulation.

## Revision 2 (Sol 6.1 review of Amendment 2, REVIEW-sol61-amd2.md: REQUEST CHANGES; amended before push)

All five issues were reproduced on `61aed50` first (measured, scratch `repro_amd2r2.py`).

1. **Context type per device entry.** The same allow-listed pid, C+G on card A and pure C on card B, gave `empty=True`. The type is now keyed by `(gpu_uuid, pid)` (`context_types`). A card with no reported type is null, never noise. A mixed same-pid capture is added.
2. **Freeze ledger.** `test_carried_obligations_are_recorded` now accepts exactly two reviewed states:
   - PENDING: both rows `carried`, sol6r8-B5 `specified`;
   - COMPLETED: all three `contract-fixed`, allowed only when the production audit passes both cases.

   A migration-map row covers the ledger test. The gate simulation now covers the WHOLE completion route (measured, `c7h_gate_sim.py`):
   - marks removed, ledger flipped and production audit present: the real gate passes and the whole contracts suite passes in the simulated tree;
   - ledger flipped without the production audit: it fails;
   - mixed ledger: it fails.
3. **C7b1 and C7c split.**
   - C7b1 proves write → stop → collect. It records the actual directory and file owners, verifies the overflow value, and binds the job by the root-owned registry plus its exact private StateDirectory, never by the numeric owner.
   - C7c proves the authenticated fetch (`test_dynamicuser_output_authenticated_fetch`, `test_cuda_job_output_fetched`).
   - C-ASM keeps the non-droppable `test_job_output_end_to_end`.
4. **T04 sweep and plan check.** B-ASM now re-runs T04a, and the table is reconciled. The SPLIT NOTE lines name the split. A plan check fails if:
   - a table row runs before its Needs;
   - an acceptance names a T-number that is not a table row, or one whose Needs come later;
   - an acceptance uses a later packet's capability (job-output-get/list, T04b).

   It runs on the live plan, and three inversions are kept as failing cases.
5. **Conformance regression.** `SessionGateway.close` now passes `parent_lease_id`. A NON-SKIPPED test binds the port calls of mapped receivers in the conformance skeletons (simple and, from revision 3, dotted such as `rig.transport`) to the declared Protocol signature with `inspect.signature().bind`; unmapped receivers that call port-method names fail.

**Also:**
- Stale prose is reconciled: "above the noise floor"; recorded-job ownership in the executor, README and C7h text.
- Sol's residual judgements are named obligations: A3/B4 `test_desktop_user_legacy_jobs_remain_tenants` (multi-card; no isolation claimed from code running as the trusted uid); C7b1's owner and overflow recording and its registry binding; the CUDA proof reads staged model data; C7b2 `test_active_host_loss_during_gpu_job`, separate from controller loss.

**Mutation checks:**
- per-card type falling back to any card's type: caught;
- a fallback only when the type is missing: caught, by a test added after the first mutant was NOT caught;
- a ledger test without the production check would accept completion without the production audit (shown, so the check is load-bearing);
- plan inversions and the close regression: caught in-test;
- the earlier 11 still caught.

## Revision 3 (Sol 6.1 APPROVE WITH CHANGES at f2b573f, REVIEW-sol61-amd2r2.md: two non-blocking guard gaps; amended before the PR)

Test and guard changes only. All three of Sol's mutations were reproduced as accepted on `f2b573f` first (measured, scratch `repro_amd2r3.py`).

1. **Conformance binding guard.** Dotted receivers are now resolved and bound: `rig.transport` is mapped to `ExecutorTransport`, which covers 6 calls in the transport skeleton. Sol's mutation, removing `timeout_s` from `rig.transport.call(...)`, is now refused, and an unmapped dotted receiver calling a port-method name is refused too. **Bounded:** the guard checks the arity and keyword names of calls on mapped receivers. It does NOT check argument values, or typos in method names on a mapped receiver, which read as fixture helpers.
2. **Plan guard.**
   - T05's current-rule and target-rule variants are distinguished. A reference must name its variant, and that variant's Needs are checked.
   - The capability map is DERIVED for the RPC ops: each hyphenated op in `rpc-ops.schema.json` x-ops is owned by the earliest packet whose SCOPE or GOAL names it, so `job-logs` → C7c, `job-submit` → C7b1, and so on. Only T04b is still hand-listed.
   - Sol's two mutations are refused: `job-logs` in C7b1 acceptance, and A-ASM's T05 switched to the target variant. So is a T05 without its variant.
   - **Bounded:** plain-word ops (acquire, release, status and so on) are too common in prose to check. Ops that no SCOPE or GOAL names have no derivable owner and are not checked. A same-named helper subcommand counts as delivery (`session-open` in C7h). Capabilities described in free prose are not seen.

**Mutation checks** (scratch `vac_amd2r3.py`), all caught:
- dotted resolution removed;
- derived op owners removed (hand list only);
- T05 variant filtering removed. This one is caught as a false positive on the live plan, which is what the filter prevents. The inversion case alone does not catch it, because unfiltered checking still flags the target variant.

## Not in this amendment (the owner's call)

- Moving C9 construction out of R1. The cost is set out in `docs/v2/BLAST-RADIUS.txt`, section "C9 DEFERRAL OPTION".
- Choosing a single authoritative representation, and the packet caps.

## Blast radius

- **Site files:** inventory lane `external_tenant` must carry `noise_cap_mib` and `noise_allowlist`; `process_noise_mib` is gone. Executor start messages must carry `workload.work_id`. Port implementations must take the new arguments.
- **Packets:** A3 (identity sources), B4 (collision rule), A6, A12, B5 and C9 adapters (signatures), C7h (production audit module; its mark-removal rows), A0c (exact repro file names), A-ASM (T04a), C7b1/C7b2/C-ASM (T04b and the GPU job proofs).
- **Unchanged:** v1 contracts and `lanes.sh`. No packet is removed.
- **Lab drafts:** the inventory draft is updated on the lab side. host C's lane allow-lists the measured browser identity; the other lanes have none yet, which is fail-closed.
