# Amendment 14: G3 evidence must come from the host it names

- **Date:** 8 Nov 2026. **Approved:** by the owner, as drift-plan item P15 (v1.8 retrospective, 7 Nov 2026). It needs
  review before use, as `FROZEN.md` requires.
- **Basis:** the G3 debt sweep on main `1f08f44` (7 Nov 2026), findings F1 and F4, and its A4 pilot addendum. Stacked on
  Amendment 13. It changes the G3 rule in `GATES.txt` (what strict evidence the gauge counts), so it is an amendment,
  not only a harness fix. No packet, port, dependency or named test of `SLICES.md` changes.

## Reproduced first (measured in the sweep unless marked)
1. **F1, vacuous clock evidence.** A strict ssh-form run (TARGET = the pilot host, runner = another host) wrote
   `conformance-clock-real-<pilot>-*.json` with `"strict": true, "proven": true`. The boot id the real twin read was the
   runner's, not the pilot's: `RealClock` reads the local kernel and the factory ignored the target. The file named a host
   the twin never touched, and nothing in it could show that. Replayed through the new validator, that file fails
   (`test_the_7_nov_vacuous_clock_evidence_fails_the_strict_validator`).
2. **F4.** `"strict"` was set for the whole session, so ports not listed in `FLIGHTCTL_CONFORMANCE_PORTS` also got
   `strict: true`; host-local evidence carried no hostname or boot id (the sweep added sidecar files by hand).
3. **Bare target name.** In the A4 pilot addendum, the same test passed with the target's FQDN and timed out with the
   bare host name (the bare name did not reach the tailnet listener; inferred in the addendum).

## Changes
1. **Observed identity.** Every real/dryrun evidence file records `"observed": {"hostname", "boot_id"}`, read once per
   session on the target through the transport the real twins use: local reads for `host-local`, BatchMode ssh to the
   target otherwise (`tests/conformance/host_identity.py`). If it cannot be read, the file records `"observed": null` and
   `"identity_problems"`.
2. **Strict refuses a mismatch.** In strict mode a real/dryrun case of a listed port fails at setup when the identity
   cannot be read or is not the target's, and such a file is never `proven`. Match rule: `host-local` needs the local
   identity; otherwise the target's host part (without `user@`) equals the observed hostname or has the same first DNS
   label. An IP-address target never matches. `"strict"` is true only for the listed ports (F4).
3. **The gauge's validator.** The gauge counts a G3 evidence file only if `host_identity.evidence_problems(record)` is
   empty: strict, a real or dryrun twin, every case passed or n/a, proven, and an observed identity that matches the
   target. Evidence written before this amendment has no `observed` and does not count; re-run it.
4. **The real clock twin refuses a remote target** with a clear error (`RemoteClockTarget`). Clock G3 runs host-local on
   each host it serves. Host-local runs are unchanged. The A1 test
   `tests/clock/test_a1_clock.py::test_clock_twins_registered_for_conformance` built the real twin with a remote target
   (`"host-1"`), the behaviour this change removes; it is updated in place to `"host-local"` (same assertions).
5. **TARGET normalisation.** `registry.target()` (and the A4 transport test) normalise `FLIGHTCTL_CONFORMANCE_TARGET`:
   `host-local` is unchanged; a bare host name (no dot, not an IP) in `host` or `user@host` becomes the resolver's
   canonical name when that is the bare name plus a domain; otherwise, or on a lookup error, the target is unchanged.
6. **Traceability.** `GATES.txt` G3 gains the Amendment 14 paragraph; new CONFORMANCE.tsv rows `amd14-1` to `amd14-3`.

## Checks
`tests/contracts_v2/test_v2_amendment14.py`: the fixture
`tests/contracts_v2/captures/g3-vacuous-clock-20261107.json` (the F1 file, host-neutral) fails the validator, both as
written and with the runner's identity recorded; matching evidence passes; the clock factory refuses a remote target;
normalisation with a synthetic resolver; strict per port; a real host-local strict clock run writes evidence that the
validator accepts; the G3 text and rows are pinned. All fail on `1f08f44` and pass here.
