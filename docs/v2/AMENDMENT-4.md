# Amendment 4: adapter and plan follow-ups from race R-A0b

- **Date:** 2 Oct 2026.
- **Basis:** the R-A0b harvest log, which carries four items left for the lead. Also HARNESS-LESSONS row 71, and the owner's line-cap rule of 2 Oct.
- **Form:** one new commit on `main` (c676b2c: contracts v2, Amendments 1-3, A0a and the merged A0b). It needs review before use, as `FROZEN.md` requires.

## Reproduced first (measured on c676b2c, scratch `repro_amd4.py`)

1. **`adapter-refused` event.** `event.schema.json` has no `adapter-refused` kind, but ADAPTERS rule 8 asked for "an `adapter-refused` event".
   - The merged `flightctl/adapters.py` prints `adapter-refused: port=... lane=...: ...` lines on stderr and exits 3.
   - SLICES A0b asks only that "Refusal exits 3 and names the port and lane".
2. **Example file name.** ADAPTERS.md and the adapters schema description named `config/adapters.json.example`. That file does not exist; `config/adapters-v2.json.example` does.
3. **`shadow_real` hole.** The tested configs had `shadow_real` set to `["workload_runner"]`, `["waker"]` or `["model_cache"]`, with `legacy_lane` set and that port overridden to `real`.
   - The oracle reported 0 problems.
   - The schema reported 1 error, and so did the merged module, through its schema walker.
4. **Sim-lane override.** In profile `sim`, a `sim` lane with the override `workload_runner: real` was accepted by the oracle, the schema and the merged module (0, 0 and 0 problems).
5. **ci.yml allowance.** The scopes of A0b (`tests/adapters`) and A0c (`tests/sim`) do not mention ci.yml, yet the frozen A0a test requires each new `tests/<dir>` in the `full` job (lesson 71).
6. **Line cap.** GATES G1 states the ~400-line cap, with no rule for harvested merges.

## Changes

1. **The `adapter-refused` event requirement is dropped** (the smaller sound route). ADAPTERS rule 8 now says what refusal is: exit 3, plus one stderr line per problem, `adapter-refused: port=<port|-> lane=<lane|->: <message>`. That is exactly what the A0b brief asked for and what the merged module does.

   **Why not add the event kind:** an event needs a gap-free `seq` committed in the authority store's transaction. A process whose adapter selection is refused has not opened that store, and an executor never has one, so the event would be unimplementable for half the processes. It would also add a schema kind and its detail shape for a record nobody can write at that moment.

   *Revision 2:* the notification topic `adapter-refused` and the error code `adapter_refused` are now removed (Sol 6.1's recommendation; see below).
2. **The example file name** is now `config/adapters-v2.json.example` in ADAPTERS.md and in the schema description.
3. **The oracle matches the schema for `shadow_real`.** `adapters_semantics` refuses any port other than the inhibitor.

   The merged module already refused it and still agrees:
   - the A0b tests pass;
   - a differential over 1008 configs found 0 accept/refuse mismatches. The corpus is every lane × port × implementation override, every profile, every lane mode and every `shadow_real` port, built from the example and from a sim site. Contract and module each accept the same 112 configs.
4. **Sim-profile lane overrides: decided "not intended", and refused.** Rule 7 ("The `sim` profile refuses `real`, `dryrun` and `record` ports") and "`sim` (all fakes, one SimClock per host)" now apply to EFFECTIVE ports, as rule 4 does for live lanes since round 3. A sim site that lets a lane reintroduce a real port could make a host write from a "sim" site.
   - **Schema:** a top-level `allOf` (if `profile` is `sim`, every lane's `ports` value must be `fake`). *Revision 2 removed this `allOf`, the oracle rule and the module line below: they are subsumed by the sim-lane rule (a sim lane is fake in every profile) and the off-lane rule. Their mutants were no longer caught, so they did not earn their place.*
   - **Oracle:** a new rule in `adapters_semantics`.
   - **Module:** ONE contract-driven line in `flightctl/adapters.py` `check_config`, because its frozen-schema walker does not evaluate `if`/`then`:

     ```python
     problems.extend(Problem(p, lane, f"sim profile refuses {i} port {p} on lane {lane}") for p, i in overrides.items() if profile == "sim" and i != "fake")
     ```

   *Revision 2:* sim lanes in `live` and `shadow` sites are now decided too (see below).
5. **ci.yml append allowance (lesson 71).** Two places now say it:
   - the SLICES "Rules every brief carries" section: every packet's scope implicitly allows ONE change outside it;
   - GATES G1 "CI APPEND".

   A packet whose diff adds a new `tests/<dir>` with `test_*.py` appends exactly the token `tests/<dir>` to the suite step of the `full` job. Nothing else in ci.yml may change. The race gate's CIYML check (`--ci-append tests/<dir>`) enforces this (revision 3).

   *History:*
   - **Revision 1** claimed this enforcement.
   - **Revision 2:** Sol 6.1 showed the check then removed the first occurrence of the token anywhere in the file, so it accepted the wrong job or step, a duplicate and a directory the packet never added. The claim was withdrawn, and the reviewer checked by hand meanwhile.
   - **Revision 3:** the fixed check accepts only the proper append (see below).
6. **Line cap** (the owner's rule, GATES G1 "LINE CAP" and the SLICES size rule):
   - The ~400 changed-line cap is a HARD gate for every race entry.
   - A harvested merge (the winner plus runners-up ideas) may exceed it only when the harvest brings a real improvement.
   - The merge report then states the total changed-line count and the line cost of each harvested item.

Records: CONFORMANCE rows amd4-1 to amd4-6; README item 78; tests in `tests/contracts_v2/test_v2_amendment4.py` (67 cases).

## Checks
- **Mutation checks** (scratch `vac_amd4.py`): each fix was reverted in a scratch copy and judged by the new test file plus `tests/adapters`. 8 of 8 reverts were caught:
  - the module line;
  - both oracle rules;
  - the schema `allOf`;
  - the event wording;
  - the example name;
  - the harvest wording;
  - the ci.yml allowance.
- The full suite, portability, denylist, whole-history scan, gitleaks and the lab-ci `pr` job are reported in the hand-back.

## Revision 2 (Sol 6.1 review: REQUEST CHANGES on one inherited defect; recommendations taken on both open questions)

Amended in place on c676b2c. Each item was reproduced first (scratch `repro_amd4r2.py`, measured on 9b08045).

**Reproduced:**
- **Rule 6 on lanes:** `lane-gpu1` (live) with `{"health_probe": "dryrun"}` gave schema 0, oracle 0, module 0, and the selection resolved `health_probe` to `dryrun`. A sim lane with `{"command_runner": "dryrun"}` behaved the same.
- **Sim lane in a live site:** the overrides `workload_runner: real`, `dryrun` and `record` were each accepted by all three. A sim lane with no overrides inherited `command_runner: real`.
- **Semgrep** (measured in `lab-ci-python:3.12`): on a git clone, `semgrep scan --config p/python deploy tests` scanned 4 files with exit 0. `flightctl/` was not a target. Semgrep's built-in default list skips `tests/`.

### Fixes
1. **Rule 6 applies to lane overrides** (inherited from c676b2c; this decided the review's verdict). The oracle and `flightctl/adapters.py` refuse a `dryrun` lane override of any read-only port, on any lane. Negative cases cover:
   - a live lane, overriding every read-only port that a lane may override. The example's features do not require `health_probe`, which the test asserts;
   - a sim lane.
2. **A sim lane runs on fakes in every site profile** (Sol's recommendation). ADAPTERS rule 7 now says so.
   - **Lane-scoped ports:** the lane-critical and mutating ports must be `fake` in the lane's EFFECTIVE selection, inherited site values included.
   - **Shared exceptions** (the site-level `clock`, `inventory_probe`, `peer_identity`, `signer`, `health_probe` and `legacy_observer`): a sim lane may inherit their site selection, but any explicit override it carries must be `fake`.
   - **Where it is enforced:**
     - the schema checks the explicit overrides (a lane-level `if mode sim`);
     - the oracle and `flightctl/adapters.py` check the effective selection.

   Tests cover live and shadow sites: inheriting is refused; all-fake is accepted; each lane-scoped port as `real`, `dryrun` or `record` is refused; each shared port explicitly `real` is refused.
3. **Orphaned topic and code removed** (Sol's recommendation). The notification topic `adapter-refused` and the error code `adapter_refused` are gone from `notification.schema.json` and `common.schema.json`. Nothing in the repo produced or consumed them (grep, measured), and startup stderr is the refusal surface.
4. **Semgrep coverage.**
   - The `pr` job now runs `semgrep scan --error --config p/python flightctl deploy tests`.
   - A repo `.semgrepignore` replaces semgrep's built-in list. It leaves out only `.git/`, `.venv/`, `__pycache__/` and `.pytest_cache/`.
   - The 7 findings are all deliberate, so each carries `# nosemgrep: <rule> -- <reason>`, the repo's existing convention:
     - 5 tmp-fixture permissions in `test_v2_amendment3_boundary.py`: two traversable 0o755 directories, and three deliberately writable modes that prove the loader refuses;
     - 2 test-double hook launches in `tests/roster/shims.py`, which run the hook the test itself names, as an argv list with no shell.
   - `flightctl/` has 0 findings.
   - The pinned line in `tests/integration/test_p6_scaffold.py::test_ci_security_configuration` is updated under migration-map rows for the three touched pre-existing test files. The rows are owned by `contracts-v2`, the lead's contract stream. The frozen `test_v2_round3.py::test_b7_migration_gate_on_this_branchs_real_diff` gates this branch's real diff from 59bdd7f under that name, so a row owned by A0a would not satisfy it. The plan test now admits that owner. Only `test_p6_scaffold.py` and `tests/roster/shims.py` existed at 59bdd7f; the boundary test was added by this branch and needs no row. The A0a brief names the replacement nodes.
   *Measured lesson:* this was first attempted with A0a rows. My local full-suite pass was a proxy: `test_b7` diffs `59bdd7f..HEAD`, and HEAD did not yet contain the edit. lab-ci on the committed clone then failed with 1 test (`test_b7`), which led to this fix.
   - **Proven with the real `tools/migration_gate.py` CLI** on the COMMITTED diff: `--base 59bdd7f --head HEAD --packet contracts-v2` passes ("2 pre-existing files touched, 0 problems"). With a map lacking the two rows it exits 1 with 2 problems. `test_b7` passes on the committed head.
5. **CIYML claim corrected (superseded by revision 3).** At revision 2, GATES, SLICES and AMENDMENT-4 stated the rule and said the race gate's CIYML check was to enforce it once fixed, with the reviewer checking by hand until then.

### Checks (revision 2)
- The differential, the mutation checks, the full suite, the `pr` steps in the image and the scans are reported in the hand-back.

## Revision 3 (wording only; Sol 6.1 APPROVED WITH CHANGES at bcbb878)

The external race gate's CIYML check has been fixed. GATES, SLICES and this document now say that the race gate enforces the ci.yml append. The revision-1 claim and its revision-2 withdrawal are kept above as dated history. The test now checks the new wording.

**Confirmed before writing.** I read the current CIYML section of the lab gate (`roster/gates/flightctl_gate_box.py`, read-only; not part of this repo). I then executed its `suite_block` and `ciyml_verdict` functions in memory against this repo's ci.yml (scratch `ciyml_check.py`; the script itself was not imported). The proper `full` append is ACCEPTED. Each of these is REJECTED:
- the token in the `pr` job (wrong job);
- the token in the `full` job's portability step (wrong step);
- a duplicate token;
- a directory already listed;
- a directory the packet did not add;
- an unrelated token (`--maxfail=1`).
