# Security checks and deferred follow-up

The pull-request workflow has one Linux job with a ten-minute timeout and
superseded-run cancellation. It runs contract tests, P6 scaffold integration,
the portability check, ShellCheck, Gitleaks, locked pip-audit, and Semgrep
Python/shell rules. Actions are pinned by commit SHA. Dependabot covers
Actions and pip. The external denylist is required; an absent, empty, or
unreadable denylist fails the job, and matching content is never printed.

The workflow materializes the denylist secret into a mode-600 temporary file;
the secret is not interpreted as a runner pathname. `deploy/ci-tools.lock`
pins the Gitleaks container by digest and ShellCheck archive by version and
SHA-256, and `deploy/provision-ci-tools` verifies both before adding them to
the job. `deploy/requirements-ci.lock` pins the Python tooling set; CI installs
it with `--no-deps` so an unlisted transitive dependency cannot be pulled in.

The bounded weekly workflow runs parser properties against the frozen schema
validator, release-stage inventory admission, denylist parsing, and
RPC-envelope/discovery-shaped JSON. It has its own ten-minute cap and does not
turn missing assembled packages into a passing result.

The other project's security slice is a separate follow-up. It should select
its own locked dependency audit, secret scanner, static-analysis scope, and
parser fuzz budget; this P6 change does not edit that project or claim its
coverage.
