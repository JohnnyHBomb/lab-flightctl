# Amendment 9: wave-2 staging splits A4u, A3b and A5a; the v1-executor rows go to A5r; the branch migration test accepts a packet's own rows

- **Date:** 5 Oct 2026. **Approved:** pending the owner (proposed by wave-2 staging, 5 Oct 2026). Every change below is a
  proposal for the owner's approval. It needs review before use, as `FROZEN.md` requires.
- **Basis:** the wave-2 stagings of A3b, A4u, A5a and A6b, measured on main `8b86807`, and point 4 reproduced again for this
  amendment on the same commit. The races of A3b part 1, A4u part 1, A5a part 1 and A6b run on `8b86807` unchanged; this
  amendment changes nothing they are judged on.

## Reproduced first (measured)
1. **A4u does not fit one race, and no packet owns the command its timer runs.** A full A4u reference (loader, local copy,
   lane binding, five unit and timer templates with a renderer, all five named tests) measured 353 changed lines after
   trimming (379 before). Entries run 17-35% over their reference, which puts them at 410-480 lines against the 400-line
   cap. Part 1 alone (loader, local copy, lane binding, `DEPLOY-LAYOUT.md`, three named tests) measured 248. The timer runs
   "the executor enforcer every 60 s", but A4's entry point takes only `--state` and `--controller`, nothing runs the
   enforcer from a process, and A5a's scope owned the enforcer's semantics, not a command. The full reference's on-lab timer
   test passed host-local on a lab host's systemd 261 (1.59 s).
2. **A3b cannot retire v1 discovery in one race.** Removing the AMD path, `tests/fakes/fixtures/amd.json` and the two A3b-row
   tests measured 196 changed lines (+10 -186). Removing the private-CSV parser on top measured 71 more (-71), and then three
   frozen tests fail (`test_new_device_preserves_existing_identity_lane_and_exact_diff`,
   `test_driver_only_change_preserves_custom_ids_lanes_and_exact_diff`, `test_discovery_mutations_regression_matrix`
   [`empty_is_zero`]) and two pass only on empty device lists (`test_device_ids_are_order_independent_and_peer_addition_stable`,
   `test_long_host_collision_allocation_is_bounded`; read). None of the five had a row. Part 1 (inventory v2 and discovery v2
   beside v1) measured 251 changed lines.
3. **A5a does not fit one race, and its v1 rows cannot run before A5b2.** The in-process semantics with the inhibitor seam
   and the five named tests measured 376 changed lines, before request validation (a first validating draft had 348
   production lines alone), the conformance rig, the entry-point wiring and the A4 rows. Part 1 measured 312. The v1
   executor must stay until the authority speaks v2: `tests/sim/test_sim_rig.py`, the A5b1, A5b2 and A8 repros,
   `tests/executor/test_review4.py` and `tests/executor/test_mutations.py` drive it and have no A5a row.
   `test_mutations.py` runs 13 mutants against named v1 tests and asserts 13 v1 source lines occur exactly once: renaming
   the mapped `test_reserve_start_fence` fails it. Deleting `tests/executor/systemd_adapter.py` breaks the collection of
   `test_executor.py`, `test_p01.py`, `test_review3.py` and `test_review4.py` (the last has no row).
4. **The frozen branch test fails every packet that executes its own rows.**
   `tests/contracts_v2/test_v2_round3.py::test_b7_migration_gate_on_this_branchs_real_diff` ran
   `tools/migration_gate.py --base 59bdd7f --head HEAD --packet contracts-v2`, so a change to any test file that existed at
   `59bdd7f` failed it unless a contracts-v2 row named the file. Reproduced for this amendment: on `8b86807` plus a
   throwaway commit that executes A6b's own delete row (`test_fake_isolation_guard`), the test fails
   ("tests/executor/test_executor.py (M) is a pre-existing test/roster file with no migration-map row for contracts-v2")
   while `migration_gate.py --base 8b86807 --head HEAD --packet A6b` passes (1 file touched, 0 problems). The A3b and A6b
   staging gates measured the same failure. Since `59bdd7f` only `tests/integration/test_p6_scaffold.py` and
   `tests/roster/shims.py` had changed, both under contracts-v2 rows, so no merged packet had met it.
5. **A6b's two v1 rows are blocked only by point 4.** Size is not the reason: with them the A6b reference would be about
   337 lines (inferred: 301 measured plus about 36 removed lines).

## Changes
1. **A4u splits** (A4u staging report, section 7). **A4u** (part 1): `flightctl/siteconfig.py` (hash-verified load from the
   deploy dir, verified local copy, the confirmed-inventory lane binding), `docs/v2/DEPLOY-LAYOUT.md`, tests in
   `tests/siteconfig/`; `test_config_loader_rejects_hash_mismatch` [realtime], `test_local_copy_used_when_store_host_asleep`,
   `test_expected_uuids_come_from_confirmed_inventory`; closes G24; PROOF G0-G2, G4 (its on-lab test moved to
   A4ub). New **A4ub** (part 2): the unit and timer templates and a renderer; `test_templates_render_without_site_strings`,
   `test_timer_unit_runs_one_shot` [realtime, onlab]; closes the G04 timer; G0-G2, G4, the on-lab test on the pilot host. **A4ub depends on A4u
   and A5a3**, because A5a3 delivers the enforcer one-shot that A4ub's timer runs (point 1). A4ub therefore comes after
   A5a3 in the packet index, and the crosswalk lists S04 and S01-S02 twice to stay in index order (S10 and S22 already
   appear twice). B5 keeps its dependency on A4u: it needs the deploy-dir layout, not the unit templates.
2. **A3b splits** (A3b staging report). **A3b** (part 1): inventory v2 card binding and discovery v2 added beside v1
   (`flightctl/inventory.py`, additions to `flightctl/discovery.py`), the same four named tests, no migration row; closes
   G07. New **A3c** (part 2, owned by the lead and not raced, because the map dictates its deletions and LINES counts
   deletions): retire v1 discovery (the AMD path, the private CSV, the v1 proposal); closes Grok 4 and D-amd-1. A3b's three
   rows move to A3c, and A3c gets five delete rows for the `tests/discovery/test_discovery.py` tests of point 2. The two
   rewrite rows now name their replacements as exact nodes in `tests/discovery/test_a3b_discovery.py`, because a bare name
   counts only in a test file the same packet adds or modifies (`tools/migration_gate.py`, round 5). A3c's precondition
   is change 4. If the lead deletes the v1 `DiscoveryHandler` outright in A3c, one whole-file delete row replaces A3c's
   rows on that file (a further amendment, as the report notes).
3. **A5a splits in three** (A5a staging report). **A5a** (part 1): `ExecutorV2` in holder mode, in process, beside the v1
   `Executor`, plus the v1 cross-host fix; `test_relative_deadline_anchored_on_host_clock`,
   `test_beat_extends_expiry_never_max_end`, `test_stop_requires_reserve_identity_and_empty_proof`,
   `test_enforcer_real_seconds` [realtime] and the A0c repro; no port. **A5a2**: inhibitor-first reserve (D-pow-3), request
   validation, inspect/ceiling/extend; `test_definite_refusal_leaves_no_fence_and_no_inhibitor` (moved from A5a) and three
   new named tests. **A5a3**: the v2 executor on the wire (the stdio entry point serves v2 through `ExecutorV2`, the
   enforcer one-shot, executor_transport conformance [strict], the four A4 transport rows); it delivers the port
   `executor_transport`, so the conformance registry's `OWED_BY` names A5a3. Dependencies change: A5b1 on A5a3 (was A5a);
   A11 on A5a2 and A6 (was A5a and A6). The repro row of `tests/sim/test_repro_cross_host_reserve.py` stays A5a's.
   **The v1-executor rows** (the five `tests/executor/test_executor.py` rewrites, the `tests/executor/systemd_adapter.py`
   delete, the `test_p01.py` and `test_review3.py` rewrites) move from A5a to a new packet **A5r**, "retire executor v1"
   (owned by the lead, after A5b2, before A-ASM), with two new delete rows for `tests/executor/test_review4.py` and
   `tests/executor/test_mutations.py`. Their replacements are A5a's tests, named as exact nodes in
   `tests/executor/test_a5a_executor_v2.py` (A5r adds none of them). The staging report left a preference call: a new
   packet after A5b2, or the end of A5b2. This amendment proposes A5r, the smaller change that keeps A5b2's scope as
   planned. **That choice is the owner's call**: if the owner picks the end of A5b2, these rows' packet becomes A5b2 and
   A5r is removed.
4. **`test_b7_migration_gate_on_this_branchs_real_diff` is rescoped.** It reads the same diff (`59bdd7f..HEAD` under
   `tests/` and `roster/`, as `migration_gate.main` does). It passes when every pre-existing file the diff touches passes
   the unchanged per-packet check `migration_gate.check` (G1b) for at least ONE packet whose rows name that file. A file
   that no row names still fails. So do a whole-file delete row whose file was only modified, and a rewrite whose
   replacement is not a collected node (a bare name counts only in a test file added or modified since `59bdd7f`),
   but only when no other packet's rows pass on that file. Two limits, inherited from G1b and accepted here: a file
   passes on the rows of ANY packet that names it, and a node-level (`path::name`) delete row passes
   `migration_gate.check` unconditionally, so a file that carries one (today `tests/executor/test_executor.py`, A6b's
   `::test_fake_isolation_guard`) accepts any change in this test. Race entries are still gated per packet (G1b in the
   race gate). A later G1b amendment may count a `::` delete row only when its node is absent from the collection.
   `tools/migration_gate.py` and `test_b7_migration_gate_logic` are unchanged, and G1b stays per packet.
5. **A6b keeps its two v1 rows** (`tests/executor/test_executor.py::test_fake_isolation_guard` delete;
   `tests/contracts/test_fakes.py::test_fake_interfaces` rewrite, replacement `test_fake_runner_per_unit_fail_closed`). The A6b
   race leaves them out, and A6b's lead merge executes them once this amendment is on main. The SLICES.md A6b entry says
   so.
6. **Traceability.** New CONFORMANCE.tsv rows `amd9-1` to `amd9-5`. The G04, `grok-plan-gap` and plan-r4 host-deadline rows
   name A4ub and A5a3; `grok-new-4` names A3c; P7 `test_discovery_mutations` and P2 `test_executor_mutations` are `dropped`
   with the reason (A3c and A5r delete their v1 tests; no packet owes a v2 mutation matrix). The frozen plan tests that pinned the old owners are updated in
   place: `test_v2_amendment5.py` (the `amd.json` row is A3c's), `test_v2_amendment6.py` (A5a3) and
   `test_v2_amendment7.py` (A5r). `contracts/v2/MIGRATION.md` still names A5a, A6 and A3b for these rows: superseded by the
   tsv files and left unchanged, as in Amendment 7.

## Checks
`tests/contracts_v2/test_v2_amendment9.py` (the splits, the moved rows and the branch rule on offline cases);
`test_v2_plan.py` stays green. Change 4 was proven both ways on throwaway commits in a scratch clone of this branch: a
packet's own row executed (A6b's delete row; A3c's `amd.json` delete; A6b's rewrite with its replacement) keeps the test
green, and an unmapped pre-existing test edited, or a rewrite without its replacement, turns it red.
