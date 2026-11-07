# Amendment 4 rev 4 (7 Nov 2026): external denylist moves to a trusted workflow

- **Date:** 7 Nov 2026. DRAFT for the lead to fold into the contracts; this branch edits no frozen contract file.
- **Basis:** John's approval of 7 Nov 2026 to fix Daybreak findings CI-01 (fork PRs skipped the external denylist, and
  no trusted check scanned their content) and CI-02 (same-repository PRs ran the PR's own `deploy/check_portability.py`
  with the denylist secret present). CV-16 (security-CI documentation out of date) is fixed in `docs/security-ci.md`.
- **Supersedes:** the "then the external denylist" part of A0a's OBLIGATIONS in `SLICES.md`, and the external-denylist
  step of the `pr` job.

## Change

1. **ci.yml.** The `pr` and `full` jobs lose their external-denylist steps. `ci.yml` runs PR code and now uses no
   secret. `full` runs every suite, then portability, then the plan check. `test_ci_full_job_lists_every_suite` checks
   that order and that `ci.yml` names no secret.
2. **denylist.yml (new).** Job `external-denylist`, GitHub-hosted, `contents: read`, triggered by
   `pull_request_target` (every PR, forks included) and by `push` to `main` (post-merge scan). It checks out the base
   revision only, with `persist-credentials: false`. It fetches the PR head by commit id into a separate bare
   repository and runs the base scanner with `python3 -I` in `--data-rev` mode. The secret reaches only that step.
   `test_trusted_denylist_workflow_shape` checks the shape.
3. **Scanner.** `deploy/check_portability.py --data-rev COMMIT ROOT` reads the tree of COMMIT from the git repository
   at ROOT with `git ls-tree` and `git cat-file --batch`. It checks out nothing and runs no code from the tree, so it
   skips the in-tree portability check, which runs the tree's own `tools/check_portability.py`. It accepts only a
   full commit id. `test_external_denylist_data_rev` covers it, including a planted `tools/check_portability.py`
   that must not run.
4. **Required check.** `external-denylist` is meant to be a required status check on `main`. That is a branch
   protection setting for John, not part of this change.

## Frozen files not edited

`SLICES.md` (A0a OBLIGATIONS and the rev 2 note) and `AMENDMENT-4.md` still put the external denylist in the `full`
job, as dated history. The lead updates them when folding this note. The touched pre-existing test files may need
migration-map rows (see `test_b7`).
