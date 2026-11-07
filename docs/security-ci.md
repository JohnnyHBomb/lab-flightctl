# Security checks and deferred follow-up

The pull-request workflow (`ci.yml`) has two Linux jobs on GitHub-hosted
runners, both with superseded-run cancellation. The `pr` job (ten-minute
timeout) runs contract tests, P6 scaffold integration, the portability check,
ShellCheck, Gitleaks, locked pip-audit, and Ruff's S (flake8-bandit) rules.
The `full` job (twenty-minute timeout) runs every test suite, the portability
check and the plan check. `ci.yml` triggers only on `pull_request` and runs
code from the PR, so it receives no secret. CodeQL default setup also scans
Python and Actions. Actions are pinned by commit SHA. Dependabot covers
Actions and pip.

## External denylist

The external denylist runs in its own workflow, `denylist.yml` (job
`external-denylist`, five-minute timeout), for every pull request, forks
included, and after every push to `main`. It is designed to be a required
status check. An absent, empty, or unreadable denylist fails the job, and
matching content is never printed.

The workflow trusts only the protected base revision. Under
`pull_request_target`, GitHub takes the workflow file from the base branch,
and the default `actions/checkout` checks out the base branch (here with
`persist-credentials: false`, into `base/`). The scanner,
`deploy/check_portability.py`, runs from that checkout with `python3 -I`, so
it imports only the standard library and nothing from the current directory.
The PR head is fetched by its commit id into a separate bare repository. The
scanner reads that commit's tree as git objects (`ls-tree`, `cat-file
--batch`) through its `--data-rev` mode. Nothing from the PR is checked out,
installed, imported, or executed, and that mode skips the in-tree portability
check because that check runs the scanned tree's own code. PR text such as
the title, body, or branch name is never expanded into a script; only the
commit id reaches the scripts, through the environment, and it must be 40 hex
digits. The workflow has `contents: read` only, no cache, no artifact, and no
other action. On a push to `main` the same scanner scans the pushed commit.
In `--data-rev` mode the scanner reads a symlink's target text, not the file it
points to. Submodule entries (gitlinks) are skipped, but `.gitmodules` is
scanned; neither mode ever scanned submodule content. The secret is unset
once it is written to the 0600 temp file. A `concurrency` group keeps one
run per PR and cancels the older run on a new push.

`FLIGHTCTL_DENYLIST` is a secret of the `denylist` environment, whose
deployment branches are limited to `main`, and the job names that environment.
A workflow pushed on any other branch cannot read it.

The denylist workflow materializes the secret into a mode-600 temporary file;
the secret is not interpreted as a runner pathname. `deploy/ci-tools.lock`
pins the Gitleaks container by digest and ShellCheck archive by version and
SHA-256, and `deploy/provision-ci-tools` verifies both before adding them to
the job. `deploy/requirements-ci.lock` pins the Python tooling set; CI installs
it with `--no-deps` so an unlisted transitive dependency cannot be pulled in.
The `ci.yml` and parser-fuzz workflows require `python -m pip check` after installation;
missing or incompatible dependencies fail the job. The offline scaffold
rehearsal does not verify a clean Python 3.12 tool installation or security
scanner execution; those results must be obtained on the CI runner.

The bounded weekly workflow runs parser properties against the frozen schema
validator, release-stage inventory admission, denylist parsing, and
RPC-envelope/discovery schema validation. It has its own ten-minute cap and does not
turn missing assembled packages into a passing result.

The other project's security slice is a separate follow-up. It should select
its own locked dependency audit, secret scanner, static-analysis scope, and
parser fuzz budget; this P6 change does not edit that project or claim its
coverage.
