# Amendment 3: C9 as a self-contained build on pre-built R1 pathways

- **Date:** 2 Oct 2026.
- **Basis:** the owner's decision on C9.
- **Form:** a new commit on top of `contracts-v2`, after Amendment 2 and the CI commits. It needs review before use, as `FROZEN.md` requires.

## The owner's decision (verbatim)

> "As long as all the wiring necessary for C9 is done, so that C9 can be an entirely self-contained build that merely uses pre-built pathways this is acceptable. reconstructing the rest to suit C9 is not."

## Reproduced first (measured)

Before this amendment:
- The plan had C9 in milestone C, ahead of C-ASM: inside R1.
- C9 delivered the `session_gateway` port, so C-ASM depended on it, and the conformance registry owed `session_gateway` to C9.
- No test or tool constrained which files C9 may touch. A grep for any touch-set check found none.

## What changed

1. **New R1 packet C9w, "session pathways for C9".** Milestone C, before C-ASM; depends on C7h, A7b and C1; delivers the `session_gateway` port. It wires every pathway C9 will use, each REFUSING while the friend flags are off:
   - the SessionGateway fake twin, and a real twin that crosses the real wire (sudo helper) and comes back refused;
   - the session RPC ops `session-open` and `session-close`, with token scope `session` and CLI `session open/close`, refusing `unavailable` and naming the feature (the web UI has no session surface);
   - `authority_admits_friend_work` and the global, per-host and conjunction flag checks;
   - disable ordering, which runs even with the flags off, and the consistency check;
   - the helper's registered dispatch points (session-open, session-close, friend unit-start, claim-release, claim-reconcile);
   - the `flightctl-claim-clear` and `flightctl-session-probe` programs as refusing stubs;
   - the real cleanup bodies (session-close, claim-release, claim-reconcile, the probe sweep);
   - the claim store interface, with read, reconcile and release;
   - the C9 SEAMS: `flightctl/c9_seams.py` and `helper/c9_seams.py`. These declare `session_open_body`, `friend_unit_start_body`, `claim_create_body`, `claim_clear_body`, `session_probe_body` and `binding_measure`, each with a refusing default. Implementations are discovered from the fixed packages `flightctl.c9` and `helper.c9`;
   - conformance auto-import of `tests/conformance/impl_*.py`.

   **R1 acceptance** (with the flags off): the refusing-twin session_gateway conformance; `test_authority_refuses_session_open_when_flag_off`; `test_authority_refuses_friend_session_on_ineligible_host`; `test_helper_refuses_without_both_flags_and_live_binding`; `test_disable_ordering_fail_closed`; `test_consistency_check_passes_before_activation`; and the new `test_c9_seams_refuse_by_default`.
2. **C9 moves to milestone D** (session enablement, after C-ASM). It depends on C9w and C-ASM, and its Ports column is now `-`. C9 BUILDS ONLY:
   - account creation;
   - claim creation and persistence;
   - the claim-clear and session-probe program bodies;
   - the sshd runbook tooling and binding measurement;
   - their on-lab proofs;
   - the pre-enablement security review.
3. **The C7h tests are split.**
   - **Moved to C9** (friend-working behaviour): `test_parent_claim_lifecycle`, `test_claim_rollback_unlinks_only_its_own_inode`, `test_quarantine_and_start_are_serialised`, `test_claim_clear_requires_operator_sudo_path`, `test_claim_clear_prompts_every_time`, `test_session_probe_bounds` and `test_probe_sweep_requires_real_monotonic_clock`.
   - **Stay in R1** (refusal and cleanup): `test_friend_flag_off_refuses_creation_allows_cleanup` and `test_session_close_order_key_then_terminate_then_slice_stop`.
   - **New R1 test:** `test_friend_subcommands_registered_and_refuse_while_off`.

   The Amendment 2 blast-radius note counted 9 friend-only C7h tests. Two of those are refusal or cleanup paths, which John's condition keeps in R1, so 7 move.
4. **Updated alongside.** C-ASM now depends on `C1-C12, C9w`. Its `session_gateway` prerequisite is C9w's refusing real twin. The crosswalk lists C9w before C-ASM and C9 after it. Registry `OWED_BY['session_gateway']` is now `C9w`. GATES names C9w a security packet. README item 75, OPEN-QUESTIONS Q1 and the `friend_sessions` description record the decision, and BLAST-RADIUS marks the deferral as decided.

## C9's touch set (enforced)

*Revision 2 replaces this section's touch set, pathway count and check method; see "Revision 2" below.*

The C9 brief's TOUCH SET block is checked by `tests/contracts_v2/test_v2_amendment3.py`, and by `tools/touch_set_gate.py` on C9's own diff.

**New paths** (they must not exist yet, and no R1 brief may name them):
- `flightctl/c9/`
- `helper/c9/`
- `tools/c9_runbook/`
- `tests/c9/`
- `tests/conformance/impl_session_gateway_c9.py`

**Extension points** (the only R1 files C9 may modify, all documentation):
- `docs/v2/CONFORMANCE.tsv`: the status and note of rows homed at C9.
- `docs/v2/migration-map.tsv`: append rows owned by C9.
- `docs/v2/SLICES.md`: the C9 brief only.

**Pathways.** C9's R1 PATHWAYS USED table lists 15 capabilities, each resolved to C9w or C7h. The test checks that the deliverer is an R1 packet and that its brief names the pathway.

## R1 touch points that could not be eliminated

*Revision 2: the operator sudoers rules are now installed in R1, and the SLICES.md scope is now checked line by line; see below.*

**None in R1 code.** What enablement changes is site data and host configuration, not repo files: the friend flags, `c9_proof`, helper.json, the operator's claim-clear sudoers rule and the sshd drop-in. These are owner actions done through the runbook.

**Bounded (inferred):**
- The SLICES.md extension point is free text. The gate cannot tell an edit to the C9 brief from an edit elsewhere in SLICES.md; a reviewer must confirm C9 edited only its own brief.
- The seams are a contract that C9w must implement. Until C9w is built, "C9 needs no R1 code edit" is a property of the plan, not yet of code.

## Checks

- Mutation checks (scratch `vac_amd3.py`): 6 of 6 caught.
  - extension whitelist dropped;
  - "new path must not exist" dropped. The first attempt was not caught, because an overlapping check also refuses `flightctl/`. I added a case for an existing path that no R1 brief names (`ondemand/`).
  - R1-deliverer check dropped;
  - named-in-deliverer check dropped;
  - gate accepting R1 files;
  - gate accepting the deletion of a new path.
- Full suite, portability, denylist, whole-history and gitleaks results are in the hand-back.

## Revision 2 (Sol 6.1 review of Amendment 3: REQUEST CHANGES, five P1 and one P2)

Amended in place on the same base. Each finding was reproduced first (scratch `repro_amd3r2.py`, measured on 03cc95b):
- **P1-1:** with the five permitted C9 additions present, `test_a3_c9_touch_set_is_declared_and_closed` failed with five "already exists" errors.
- **P1-2:** the session conformance body failed at `open()["ok"] is True` against a refusing stand-in. `migration_gate` refused a C9w edit of `test_work_support.py`. ADAPTERS named C9.
- **P1-3:** `operator_sudo_audit` on a listing without the claim-clear rule returned "no sudo route" and "timestamp_timeout ... default (5)".
- **P1-4:** `session_open_args` had only `lease_id`, `key_fingerprint` and `idle_timeout_s`. There was no key op, no executor `session` kind, and nothing about session slices in attribution.
- **P1-5:** C9w had no manifest, stale-file or pre-import rule.
- **P2:** all five of Sol's mutations were accepted.

### Fixes

1. **Touch set checked at the R1 base (P1-1).**
   - The touch set and the pathway table are pinned in `tests/contracts_v2/test_v2_amendment3.py`. That file is an R1 file outside C9's touch set, so C9 cannot widen either.
   - A new path may exist only if the R1 release tag `v2-r1` exists and holds no file under it. C-ASM creates the tag. Before the tag exists, any file under a new path is refused.
   - `tools/touch_set_gate.py` reads the touch set from the base commit.
   - C9's conformance success file is `tests/conformance/test_session_gateway_c9.py`. It replaces `impl_session_gateway_c9.py`. A second `real` registration would silently replace C9w's twin, because `registry.register` overwrites; and C9 changes host-side bodies, not the twin.
2. **Session conformance split (P1-2).**
   - `test_work_support.py` now holds the R1 cases:
     - `test_session_open_refused_while_friend_flags_off`: a typed `unavailable` refusal with no host-state change;
     - `test_session_close_on_seeded_session_proves_cleanup`.
   - The success proof moves to C9's new file. A `friend_flags("off"|"on")` applicability marker (`FLIGHTCTL_CONFORMANCE_FRIEND_FLAGS`, default off) records the other state as "n/a". The case asserts the read-back flag state.
   - The conformance conftest now imports `impl_*.py`.
   - ADAPTERS names C9w (the refusing real twin) plus C9 (the bodies).
   - **Deviation from the lead's brief:** no migration-map row was added for C9w. The R1 cases are written into the contract now, so C9w adds `impl_session_gateway.py` and edits no pre-existing test file. The real `migration_gate` proves this on the simulated C9w diff ("0 pre-existing files touched").
3. **Operator rules installed in R1 (P1-3), the lead's preferred route.**
   - C7h installs `flightctl-claim-clear` and `flightctl-session-probe` as refusing stubs, together with the operator's two authenticated sudoers rules. This is safe: the rules give the operator a root program that only refuses.
   - `test_install_audit` and `test_claim_clear_prompts_every_time` stay whole in C7h. sol6r8-B5 and the sol6r9-new rows keep their C7h home and completion route. C9 needs no sudoers change.
4. **Session boundary (P1-4), pre-wired in C9w.**
   - **Key enrolment.** The ops are `session-key-enrol` (refused while off), `session-key-list` and `session-key-revoke`, with scope `session` and CLI `session key add/list/revoke`.
     - Schemas: `session.schema.json` `public_key_text` and `enrolled_key`.
     - Oracles: `parse_session_key` (bare keys only: no options, no line breaks, the blob type must match) and `resolve_session_key` (the caller's own unrevoked key only).
   - **Executor wire.** The executor kind `session` covers register, close and reconcile.
     - Register creates a registry entry only from an ok SessionGateway.open. While off it is refused: definite, `unavailable`, nothing recorded.
     - The executor closes sessions itself on local expiry and on controller loss. The authority sends reconcile on every (re)connection and A8 pass.
     - Oracle: `executor_session_closes`.
   - **Attribution.** `session_attribution` attributes a process only by its cgroup inside the slice of an OPEN registry entry for this lane, never by uid. gpu-probe's attribution text says so.
   - **Named R1 acceptance tests through unchanged R1 callers:** `test_session_pathway_end_to_end_through_r1_callers` (it includes the unique parent admission), plus the enrolment, executor and attribution tests.
5. **Seam loading (P1-5).**
   - An R1 install never contains C9's packages (`c9_absent_audit`), and activate or rollback removes files absent from the manifest (`release_stale_removals`).
   - `c9_seam_load` behaves as follows:
     - With the flags off, the package is neither read nor imported.
     - With the flags on, every file and directory under the install prefix is checked against the release manifest BEFORE import. The interpreter runs isolated, so this is the whole non-stdlib import tree. Each entry must be a non-symlink, root-owned and not group- or other-writable, and its sha256 must match.
     - Any failure, an import error or a missing hook leaves the refusing defaults. Cleanup never uses the loader.
6. **Guards (P2).**
   - The pinned touch set and the canonical 20-row capability set are checked for count, identity and distinctness.
   - The gate checks the extension points line by line:
     - CONFORMANCE: only the status and note columns of rows homed at C9 alone may change.
     - migration-map: rows may only be appended, and only C9's.
     - SLICES: edits are allowed only inside the C9 brief, and never to its pinned blocks.
   - The cleanup bodies are declared real, each with a seeded R1 test. The conformance seeded case fails against a stub cleanup body (`test_a3_session_conformance_against_stand_ins`).
   - All five of Sol's mutations are now caught.
7. **Moved back to R1.**
   - C9w: `test_probe_sweep_requires_real_monotonic_clock`.
   - C7h: `test_claim_clear_prompts_every_time`, `test_seeded_claim_release_and_reconcile`, `test_quarantine_persists_until_operator_clear` and `test_seeded_session_close_proof`.
   - Five friend-working tests stay in C9.

### Still bounded (stated, not checked)
- Whether C9's prose inside its own brief, its CONFORMANCE notes and its migration reasons is accurate is review, not a check.
- Whether C9w's code consumes the registry, the loader and the key resolution is proven by C9w's named acceptance tests when C9w is built. Until then it is a property of the plan.
- The real-twin seeded conformance needs an owner-run root seeding step and an owner-created test friend account on the R1 test host.

### Checks (revision 2)
- Simulated C9w and a legal C9 build in a scratch git repo (`c9_sim.py`, measured):
  - **C9w** (adds `impl_session_gateway.py`): the real `migration_gate` passes with 0 pre-existing files touched. The R1 session cases pass with the flags off and are n/a with them on.
  - **Legal C9 on top of the tag `v2-r1`:**
    - additions: the five touch-set paths;
    - extension edits: a C9-homed CONFORMANCE status, an appended C9 migration row and an edit inside the C9 brief.
  - **Results for the legal C9 build:**
    - `touch_set_gate` passes;
    - `migration_gate` passes;
    - whole contracts suite: 579 passed, 1 skipped, 2 xfailed;
    - whole `tests/`: 1069 passed, 39 skipped, 2 xfailed;
    - C9's success proof passes with the flags on and is n/a with them off.
  - **Negative controls, each refused:**
    - C9 editing `test_work_support.py`: both gates refuse;
    - C9 widening its own touch set: the gate and the suite refuse;
    - `flightctl/c9` present at the tag: the suite refuses;
    - `flightctl/c9` present with no tag: the suite refuses.
  - **The first simulation run found two R1 gaps, fixed here:**
    - the conformance-binding receiver map had no entry for C9's new file;
    - a gate test depended on C9-brief prose that C9 may legally edit.
- Mutation checks (`vac_amd3r2.py`): 40 of 43 caught. The 3 misses are equivalent mutants, documented:
  - the R1-deliverer check is subsumed by the pin;
  - a symlink is mode 0777 on Linux, so the writable check refuses it;
  - the stale-package early return is subsumed by the extra-file check.

## Revision 3 (Sol 6.1 review of revision 2: REQUEST CHANGES on seam loading, two P1s, plus one mutant and four smaller gaps)

Findings 1-4 and 6 were closed by that review. This revision is amended in place on the same base. Each item was reproduced first (scratch `repro_amd3r3.py`, measured on 6fb0590):
- The loader did not load the probe body with the flags off, which is the only state the probe ADD policy admits.
- `/usr/bin/python3 -I -B` reported `no_site=0`, with `site` loaded and site-packages on `sys.path` (`yaml`, `packaging`, `gi` and `dbus` resolved). `-I -S -B` reported `no_site=1`, with `sys.path` holding only the stdlib zip, the stdlib and lib-dynload.
- A world-writable prefix still loaded.
- SystemExit at import escaped the loader.
- An Ed25519 key with the wrong nested length was accepted, and so was an ECDSA blob holding only its type.
- A revoked key resolved again after re-enrolment.
- A `closed` registry entry with a null reason and a null proof validated.

### Fixes
1. **Loader states (P1).**
   - The loader state is `off`, `window` or `on` (`c9_loader_state`). A mixed flag state counts as `off`.
   - The ENABLEMENT WINDOW is per host and opened by the operator through the existing session-probe operator route (`--window-open --ttl`, at most 3600 s). It is refused while any flag is on, and an open window cannot be extended.
   - The window is valid only on its own host, in the same boot and before both its wall and monotonic deadlines. It closes on success, failure, expiry or the rollback sweep, and every call is audited.
   - The body table, with each body's states:
     - `session_probe_body`: window only;
     - `binding_measure`: window and on;
     - `session_open_body`, `friend_unit_start_body`, `claim_create_body`, `claim_clear_body`: on only.
   - Why `claim_clear_body` is on only: no runbook step needs it, and lifting a quarantine while friend creation is refused changes nothing.
   - Every cell is tested. The table is checked against runbook steps 5, 7 and 8, and against the probe ADD policy: the probe works in exactly one state, the window.
   - The runbook now opens the window before the probe, measures with `--measure` (the helper's own `binding_measure`), and closes the window before any flag is written. All of this is pre-wired in C9w.
   - **Deviation:** no new program or sudoers rule. The window rides on the operator's existing session-probe route, which is smaller and already audited in R1.
2. **Import boundary (P1).**
   - Trusted startup is `python3 -I -S -B`, with `sys.path` set to the stdlib plus the verified prefix only.
   - `C9ImportGuard` sits first on `sys.meta_path` and is authoritative: it resolves every import itself and refuses anything that is neither builtin or frozen, nor stdlib, nor inside the prefix. This holds even when a body puts another directory on `sys.path`.
   - Dependencies must be vendored inside the manifest. Cache files are refused.
   - The prefix and every ancestor are inspected. Any filesystem error refuses.
   - ANY BaseException at import refuses, KeyboardInterrupt included: the helper is non-interactive, and refusing is the outcome either way, so the loader never raises.
   - Cached `sys.modules` entries are purged before verification.
   - The reference is now the stdlib-only `tests/contracts_v2/c9_loader.py`. Its boundary cases run under the real `-I -S -B` interpreter in a subprocess.
3. **The cached-module mutant is now caught.** Sol was right that it is not an equivalent mutant. The new negative control is `test_seam_loader_cached_module_without_a_package_refuses`, plus a real-interpreter cached case.
4. **Smaller items.**
   - `parse_session_key` checks the complete structure of each supported type:
     - Ed25519: exactly one 32-byte key;
     - ECDSA: curve `nistp256` and an uncompressed point on the curve;
     - sk- types: an `ssh:` application;
     - every type: the blob is consumed exactly.
   - **Revocation:** a revoked fingerprint is never re-enrolled, by anyone, until the operator purges its record (`session-key-revoke` with `purge`). A revocation usually means the key was lost or compromised, and a friend-side re-enrolment would silently undo it.
   - **Executor registry:** `closed` requires a reason and an all-true proof. `closing` and `failed` require a reason. `open` carries neither a reason nor a proof.
   - **Frozen literals:** C9 must preserve the frozen literals inside its brief, not only their meaning. The C9 brief now says so.

### Checks (revision 3)
- **Mutation checks** (`vac_amd3r3.py`, source-text mutants on a scratch copy, judged by the real test files including the subprocess interpreter tests): 42 of 45 caught. The 3 misses are documented equivalents:
  - a prefix outside the trust root, for a non-root runner: the walk reaches the root-owned `/` and refuses;
  - an inherited `sys.path`: the guard still refuses;
  - the in-process bytecode flag: `-B` already forbids writing bytecode.
- The first run caught 39 of 45. The 4 real misses were closed with new tests:
  - a body that edits `sys.path` itself;
  - a non-callable hook;
  - an ECDSA point with a wrong prefix byte;
  - an ECDSA point one byte too long.
  The same run also found that the loader's ancestor walk could loop at `/`; that is fixed.
- **C9 simulation rerun** (`c9_sim.py`, measured): legal C9 on `v2-r1` passes `touch_set_gate` and `migration_gate`; whole contracts suite 655 passed, 1 skipped, 2 xfailed; whole `tests/` 1145 passed, 39 skipped, 2 xfailed. All four negative controls are still refused.
- **Worktree full suite:** 1143 passed, 39 skipped, 2 xfailed. Portability, denylist, history and gitleaks results are in the hand-back.

## Revision 4 (Sol 6.1 review of revision 3: REQUEST CHANGES, two P1s)

All revision-3 items were verified by that review (all 18 table cells, key structures, revocation, registry, the cached-module control). This revision is amended in place on the same base. Both P1s were reproduced first (scratch `repro_amd3r4.py`, measured on 3f730be, the second under `/usr/bin/python3 -I -S -B`):
- **Window:** before the step-7 sweep the state was `window` and `binding_measure` was allowed. After `--sweep --all` closed the window, step 8(a) saw `off` and `binding_measure` was refused.
- **Guard:** with the guard first and the nested `/usr/lib/python3.14/site-packages` appended, `packaging` and `yaml` were imported.
- **Loader:** with an importer that appends that directory, `c9_seam_load` loaded `packaging` from it (`loaded=True`).
- **Alias:** an alias prefix loaded, and imports used the alias spelling.
- **Cached outside module:** an outside module imported before the guard did not stop the load.

**Measured on this interpreter (sysconfig):** `stdlib` = `platstdlib` = `/usr/lib/python3.14`; `purelib` = `platlib` = `/usr/lib/python3.14/site-packages`, which is nested. The strict-startup `sys.path` is the stdlib zip, the stdlib directory and lib-dynload. In a venv, sysconfig `platstdlib` points into the venv, so the strict-startup `sys.path` is the reliable stdlib source.

### Fixes
1. **Window sequence.**
   - Smallest sound fix: the operator's step-7 `--sweep --all` removes the probe key and leaves a valid window open (it also removes a window record that is already invalid, which refuses measurement anyway). Only two things close the window:
     - the rollback unit's `--sweep --all --close-window`;
     - the per-minute `--sweep --expired`, once the window is no longer valid (`c9_window_sweep`).
   - Step 8(a) reopens a window that is no longer valid before `--measure`. On failure it closes with `--result failure`. It closes the window before any flag is written.
   - The presence-only test is replaced:
     - a command map ties every `flightctl-session-probe` command in steps 5, 7 and 8 to an executed operation;
     - a SUCCESS sequence carries the window state from opening through measurement to closure, and then to the flags;
     - six FAILURE paths each end with the window closed, the flags off and the probe key gone: login fails; operator gone; window expires (abandoned, and reopened); host reboots; measurement fails.
   - All of this uses the actual window, state, body, probe and sweep oracles.
2. **Exact stdlib trust.**
   - `stdlib_trusted` admits a file only when it is under a strict-startup stdlib entry, under neither `purelib` nor `platlib`, and has no `site-packages` or `dist-packages` directory in between.
   - The prefix is canonicalised once and bound for both verification and import, so repointing an alias cannot substitute a tree.
   - `preloaded_outside_modules` refuses the load if any module already in `sys.modules` comes from outside exact stdlib and the prefix. The loader itself lives inside the prefix, as in production.
   - New real-interpreter controls: both of Sol's nested-site-packages cases, the alias and alias-swap cases, a cached outside module, and `sys.path` replaced exactly.
   - **Narrowed claim:** the inherited-`sys.path` mutant is NOT claimed equivalent any more. It is caught by the `path_exact` case.

### Checks (revision 4)
- **Mutation checks** (`vac_amd3r4.py`, source-text mutants judged by the real test files): 50 of 53 caught, including every rev-4 mutant:
  - the rev-3 descendant-trust rule;
  - the nested-directory and `purelib`/`platlib` exclusions;
  - the pre-guard module check;
  - alias import;
  - the plain sweep closing the window;
  - an expired-window sweep.
- The 3 misses are equivalents, with the qualifications Sol gave:
  - **Trust-root containment check:** equivalent in the current oracle, because the ancestor walk refuses when it cannot reach the trust root.
  - **Guard prefix not canonicalised:** equivalent, because the loader passes the canonical prefix and `_under` canonicalises both sides.
  - **In-process bytecode flag:** equivalent under the tested `-B` startup precondition.
- **C9 simulation rerun** (measured): legal C9 on `v2-r1` passes both gates; whole contracts suite 670 passed, 1 skipped, 2 xfailed; whole `tests/` 1160 passed, 39 skipped, 2 xfailed. All four negative controls are still refused.
- **Worktree full suite:** 1158 passed, 39 skipped, 2 xfailed. The nested-site-packages controls ran; they did not skip on this host. Scans are in the hand-back.

## Revision 4 review (Sol 6.1, amd3r4): APPROVE WITH CHANGES

Both revision-3 P1s are closed, and the owner's condition is met at the contract level: no remaining C9 need forces an R1 edit outside the touch set. The two wording corrections Sol requested are applied:
- `preloaded_outside_modules`: file-less modules carry no provenance; accepting builtin and frozen entries rests on the trusted `-I -S -B` startup.
- The step-7 `--sweep --all` removes the probe key and leaves a VALID window open; it also removes an already-invalid window record, which refuses measurement anyway.

Carried, mandatory before R1 freezes: C9w's acceptance through unchanged callers (real-wire refusal while off, seeded cleanup, body dispatch), the installed-interpreter checks on each supported deployment, and the on-lab root-ownership and ancestor-chain proofs.
