# Amendment 7: A6 splits into A6 (real twin) and A6b (dryrun twin, FakeRunner, v1 fake retirement)

- **Date:** 4 Oct 2026. **Approved:** by the owner, 4 Oct 2026 (the split, and G3's one non-sleep unit). It needs review
  before use, as `FROZEN.md` requires.
- **Basis:** race R-A6 staging (measured on main `c74bfea` and on a lab host's systemd 261).

## Reproduced first (measured)
1. **A6 does not fit the 400-line cap.** A full A6 reference measured 541 changed lines (runner 222, tests 152, fake 89,
   registration 37, migration rows -36, rest 5). The real twin alone measured 331.
2. **`tests/executor/systemd_adapter.py` cannot be deleted in A6.** `tests/executor/test_executor.py:13`,
   `tests/executor/test_p01.py:20` import `IsolatedSystemd` from it, `tests/executor/test_review3.py:8-9` through the re-export in
   `test_executor.py`, and
   `make_executor` uses it as the executor's systemd seam. It goes with A5a's executor-test rewrite.
3. **G3 "sleep units only" conflicts with a frozen case.** `test_units_are_isolated_and_crash_is_observed` starts one unit
   running `sh -c "exit 3"` to observe a crash.

## Changes
1. **A6 (part 1):** the real twin and its real-only conformance registration; named tests
   `test_real_twin_reads_systemd_captures`, `test_crash_observed_and_cgroup_empty` [realtime, onlab],
   `test_unit_and_inhibitor_survive_logout` [onlab]. A6 keeps the port `workload_runner` (the real twin delivers it).
2. **New A6b (part 2, depends on A6):** the dryrun twin, the per-unit fail-closed FakeRunner with `script_next`, their
   registrations, `test_fake_runner_per_unit_fail_closed`, `test_dryrun_never_starts` [onlab], and the migration rows
   `test_fake_isolation_guard` (delete) and `test_fake_interfaces` (rewrite), now owned by A6b. A6b precedes A-ASM.
3. **`tests/executor/systemd_adapter.py` delete row moves to A5a**, with two new A5a `rewrite` rows for
   `tests/executor/test_p01.py` and `tests/executor/test_review3.py` (revision 2, Fable review: the gate is row-based, and
   both files change when the adapter goes).
4. **G3 for A6** allows `sleep` units plus that one `sh -c "exit 3"` unit (it exits at once and touches nothing); every
   strict conformance case runs.
5. CONFORMANCE.tsv rows that named A6 for the fake and its v1 test now name A6b (rows P0 `test_fake_interfaces`, P2
   `test_fake_isolation_guard`, `grok-new-5`). `contracts/v2/MIGRATION.md` and `ADAPTERS.md` still say A6: superseded by
   the tsv files, left unchanged.
6. **G3 wording** also names the on-lab tests' units (`sleep`, and one `systemd-inhibit ... sleep` for the linger probe).

## Checks
`tests/contracts_v2/test_v2_amendment7.py`; `test_v2_plan.py` stays green.
