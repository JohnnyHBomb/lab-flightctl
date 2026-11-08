# Amendment 15: one migration row so A5b2 can change the reserve deadlines that A5b1's test pins

- **Status:** DRAFT, revision 1. `FROZEN.md` requires review before use (blind review, then the owner).
- **Date:** 8 Nov 2026. **Approved:** not yet.
- **Basis:** the A5b2 revalidation on main `c3a1bfb` (restart step 4, 8 Nov 2026). No packet, port, dependency, gate or
  named test of `SLICES.md` changes.

Labels: **measured** = run on `c3a1bfb` for this amendment; **inferred** = a conclusion from the evidence.

## Reproduced first
1. A5b1 (PR #49) added `tests/authority/test_a5b1_executor_client.py`. Its test `test_release_identity_matches_reserve`
   asserts at line 95 that the protocol-2 reserve carries exactly one deadline, max-end, with `in_s` = the whole seconds
   to the lease's `max_end` (measured: `assert reserve["deadlines"] == [{"kind": "max-end", "in_s": 3600, ...}]`).
   That was A5b1's decided shape.
2. A5b2 must change both parts. Amendment 11 makes max-end `approved_max_end - sent_at - timing.max_end_margin_s`
   (30 s), and A5b2's plan entry makes the reserve carry the rolling expiry and the heartbeat-stale duration that the
   beats renew. With the A5b2 reference ported onto `c3a1bfb`, the whole suite gives 1 failed, 1450 passed, 54 skipped,
   3 xfailed. The one failure is that assertion (measured: `in_s` 3570 != 3600, and two more items).
3. The file has no migration row, so G1b (`tools/migration_gate.py --packet A5b2`) fails any change to it (inferred
   from the gate's rule; the row below is what lets it pass).

## Change
1. `docs/v2/migration-map.tsv` gains one row: `tests/authority/test_a5b1_executor_client.py::test_release_identity_matches_reserve`
   (the reserve deadlines assertion), packet A5b2, `rewrite`, with the replacement
   `test_beat_loop_keeps_lease_alive_30min_sim` (A5b2's named test that asserts the reserve's three deadlines; a bare
   name, as `test_v2_plan.py` requires). Only that one assertion line may change, in place, to the three deadlines. Every other
   line of the file stays as it is. The race's FROZEN_ALLOW names the exact old line.
2. `tests/contracts_v2/test_v2_amendment11.py::test_renew_rows_go_to_a7b` pins the set of A5b2 rows (measured: it fails
   with the new row). Its A5b2 assertion is updated in place to include the new row (same assertions otherwise), as
   Amendment 14 change 4 did for the A1 clock test.
3. `CONFORMANCE.tsv` row `amd15-1`.

## Checks
`tests/contracts_v2` passes with the row and the updated pin (measured); without the pin update
`test_renew_rows_go_to_a7b` fails (measured). The whole
suite on this branch matches `c3a1bfb` (measured; counts in the commit message).

## Dated decisions
| Date | Decision | By |
| --- | --- | --- |
| 2026-11-08 | Draft: the A5b1 deadlines assertion gets an A5b2 rewrite row | lead's staging agent (needs review and the owner) |
