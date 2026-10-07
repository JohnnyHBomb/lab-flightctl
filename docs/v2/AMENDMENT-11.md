# Amendment 11: brief prep splits A5a3 (new A5a4) and A5b1 (new A5b1s, A5b1c); A5b2 before A5b1c; renew rows to A7b; the max-end margin is a policy constant; the authority units go to A7

- **Date:** 6 Oct 2026. **Approved:** by the owner, 6 Oct 2026 ("recommendations accepted"), from the brief-prep proposals
  for A5a3, A4ub, A5b1 and A5b2. It needs review before use, as `FROZEN.md` requires.
- **Basis:** the brief-prep stagings of A5a3, A4ub, A5b1 and A5b2 on provisional stacks over main `5aefe88` (A5a2 stand-in
  `89bca81`, A5a3 part 1 `8486622`, A5b1 part 1 `97ebd15`, A5b2 `0eeac41`), with A5a3 also checked on the real A5a2 part 1
  reference `3109395`. Written on main `336ed32` and rebased onto `15782b3` (after Amendment 10, the argv0 whitespace rule). The
  next number, Amendment 12, holds the wave-3 A5a2 and A11 splits. Revision 2: blind Fable review (REQUEST CHANGES:
  the A7b rows' bare replacement cannot pass G1b for A7b; A7 needs a row to edit A4ub's exact-list test; CONFORMANCE
  owners; an undecided rounding clause), all findings taken except two nits (listed in the PR).

## Reproduced first (measured in the stagings unless marked)
1. **A5a3 does not fit one race.** The packet as planned, as a compact lead reference, measured 371 changed lines before
   the four A4 transport rows: `executor_stdio.py` 95, `tests/executor/wire_site.py` 29, the two named realtime tests 91,
   `tests/conformance/impl_executor_transport.py` 156. Entries run 17-35% over their reference (lesson 94), which puts the
   packet at 434-500 lines against the 400-line cap. The conformance rig alone passed the frozen executor_transport cases
   strict host-local (10/10, fake and real, a stub nvidia-smi), so its size is not padding.
2. **The four A4 transport rows are not needed while v1 stays on the wire.** With the entry point routing
   `schema_version` 1 to the unchanged v1 `Executor` and 2 to `ExecutorV2`, all five tests of
   `tests/transport/test_a4_transport.py` pass unchanged on the A5a3 reference `8486622`. Amendment 6's reason for the rows
   ("A5a's v2 executor refuses them") holds only once the v1 wire is retired, which is A5r.
3. **A5b1 names eight acceptance tests** against the cap of 5. Part 1 (protocol-2 client, identity, definite refusal,
   cause, four named tests and the A0c repro) measured 237 changed lines packed and 312 in normal style. The token storage
   half measured 119 production lines with no test (`1b2843b`), and as sketched it breaks three frozen tests
   (`test_review3.py::test_frozen_rpc_vectors_against_production_handlers`,
   `test_review4.py::test_booking_claim_approval_binds_original_execution_rpc`: the v1 grant's `data.lease` must keep the
   lease-v1 shape; `test_revision2.py::test_revision2_transport_errors_and_store_events_never_export_secret_values`); with
   its tests it is estimated at 300-360 lines (inferred). Two named tests, `test_ceiling_needs_persisted_fence` and
   `test_replay_checks_request_fingerprint`, have the exact names of frozen oracle tests in `tests/contracts_v2/test_v2_round8.py`.
4. **A5b1c's grant rule needs A5b2's margin.** With the round-4 rule (`ceiling_confirmed`) acquiring through the real
   executor process: margin 0 and a 1.5 s added request delay sent `reserve, ceiling, ceiling, ceiling` and never
   confirmed; margin 30 with the same delay confirmed on the reserve. The bound confirms only when the round trip fits in
   the margin plus the sub-second rounding slack (inferred from the algebra; it matches every run). If A5b1c landed first,
   A5b2's `test_slow_round_trip_triggers_shorten_only_ceiling` would already pass on its base (G0 fails; measured: all three
   sim named tests of A5b2 pass with the round-4 rule in place).
5. **Rolling renew on the protocol-1 path breaks a frozen test with no row.** `tests/authority/test_review3.py` line 160
   (`send(3, ..., expected=409)  # already at booking bound`) fails `assert 200 == 409` under rolling renew. A5b2's
   prototype therefore rolls renew only under executor protocol 2, and its three renew rows (`test_revision2.py`,
   `tests/client/test_vectors.py`, `tests/contracts/vectors/cli.json`) went unused. The client vector formats a scripted
   response and never reaches the authority (read), so it stays green whatever A5b2 does (measured).
6. **The margin had no contract value** (read): `common.schema.json` `relative_deadline` names `margin_s`, no schema fixes
   it, and the round-3 oracle test uses 30.0. The A5b2 brief had to DECIDE 30 s itself.
7. **A4ub cannot prove the authority units** (read on `5aefe88`): no authority server exists before A7
   (`grep -rn "serve_forever\|HTTPServer" flightctl/` finds nothing), and the certificate renewer is the site CA's process
   (A7 design notes, Amendment 1), whose cadence couples to A7's `tls.rotation` settings.

## Changes
1. **A5a3 splits.** **A5a3** (the wire): the stdio entry point serves v2 through `ExecutorV2` (host id, lane cards from
   the confirmed inventory copy, the occupancy real twin), keeps schema_version 1 on the v1 executor until A5r, and the
   enforcer one-shot; `test_stdio_serves_v2_through_executor_v2` [realtime], `test_enforcer_one_shot_entry_point`
   [realtime]; no port; PROOF G0, G2, G4. A5a3 also depends on A4u (the entry point reads the A4u site copy; A4u is merged).
   New **A5a4** (after A5a3): executor_transport conformance [strict] over the A5a3 entry point (fake: in-process
   `ExecutorV2` per SimHost; real: local subprocess and ssh), port `executor_transport`, so the registry's `OWED_BY`
   names A5a4; PROOF G0 (REPRO-style: the fake cases are skipped while the port is unregistered), G2, G3, G4. A5b1 and
   A4ub keep their dependency on A5a3 (they need the real executor process, not the rig); A-ASM's range covers A5a4.
2. **The four A4 transport rows move from A5a3 to A5r**, same names (A5r rewrites the file in place when it retires the
   v1 wire, so the bare names count under G1b). A5r's SCOPE names them.
3. **A5b1 splits in three.** **A5b1** (part 1, staged): the protocol-2 client, identity before reserve, definite refusal,
   cause, the A0c identity repro rewritten onto protocol v2 (its row is unchanged); four named tests; `flightctl/store.py`
   leaves its SCOPE. New **A5b1s** (part 2): token storage, split store and replay rules: `test_token_scrub_storage_level`,
   `test_split_store_crash_and_restore`, and `test_store_replay_checks_request_fingerprint` (renamed). New **A5b1c**
   (part 3): no grant until the ceiling is acknowledged: `test_grant_withheld_until_ceiling_acknowledged` and
   `test_grant_needs_persisted_fence` (renamed). The test texts are the current A5b1 entry's, unchanged except the names.
   A5b1s's stager sizes it first; if it is over the line, the fallback cut (storage and scrub, then the replay rules) is a
   later amendment.
4. **Order: A5b1, A5b2, A5b1s, A5b1c, then A5r.** A5b2 keeps its dependency on A5b1. A5b1s depends on A5b1. A5b1c
   depends on A5b2, A5b1s and A5a2 (A5b2 for the margin and the ceiling sender, point 4; A5b1s as proposed, with no
   evidence for or against). A5r, A7, A7b and A8 keep their dependencies. The crosswalk lists
   `A5b1, A5b2, A5b1s, A5b1c, A5r` and `A5a, A5a2, A5a3, A5a4`.
5. **A5b2 rolls renew only under executor protocol 2**; the protocol-1 /v1 renew keeps its v1 behaviour until A7b. The
   three renew rows move from A5b2 to **A7b**, whose /v1 adapter retires the v1 renew semantics, with a new A7b row for
   `tests/authority/test_review3.py (renew vector index 3 expecting 409 'already at booking bound')`. All four name A5b2's
   exact node `tests/authority/test_a5b2_beat_renew.py::test_renew_rolls_within_ceiling_and_refuses_past_it` (the file the
   A5b2 brief decides), because A7b adds none of A5b2's tests and G1b counts a bare name only in a test file the same
   packet adds or modifies (measured by the review: the bare name gave "4 problems" for A7b, the exact node "0 problems").
   If A5b2's merged file differs, an amendment re-pins the node before A7b races. A5b2 keeps only its A0c repro row (rewritten onto protocol 2: the v1 wire cannot beat, measured
   15/15 "heartbeat identity mismatch").
6. **The max-end margin is a contract constant:** `policy.schema.json` `timing.max_end_margin_s`, `const` 30, required
   (the decided-constant pattern of `max_clock_skew_s`), in both schema examples and `config/policy-v2.json.example`.
   `common.schema.json` `relative_deadline` names it as its `margin_s`. A5b2's GOAL names it.
7. **A4ub's authority units move to A7.** A4ub's SCOPE drops `units/authority/*`; its index row now reads "enforcer
   one-shot service". A7's SCOPE gains `units/authority/flightctl-authority.service.template` (rendered with A4ub's
   `render_unit`) and the authority row of the `DEPLOY-LAYOUT.md` unit table; the certificate renewer is documented as a
   site process and templated only if it is a flightctl command. A7 gains no named test. A4ub's
   `test_templates_render_without_site_strings` asserts the template list is exactly the executor's two (A4ub brief), so
   a new A7 row (`tests/siteconfig/test_a4ub_units.py::test_templates_render_without_site_strings`, rewrite, same name)
   lets A7 append its template to that list; without it A7 would fail G1 FROZEN.
8. **Traceability.** New CONFORMANCE.tsv rows `amd11-1` to `amd11-5`. The G04, `grok-plan-gap` and plan-r4 host-deadline
   rows add A5a4 (and `grok-plan-gap` A7's unit template); the plan-r4 idempotency row names A5b1s instead of A5b1; the
   renew rows (plan-r4 renew, P0 and P3 vectors, P1, grok-new-2, G03) add A7b; `sol6r6-new` and `sol6r8-new` name A5b1s
   and `sol6r7-N1` A5b1c in their notes. The
   frozen plan tests that pinned the old owners are updated in place: `test_v2_amendment6.py` (A5a4, A5r) and
   `test_v2_amendment9.py` (A5a4, A5a3's new dependency, the A4 rows). `contracts/v2/MIGRATION.md` still names A5b1 and
   A5b2 for the renew rows and A5a for the executor rows: superseded by the tsv files and left unchanged, as in
   Amendments 7 and 9. The `RESPONSE-TO-SOL6.tsv` history rows are left as they are.

## Not in this amendment
The wave-3 sub-lead's A5a2b and A11b split proposals are Amendment 12.

## Checks
`tests/contracts_v2/test_v2_amendment11.py` (the splits, the renamed tests against the round-8 oracle names, the order,
the moved rows, the margin constant against the schema and the config example, the authority units);
`test_v2_plan.py` and `test_b7_migration_gate_on_this_branchs_real_diff` stay green.
