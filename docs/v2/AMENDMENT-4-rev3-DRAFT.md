# Amendment 4 rev 3 (7 Nov 2026): semgrep replaced by Ruff S

- **Date:** 7 Nov 2026. DRAFT for the lead to fold into the contracts; this branch edits no frozen contract file.
- **Basis:** John's decision of 7 Nov 2026. Every semgrep release up to 1.184.0 pins `mcp==1.29.0` and `opentelemetry-api~=1.37.0`, which carry open advisories that upstream ignores (semgrep issue #12018). John accepts no waiver, no tool moved out of audit scope, and no vulnerable dependency in the lock. He chose Ruff over Bandit.
- **Supersedes:** the semgrep parts of Amendment 4 rev 2, item 4 (the step, `.semgrepignore`, the `nosemgrep` convention).

## Change

1. **Step.** The `pr` job runs `ruff check --select S flightctl deploy tests` in place of `semgrep scan --error --config p/python flightctl deploy tests`. The tool check is `ruff --version`.
2. **Lock.** `deploy/requirements-ci.lock` drops `semgrep` and the 48 packages only it needed, including `mcp` and every `opentelemetry-*` package. It adds `ruff==0.17.4` (no dependencies). The lock was re-resolved with pip's resolver on Python 3.12; all other pins are unchanged.
3. **No exception.** Plain `python -m pip check` is clean and `pip-audit --strict --no-deps -r deploy/requirements-ci.lock` reports no known vulnerability. No waiver or exception script exists.
4. **Config.** `pyproject.toml` has `[tool.ruff.lint.per-file-ignores]` for `tests/**` only: S101, S102, S105, S106, S107, S108, S311, S603, S607 (test-harness patterns: asserts, fake tokens, fixed-seed random, `exec` of the repo's own files, argv-list subprocess launches). `flightctl/` and `deploy/` are exempt from nothing. S103 is not exempt anywhere. The scan scope stays `flightctl`, `deploy` and `tests`.
5. **Suppressions.** `# nosemgrep: <rule> -- <reason>` becomes `# noqa: S### -- <reason>` where Ruff flags the line. Every `noqa` names its rule and a reason; `test_ruff_s_scans_flightctl_deploy_and_tests` enforces the form.
   - The 4 `0o755`/`0o777` tmp-fixture chmods in `test_v2_amendment3_boundary.py` become `noqa: S103`.
   - Ruff S603 is exempt in `tests/**`, so the 2 hook launches in `tests/roster/shims.py` need no suppression. Ruff did flag both (measured before the exemption); their reason stays as a plain comment.
   - Ruff S103 does not flag the `0o664` line or the 4 `0o700` chmods in `deploy/flightctl_release.py`. Those lines keep their reason as a plain comment.
   - New Ruff-only findings in `flightctl/` and `deploy/` (S603 x4, S607, S110, S310, S101 x3) are each suppressed with a reason; none was a real defect except S310 (below).
   - One product change: `HttpTransport.request` now refuses an endpoint that is not `http://` or `https://`, so the S310 reason is true (the endpoint comes from an environment variable and was not scheme-checked).
6. **`.semgrepignore`** is removed; nothing else used it.
7. **Coverage left to CodeQL (inferred, not measured).** GitHub CodeQL default setup (python and actions) stays. Semgrep's `dangerous-subprocess-use-tainted-env-args` has no Ruff rule; CodeQL's taint queries (command injection, uncontrolled data in a command) are the closest cover.

## Frozen files not edited

`AMENDMENT-4.md`, `contracts/v2/README.md`, `SLICES.md`, `CONFORMANCE.tsv` and `migration-map.tsv` still say semgrep as dated history. The lead updates them when folding this note. The touched pre-existing test files may need migration-map rows (see `test_b7`).
