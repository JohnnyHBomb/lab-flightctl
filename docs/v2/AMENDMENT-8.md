# Amendment 8: the A6 linger probe reads loginctl correctly on systemd 256 and later (fix owned by A6b)

- **Date:** 5 Oct 2026. **Approved:** pending the owner. It needs review before use, as `FROZEN.md` requires.
- **Basis:** A6's G3 run on main `12396e0` (the A6 merge), on lab hosts running systemd 261 (measured).

## Reproduced first (measured)
1. **The probe's property read returns nothing.** `loginctl show-user <uid> --property=Linger,Sessions` (the argv A6's
   packet fixed) prints no output and exits 0 on systemd 261. `loginctl show-user <uid> -p Linger -p Sessions` prints both
   properties. A6's `test_unit_and_inhibitor_survive_logout` therefore reported `"linger": null, "other_sessions": -1` and
   failed as inconclusive on an ssh target where both units survived.
2. **The user manager is a session.** Since systemd 256 the per-user service manager is listed in `Sessions` as its own
   session with `Class=manager`. `other_sessions = len(Sessions) - 1` counts it, so it can never reach 0, and the probe
   could never be conclusive on a host where linger is off.
3. **Some hosts cannot answer at all.** A host with an autologin console or a desktop session has another `user`-class
   session; with linger off, the probe is inconclusive there by design (D-pow-4: it reports, it does not decide).

## Changes
1. **Probe reads (replaces A6's argv):** `["loginctl", "show-user", <uid>, "-p", "Linger", "-p", "Sessions"]`. If the
   output lacks either the `Linger=` or the `Sessions=` line, or the command fails, the probe FAILS as `unreadable` (a
   different message from `inconclusive`); it never treats a missing value as "no".
2. **Session classes:** for each id in `Sessions`, `["loginctl", "show-session", <id>, "-p", "Class", "--value"]` through
   the same runner. `other_sessions` = the number of sessions whose class does not begin with `manager` or `background`,
   minus one (the probe's own session). A failed class read is `unreadable`.
3. **Report:** adds `"systemd"` (the first line of `["loginctl", "--version"]`) and `"sessions"` (a list of
   `{"id", "class"}`). Every other field and the decision rule stay as A6 wrote them (conclusive when linger is `yes` or
   `other_sessions` is 0).
4. **Owner:** A6b, through the new `rewrite` row for `tests/runner/test_a6_runner.py::test_unit_and_inhibitor_survive_logout`
   (same name). The offline captures for the probe's reads (both forms of point 1, a `manager` session) are part of A6b's
   tests.
5. **Proof:** A6b's G3 runs the probe on a lane host with no other login session. Its report is the evidence for the
   inventory's `power.linger` decision (D-pow-4, OPEN-QUESTIONS); until then that decision stays open.

## Checks
`tests/contracts_v2/test_v2_amendment8.py`; `test_v2_plan.py` stays green.
