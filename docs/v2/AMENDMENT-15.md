# Amendment 15: one migration row so A5b2 can change the reserve deadlines that A5b1's test pins

- **Status:** DRAFT, revision 2. `FROZEN.md` requires review before use (blind review, then the owner). Revision 2:
  blind Fable review APPROVE WITH CHANGES, findings 1-9 taken.
- **Date:** 8 Nov 2026. **Approved:** not yet.
- **Basis:** the A5b2 revalidation on main `c3a1bfb` (restart step 4, 8 Nov 2026). No packet, port, dependency, gate or
  named test of `SLICES.md` changes.

Labels: **measured** = run on `c3a1bfb` for this amendment; **inferred** = a conclusion from the evidence.

## Reproduced first
1. A5b1 (PR #49) added `tests/authority/test_a5b1_executor_client.py`. Its test `test_release_identity_matches_reserve`
   asserts at line 95 that the protocol-2 reserve carries exactly one deadline, max-end, with `in_s` = the whole seconds
   to the lease's `max_end` (measured: `assert reserve["deadlines"] == [{"kind": "max-end", "in_s": 3600, ...}]`).
   That was A5b1's decided shape (its brief, DECIDED 5; the brief is not in this repository).
2. A5b2 must change both parts. Amendment 11 makes max-end `approved_max_end - sent_at - timing.max_end_margin_s`
   (30 s): that alone fails the pinned line (3570 != 3600). The A5b2 brief (DECIDED 4-5) also puts the rolling expiry
   and the heartbeat-stale duration on the reserve, so the host enforces liveness before the first beat (README changed
   semantics 3); that adds the two items. With the A5b2 reference ported onto `c3a1bfb`, the whole suite gives 1 failed, 1450 passed, 54 skipped,
   3 xfailed. The one failure is that assertion (measured: `in_s` 3570 != 3600, and two more items).
3. The file has no migration row, so G1b (`tools/migration_gate.py --packet A5b2`) fails any change to it (measured:
   `migration_gate.check` on the base map reports "no migration-map row for A5b2"; the row below is what lets it pass).

## Change
1. `docs/v2/migration-map.tsv` gains one row: `tests/authority/test_a5b1_executor_client.py::test_release_identity_matches_reserve`
   (the reserve deadlines assertion), packet A5b2, `rewrite`, with the replacement
   A5b2's exact node `tests/authority/test_a5b2_beat_renew.py::test_beat_loop_keeps_lease_alive_30min_sim` (A5b2's named test that asserts the reserve's
   three deadlines, in the file the A5b2 brief decides). The exact node, as in rows 7-10, because a bare name would
   also match a same-named test in any test file A5b2 modifies, this file included; `test_v2_plan.py` requires only
   that the replacement appears in `SLICES.md`, which the A5b2 SCOPE line now names (change 4). Only that one
   assertion line may change, in place, to the three deadlines. Every other line of the file stays as it is. G1b is
   file-level: this row licenses A5b2 to touch the file; it does not check how much changed. The one-line limit is a
   scope rule of the A5b2 brief (its FROZEN list and DECIDED 1), enforced by the race harness's allow-list, which names
   the exact old line, and checked at review; any other change to the file fails the packet.
   Supersedes the Amendment 11 change-5 sentence 'A5b2 keeps only its A0c repro row': A5b2 now has two rows (the repro
   and this assertion). Amendment 11's text is not edited.
2. `tests/contracts_v2/test_v2_amendment11.py::test_renew_rows_go_to_a7b` pins the set of A5b2 rows (measured: it fails
   with the new row). Its A5b2 assertion is updated in place to include the new row (same assertions otherwise), as
   Amendment 14 change 4 did for the A1 clock test.
3. `CONFORMANCE.tsv` row `amd15-1` (note: its executable pin).
4. `SLICES.md` A5b2 SCOPE names the assertion and its replacement node (the precedent of Amendment 11 naming
   `test_templates_render_without_site_strings` in A7's SCOPE). No packet, port, dependency, gate or named test changes.

## Checks
`tests/contracts_v2` passes with the row and the updated pin (measured); without the pin update
`test_renew_rows_go_to_a7b` fails (measured). Measured: whole suite on `c3a1bfb` + the ported
A5b2 reference: 1 failed, 1450 passed, 54 skipped, 3 xfailed; `tests/contracts_v2` on this branch: 858 passed, 2 xfailed.
Whole suite on this branch: 1446 passed, 54 skipped, 4 xfailed, the same as `c3a1bfb`.

## Dated decisions
| Date | Decision | By |
| --- | --- | --- |
| 2026-11-08 | Draft: the A5b1 deadlines assertion gets an A5b2 rewrite row | lead's staging agent (needs review and the owner) |
| 2026-11-08 | Revision 2 (blind Fable review, APPROVE WITH CHANGES): replacement is A5b2's exact node; the one-line limit is the brief's scope rule, not G1b; supersedes Amendment 11's "A5b2 keeps only its A0c repro row" | lead's staging agent (needs the owner) |
