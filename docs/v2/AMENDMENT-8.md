# Amendment 8: the A6 linger probe reads loginctl correctly and counts sessions from snapshots (fix owned by A6b)

- **Date:** 5 Oct 2026. **Approved:** pending the owner. It needs review before use, as `FROZEN.md` requires.
- **Basis:** A6's G3 run on main `12396e0` (the A6 merge), on lab hosts running systemd 261 (measured). Revision 2: Fable
  cold review (REQUEST CHANGES: per-id class reads race the probe's own closed sessions; `background` must count; offline
  captures need a seam; exact report fields), all findings taken.

## Reproduced first (measured)
1. **The probe's property read returns nothing.** `loginctl show-user <uid> --property=Linger,Sessions` (the argv A6's
   packet fixed) prints no output and exits 0 on systemd 261. loginctl does not split `--property=` on commas (unlike
   systemctl), so `Linger,Sessions` is one property name that matches nothing. `-p Linger -p Sessions` prints both. A6's
   `test_unit_and_inhibitor_survive_logout` therefore reported `"linger": null, "other_sessions": -1` and failed as
   inconclusive on an ssh target where both units survived.
2. **The user manager is a session.** Since systemd 256 the per-user service manager is listed as its own session with
   class `manager` (pam_systemd(8)). `other_sessions = len(Sessions) - 1` counts it, so it can never reach 0. On systemd
   before 256 no manager entry exists and the rule below is unchanged; a host whose loginctl lacks `--json=` exits non-zero
   and the probe is `unreadable`, which is the intended answer. Every lab host measured for A6's G3 runs 261.
3. **Per-id reads race the probe's own sessions.** Each `SshCommandRunner` call is its own ssh login session (one `ssh`
   process per call, no multiplexing). A session id read in one call has usually ended by the next call, and
   `loginctl show-session <ended id>` exits 1 ("No session ... known").
4. **Some hosts cannot answer at all.** A host with an autologin console, a desktop or a `background` (cron) session has
   another session that keeps the user manager up; with linger off the probe is inconclusive there by design (D-pow-4: it
   reports, it does not decide).

## Changes
1. **Linger read (replaces A6's argv):** `["loginctl", "show-user", <uid>, "-p", "Linger"]`. The output must contain a
   `Linger=` line whose value is `yes` or `no`; anything else (no line, another value, a non-zero exit, a transport error)
   FAILS the probe as `unreadable`. A missing value is never "no".
2. **Session snapshots (one call each, no per-id reads):** `["loginctl", "list-sessions", "--json=short"]` once before the
   units start (snapshot A) and once after the 20 s wait, before the stops (snapshot B). Each is a JSON array of objects
   with at least `session` (string), `uid` (integer) and `class` (string); keep the entries whose `uid` is the probe's uid.
   Unparsable output, missing keys or a non-zero exit is `unreadable`. A **counted** entry is one whose `class` does not
   begin with `manager` (every other class, `background` included, pulls in or pins `user@.service`; only `manager` and
   `manager-early` are the manager itself). Each snapshot is taken from its own ssh login session, so each snapshot must
   contain **exactly one** counted id absent from the other snapshot (its own). Zero such ids in either snapshot means the
   premise failed (the probe's session is not registered, or one ssh session served both calls): the probe FAILS
   `unreadable`. More than one means a session opened or closed during the wait: the probe FAILS `inconclusive`.
   `other_sessions` = the number of counted ids present in **both** snapshots.
3. **Report:** the one `LINGER-PROBE ` line carries exactly `target`, `systemd` (the first stdout line of
   `["loginctl", "--version"]`, e.g. `systemd 261 (...)`; a failed read is `unreadable`), `linger` (`"yes"` or `"no"`,
   never null), `sessions` (snapshot A's entries for the uid as `[{"id": <session>, "class": <class>}]`), `other_sessions`
   (integer >= 0), `wait_s`, `unit_survived`, `inhibitor_survived`. Failure messages are `unreadable: <report JSON>` and
   `inconclusive: <report JSON>` (`json.dumps(..., sort_keys=True)`), so the gauge greps the three prefixes. Decision rule
   unchanged: conclusive when `linger` is `yes` or `other_sessions` is 0; then the test passes exactly when both units
   survived. Reading for the inventory's `power.linger`: `yes` and both survived = `enabled`; `no`, 0 and both survived =
   `not-required`; either died = `required-missing`; unreadable or inconclusive = `unknown`.
4. **Owner:** A6b, through the new `rewrite` row for `tests/runner/test_a6_runner.py::test_unit_and_inhibitor_survive_logout`
   (same name). The probe's parsing and counting is one function in `tests/runner/test_a6_runner.py`,
   `linger_report(uid, linger_stdout, snapshot_a, snapshot_b) -> dict` (raising a module-level `Unreadable` or
   `Inconclusive` that carries the report), so A6b's offline test feeds it captures: the comma form's empty output
   (unreadable); `Linger=no` with snapshots {manager, own user} and {manager, another own user} (other_sessions 0,
   conclusive); the same plus a `user` tty session in both (1, inconclusive); a `background` entry in both (1,
   inconclusive); identical snapshots (unreadable); a snapshot with no counted entry (unreadable). That offline test is not
   a named acceptance test. G0 for this row is the recorded A6 G3 ssh run on main `12396e0` (inconclusive, linger null,
   other_sessions -1); G1b's "passes on head" is A6b's G3 run of point 5. A6b's estimate stays under the 400-line cap; if
   it does not, the probe rewrite is the harvest item (Amendment 4 rule), never the offline test.
5. **Proof:** A6b's G3 runs the probe on a lane host with no other login session. Its report is the evidence for the
   inventory's `power.linger` decision (D-pow-4, OPEN-QUESTIONS); until then that decision stays open. SLICES.md (A-ASM
   step 5 and its owner line) and OPEN-QUESTIONS.txt now name A6b's probe.

## Checks
`tests/contracts_v2/test_v2_amendment8.py`; `test_v2_plan.py` stays green.
