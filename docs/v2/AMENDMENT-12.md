# Amendment 12: wave-3 staging splits A5a2 (new A5a2b, request validation) and A11 (new A11b, reconcile and the guard proof)

- **Date:** 7 Oct 2026. **Approved:** by the owner, 6 Oct 2026, from the wave-3 stagings' split proposals for A5a2 and
  A11. It needs review before use, as `FROZEN.md` requires.
- **Basis:** the wave-3 stagings of A5a2 (references `8abc307` and `3109395` on main `5aefe88`) and A11 (reference `7489338`
  on the provisional base `3109395`). Stacked on Amendment 11. Both part-1 races were judged on exactly the scopes written
  below: R-A5a2 raced A5a2 part 1 on `5aefe88` (its harvested merge branch, `c96fc65`, not yet a PR, holds the three named
  tests in `tests/executor/test_a5a2_executor_v2.py` and no request validation), and R-A11 races A11 part 1 on `3109395` (three twins, no executor change). This
  amendment changes nothing either race is judged on.

## Reproduced first (measured in the stagings unless marked)
1. **A5a2 does not fit one race at entry style.** The full reference (inhibitor, validation, inspect/ceiling/extend, four
   named tests, `8abc307`) measured 301 changed lines packed and 543 with `black -l 120`; at the measured 1.52 entry factor
   that is about 357. Part 1 without request validation (`3109395`, three named tests) measured 219 packed and 398 with
   black, about 262. Request validation is self-contained: a standard-library interpreter of the frozen schema, its named
   test and a generated corpus. It touches no inhibitor, inspect, ceiling or extend semantics.
2. **Validation is a real packet.** On the base, schema-invalid reserves are served (a fence is written) with an undeclared
   key, a boolean generation, schema_version 1 or a protected-and-preemptible policy; others raise out of `handle`
   (KeyError, TypeError, ValueError for `sent_at` "notadateZ" and a NaN `in_s`).
3. **A11 does not fit one race.** Part 1 alone (three twins in `flightctl/power.py`, the conformance registration, three
   named tests, the ci.yml append) measured 307 changed lines (`git diff --numstat 3109395 7489338`), the lesson-94
   ceiling. Part 2 (the `ExecutorV2` reconcile hook, its named test, the on-lab guard test) is not built; it is estimated
   at 100-110 more lines (inferred), so the packet as written would be above the 400-line cap.

## Changes
1. **A5a2 splits.** **A5a2** (part 1, as raced): inhibitor-first reserve and its definite/uncertain refusal (D-pow-3), the
   inhibitor released only after the verified release (a quarantine keeps it), inspect/ceiling/extend;
   `test_definite_refusal_leaves_no_fence_and_no_inhibitor`, `test_ceiling_shortens_only_and_extend_needs_approval`
   [realtime], `test_inspect_reports_holder_unit_absent`. New **A5a2b** (after A5a2): every v2 request validated against
   the frozen `executor.schema.json#/$defs/request` (definite `invalid`, nothing written, no port called; the date-time
   format and finite numbers asserted) by a standard-library interpreter; `test_invalid_requests_are_definite_and_write_nothing`;
   PROOF G0, G2. **A5b1 depends on A5a3 and A5a2b** (the authority must not see an executor that serves schema-invalid
   requests); A5a3 keeps `A5a2, A4u`. Neither part owns a migration row.
2. **A11 splits.** **A11** (part 1, as raced): the Inhibitor port's real twin, dryrun and fake in `flightctl/power.py`,
   `tests/conformance/impl_inhibitor.py`, `tests/power/` and its ci.yml append; inhibitor conformance [strict],
   `test_inhibitor_failure_is_definite_refusal`, `test_real_twin_runs_the_contract_commands`,
   `test_dryrun_twin_lists_for_real_and_records_the_rest`. The executor hooks leave its SCOPE: A5a2 owns reserve and
   release (point 1), A11b the reconcile. New **A11b** (after A11, before A12): the `ExecutorV2` reconcile over the
   Inhibitor port (a fence that records a held inhibitor whose unit is missing gets it back; a `flightctl-awake-*` unit
   with no such fence is released); `test_reconcile_recreates_missing_and_removes_orphan_inhibitors`,
   `test_guard_sees_inhibitor` [realtime, onlab]; PROOF G3 on the pilot host. A11 keeps `A5a2, A6` and the port; A12 keeps
   `A11, A7b, A10` (the waker needs only the port); A-ASM's `A0a-A12` range covers A11b.
3. **Crosswalk.** `A5a, A5a2, A5a2b, A5a3, A5a4` and `A11, A11b, A12` (index order). The G6 row T07 names A11b, whose guard
   test is that row's first real evidence. The frozen negative case in
   `test_v2_round7.py::test_self_plan_checker_accepts_the_real_plan_and_catches_swaps` builds its swap from the exact
   `A11, A12` row text, so it is updated in place to the new text (same swap, same expected "crosswalk order").
4. **Traceability.** New CONFORMANCE.tsv rows `amd12-1` and `amd12-2`; `amd9-3` notes that validation is A5a2b's; G14 adds
   A11b. The frozen plan tests that pinned A5b1's dependency are updated in place: `test_v2_amendment9.py` and
   `test_v2_amendment11.py`.

## Checks
`tests/contracts_v2/test_v2_amendment12.py` (both splits against the raced named tests, dependencies, index and crosswalk
order, no migration rows); `test_v2_plan.py`, `test_v2_round7.py` and `test_b7_migration_gate_on_this_branchs_real_diff`
stay green.
