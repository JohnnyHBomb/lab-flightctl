# Legacy compatibility: the lanes.sh shim

Goal: every existing caller of the legacy `lanes.sh` keeps working **unmodified** through cut-over:
token capture with a 16-hex check, `grep ' free'` on status, `grep "token ${TOK:0:8}"`, rolling
renew loops, bounded `wait`, and `LANE_OWNER` labels. The shim is a small client installed at the legacy
path. The legacy script itself is renamed and kept for rollback and for lanes still in `legacy` mode.
Vectors: `tests/contracts_v2/vectors/legacy-shim.json`. Configuration: `legacy-compat.schema.json#/$defs/shim_config`.

## Per-lane mode (the cut-over switch at the client side)

| Mode | Behaviour | When |
| --- | --- | --- |
| `legacy` | Exec the renamed legacy script with the same argv and environment. Nothing else happens. | Before shadow; after a rollback. |
| `tee` | Run the legacy script, which stays **authoritative**: its stdout and exit code are what the caller sees. The shim also sends the equivalent v2 RPC with `admission.shadow_of`, bounded by `mirror_timeout_s`. A mirror failure never changes the caller's result; it is logged as an `adapter-error` divergence. | Shadow period. |
| `v2` | Talk only to the v2 authority. The legacy writer for that lane is disabled (renamed script plus a holding lease, per CUTOVER-RUNBOOK). | Live. |

## Command semantics in `v2` mode

| Legacy command | v2 RPC | stdout (byte-compatible with legacy) | exit |
| --- | --- | --- | --- |
| `acquire <lane> "<purpose>" [ttl-min]` | `acquire {purpose, class: batch, est_s = ttl, ttl_s = ttl, max_s: null, token_format: hex16, client_label: $LANE_OWNER, holder_binding: ttl-only}` | exactly the 16-hex token and a newline | 0 |
| ...busy (409 busy or queue head elsewhere) | | `BUSY <lane>: <label or principal>@<host> '<purpose>' until HH:MM` (holder taken from `error.details.holder`; host = the holder's client host if known, else the lane host) | 1 |
| ...unknown lane | | `unknown lane X (known: a b c)` (from snapshot) | 2 |
| ...202 waking | | nothing yet; the shim retries the same request id every `retry_after_s` until `wait_until`, then prints the busy line with `waking <host>` | 0 or 1 |
| ...403 quota_exceeded or denied | | `REFUSED: <message naming the limit>` on stderr | 2 |
| ...503 / transport failure | | `UNAVAILABLE: <cause message>` on stderr | 3 |
| `release <lane> <token>` | `release {token}` | `released <lane>` | 0 |
| ...wrong token | | `REFUSED: <lane> held by a different token` | 1 |
| ...lane already free, or the token's lease closed | | `released <lane>` (legacy released a free lane silently; v2 replays the closed state) | 0 |
| ...202 release-confirming | | the shim polls status until closed or quarantined, bounded by 120 s; then `released <lane>`, or `REFUSED: <lane> quarantined: <cause>` with exit 1 | 0 or 1 |
| `renew <lane> <token> [ttl-min]` | `renew {token, ttl_s}` (rolling; succeeds inside the ceiling) | `renewed <lane> to HH:MM` (local time of the new `expires_at`) | 0 |
| ...ceiling reached or wrong token | | `REFUSED: <lane> not held by that token` or `REFUSED: <lane> at approved maximum HH:MM` | 1 |
| `wait <lane> "<purpose>" [ttl-min] [max-wait-min]` | `queue add` then `acquire {queue_id}` with refresh every 60 s and 202-waking retries; default max wait **120 min** (legacy), not the new CLI's 10 | the token, as for acquire | 0 |
| ...timeout | `queue remove` | `TIMEOUT waiting for <lane>: BUSY ...` | 1 |
| `status [--all]` and bare `status` | `snapshot` | one line per lane: `<lane:16> free`, or `<lane:16> HELD by <label or principal>@<host> for '<purpose>' (token <hint>, N min left)`, or `<lane>  (unreachable)` / `<lane>  (asleep)` | 0 |
| `status <lane>` | `status` | the same single line | 0 |

Notes:
- **Token.** The authority issues the hex16 token itself, as the lease token, when `token_format=hex16`
  (decision D-shim-1). There is no alias table: the 64-bit strength matches legacy, and holder operations
  also require the authenticated principal. `token <hint>` (first 8 hex) is shown only to the lease's
  principal and to operators. Others see `token ********`, which no legacy caller greps for: they grep
  for their own token.
- **LANE_OWNER** becomes `client_label`: display only, never identity. Identity is the tailnet peer, plus
  a bearer token if the caller's environment provides `FLIGHTCTL_TOKEN`. All agents on one workstation
  share one principal unless separately enrolled (plan r4).
- **The polling callers** (`grep ' free'` loops) keep working, but they acquire raw. The authority
  refuses a raw acquire while a FIFO waiter exists (QUEUE.txt), so those callers can starve. The packet
  that installs the shim also changes them to `wait` (lab-side edit, listed in CUTOVER-RUNBOOK).
- **Exit codes** match legacy. New failure classes map onto them: busy, timeout and refused are 1;
  unknown lane, invalid and denied are 2; unavailable is 3. Legacy fell through with ssh's 255. The shim
  returns 3 instead, documented as an intentional change: the caller now sees "unavailable", not a random
  ssh code.
- **Fractional TTLs.** Legacy accepted `float()` minutes. The shim rounds up to whole seconds. The new
  CLI keeps its integer rule.

## `tee` mode mirror mapping (shadow evidence)

Each legacy call produces one mirror RPC with `admission.shadow_of = {legacy_op, legacy_exit,
legacy_stdout_class, legacy_token_hint}`. The authority evaluates the request exactly as in live mode
against its shadow state, executes nothing, and records:

- `shadow-decision` when it would have decided the same as legacy (for example both grant, or both busy);
- `shadow-divergence` with a class when it would not:
  - `grant-mismatch`: legacy granted and v2 would refuse (for example a FIFO waiter exists), or the reverse;
  - `holder-mismatch`: v2's shadow lease belongs to a different label or principal;
  - `release-mismatch`: legacy released and v2 would quarantine because the occupancy probe shows a tenant;
  - `occupancy-mismatch`: raised by the watcher, not the shim. Legacy says free while the GPU shows a tenant;
  - `adapter-error`: the mirror failed, timed out or could not be parsed.

Known, explained divergences (legacy has no FIFO, legacy treats unreadable as free) carry `explained_by`.
`policy.shadow.divergence_classes_blocking` decides which unexplained classes reset the clean-days
counter. A lane flips to `v2` only when all of these hold:

1. `policy.shadow.clean_days_required` consecutive days (3) with zero unexplained blocking divergences;
2. **coverage** (Sol 6 N4): the window holds at least `policy.shadow.min_path_counts` mirrored decisions
   for each path (acquire, renew, release, wait, busy, status). Paths that do not occur naturally are
   exercised by the gauge through legacy `lanes.sh` with a dummy holder, while legacy stays authoritative;
3. **zero mirror errors** (`adapter-error`) in the window. `mirror_errors_block` is fixed at true, because a
   shadow that cannot see a call has not checked it;
4. the live acceptance tests listed for that lane's assembly pass.

The divergence report prints the counts, the errors and the clean-days counter. The flip is an operator
edit of the shim config and `adapters.json`, recorded as an event.
