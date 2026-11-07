# Amendment 13: site refresh, Daybreak must-fix gates before A7, and the state-machine definition of done

- **Status:** revision 2. `FROZEN.md` requires review before use. Revision 2: blind Fable review (APPROVE WITH NITS); its
  one medium finding and seven nits are taken (the checks file, the contract additions applied with their target fields,
  the four low-scoring machines, D5's wording, the SEATS lines, a stray punctuation mark, AD2's inferred claim, the
  A11b guard re-run). Revision 2 also records the lead's decisions on the two open points (see "Decided points").
- **Date:** 7 Nov 2026. **Approved:** by the owner, 7 Nov 2026: decisions D1-D6 (quoted below), the regression-guard rule
  (change 4), the Daybreak must-fix list (change 5) and the definition-of-done caveat (change 6).
- **Basis:** the lab's restart review of 6 Nov 2026 and the Daybreak security and state-machine review of 7 Nov 2026
  (static, read-only, at `d7e0d1d`), both kept in the lab's research notes outside this repo; this amendment's own reads
  on main `09f0ba7`. Host names are replaced by the role names of `SLICES.md`; the role-to-host mapping is in the external
  site map only.

Labels: **measured** = read or run on `09f0ba7` for this amendment; **reported** = stated by the cited review and not
re-run here; **inferred** = a conclusion from the evidence; **guess** = a proposed value with no measurement behind it.

## The owner's decisions (7 Nov 2026; host names replaced by role names)
- **D1** The pilot host is the pilot host of `SLICES.md` (the store host is booked for a long study and CI).
- **D2** Lane T is the single remaining card on host C; host C stays out of A-ASM.
- **D3** Take a golden capture of lane D's card (RTX 3090); the desktop host becomes a standby-only desktop lane at C-ASM.
- **D4** The repo stays public; on-lab proofs only via the local gauge, never a self-hosted runner.
- **D5** Races are Opus vs Sonnet for M packets; S packets one arm + a blind review; no catch-up lane. (The restart
  review's recommendation added "until a local model meets the bar"; the decision itself is "no catch-up lane".)
- **D6** Scope is milestone A through A-ASM only, interleaved with other lab work.
- **Regression guard** (owner, 7 Nov 2026): a hidden check may be a labelled regression guard, exempt from fail-on-base,
  when it protects behaviour that already works on base and the packet reworks that path.
- **Definition of done** (owner, verbatim): "restart once completed should leave all the unearthed state machines explicit
  and understood".

## Reproduced first
1. **The site roles are stale in four places** (reported by the restart review from the external site map and the lab's
   lane registry; the registry read again here, measured). Lane T is one card, not a pair. The store host now has two 48 GB
   cards, runs CI runners for other lab projects and its lane is held by a long study. The desktop host's lane D exists
   (since 16 Oct) with a 24 GB RTX 3090 that also drives the display; the role table still says "small card". A11b's G3
   ran on the store host as a stand-in pilot (reported), so A11b's guard proof is still owed on the pilot host (inferred).
2. **No golden capture exists for lane D's card model** (measured: `tests/fakes/captures/gpu/` holds `titan-rtx`,
   `quadro-rtx-8000` and `tesla-t4` only). Adding one breaks a frozen test: `tests/gpu/test_a3i_inventory.py:89` pins the
   set of card models to those three (measured), so the capture needs a migration-map rewrite row.
3. **POW-01 holds on main** (measured): `flightctl/executor_stdio.py` injects no Inhibitor into `ExecutorV2`;
   `flightctl/authority.py:770` sends `awake.hold_inhibitor: false`; `ExecutorV2.reconcile_inhibitors`
   (`flightctl/executor.py:2365`) has no production caller.
4. **CV-15 holds on main** (measured): `flightctl/discovery.py` keeps `project_proposal` (327), `DiscoveryHandler` (352)
   and the v1 `discover` (1255), and `tests/discovery/test_discovery.py` keeps 24 tests. A3c (PR #37, `09e99ca`) removed
   the AMD path and the private CSV parser only; its goal ("the only discovery is v2") is not met.
5. **BOOK-01, DB-01 and CMD-01 code paths are unchanged since the review** (measured by reading, not run): cancel accepts
   every owned state and an omitted revision (`authority.py:1776-1778`); the claim postflight writes `claimed` without a
   state or revision check (`authority.py:1200`); `commands.py:63` buffers both pipes with `communicate`; the only request
   byte ceiling is the executor's (`executor_stdio.py:29`, 1 MiB). The exploit scenarios themselves are reported, not
   reproduced; each fixing packet's G0 is the reproduction.
6. **Contract gaps** (measured): booking-v1 has no `claiming` state; `contracts/v2` has no authority request-byte ceiling,
   per-principal queue cap or retention constant; `error_code` has no `reply_too_large`, but `reply_unparsable`
   exists and fits an over-limit reply (inferred).
7. **Plan-test pins that shape the placement** (measured): `test_v2_amendment11.py:72` pins A5b1c's Depends cell, and
   `test_v2_amendment12.py:50` pins A12's; `test_v2_round7.py:435` pins the crosswalk row `A11, A11b, A12`, so A11 and
   A11b cannot move up the index. The contracts_v2 suite was green at base (842 passed, 2 xfailed, measured).
8. **Race outcomes** (reported by the restart review): R-A5a2, R-A11 and R-A5b1 ran Opus and Sonnet only; Sonnet won all
   three.

## Changes
1. **Site roles (D1-D4).** The role table in `SLICES.md` is updated: host C keeps the controller and owns a one-card
   lane T, which is not in A-ASM; the pilot host is confirmed as the first host to go live; the store host carries two
   48 GB cards, CI and long-study duty and is not the pilot; lane D is a 24 GB desktop card that also drives the display,
   standby-only, enabled at C-ASM. Owed pilot proofs that ran on a stand-in host (A11b's guard test) are re-run on the
   pilot host in the G3 debt sweep before A-ASM step 4 (written as a line in that step). D4: G2's lab-ci is the local gauge; no self-hosted runner is
   registered to this repo, and hosted CI stays on GitHub-hosted runners.
2. **Race policy (D5).** New seat shorthand `Sn` (Sonnet) and `BR` (blind review). The Seats cell of every unmerged
   milestone-A packet is rewritten: M packets `O, Sn`; S packets `O or Sn, BR` (the lead picks the arm and records it);
   lead-owned packets unchanged. GATES `WHO` says the same. Milestone B, C and D rows keep their old cells until a later
   amendment opens those milestones (D6). The SEATS lines of the eleven repriced briefs (A5a4, A4ub, A5b2, A5b1s, A5b1c,
   A7, A7b, A8, A9, A10, A12) are rewritten to match their cells.
3. **Scope (D6).** The restart ends when A-ASM is live on the pilot lane and the state-machine exit gate (change 6) is
   PROVEN. No milestone B, C or D packet is staged in this restart.
4. **Regression guard (G0b).** A race's hidden set may hold checks labelled `regression-guard`. Such a check is exempt
   from G0's fail-on-base rule only when (a) it passes on the base commit, (b) the packet's scope reworks the code path
   the check exercises, and (c) the label and the reason are written in the hidden set and in the PROOF file. A guard
   that fails on base is an ordinary hidden check. **First guard:** the v1-shaped protocol-2 stop check (a stop whose
   outcome is unknown answers 503 `unknown`, the lane is quarantined, the error carries no cause) joins the A5b2 hidden
   set when A5b2 is revalidated on the current main.
5. **Daybreak must-fix gates (G7), all before A7.** Placement rule: the listener (A7) is what first makes the
   authority's RPC boundary reachable from the network, and the review found the RPC findings latent until then
   (reported), so every gate below must be PROVEN before A7 starts. A7's Depends becomes `A5b2, A3c2, AD1-AD7`. The new
   packets sit in the index after A6b and before A7, except A3c2 (after A3c). A5b1c cannot formally depend on AD1-AD2
   (its Depends cell is pinned by a frozen test, item 7), but AD1-AD2 depend only on A5a3, so the lead may run them as an
   independent chain beside A5b2 and A5b1c (GATES FLOW step 4), which is the order the review recommends.

   | Gate | Finding | Closed by | Acceptance (all named tests fail on base, G0) |
   | --- | --- | --- | --- |
   | G7-POW | POW-01 | AD1, plus A5b1c | The stdio executor's production constructor holds the real Inhibitor; reconcile runs before the first request is served, and a failed reconcile serves no reserve (definite refusal); A5b1c's PROOF shows `hold_inhibitor` true for every lane on a `power.mode=sleeps` host and no grant without matching fence and held-inhibitor evidence. G3 on the pilot host. |
   | G7-SM | top three machines | AD2 (executor v2 fence), AD6 (allocation aggregate), AD7 (booking) | Each meets the G8 definition of done (change 6). |
   | G7-CMD | CMD-01 | AD3 | A child that writes past the combined stdout+stderr limit is killed (process group) as soon as the limit is passed, and the bytes held never exceed it; the transport bounds a reply before JSON decode and reports it as `reply_unparsable`, `definite: false` (an effect may have happened, so the caller takes the uncertain path). Realtime test with a real child. |
   | G7-DB | DB-01 | AD4, AD5 | A request over the byte ceiling is refused before parse; every operation request is validated at `Authority.handle` against the frozen operation schema (definite `invalid`; no row, event or idempotency record written); per-principal cap on open queue entries and a mutation rate quota answer `quota_exceeded`; terminal queue rows and idempotency rows past their replay deadline are collected; `queue list` and `report` read at most 1,000 rows per page with a cursor. |
   | G7-BOOK | BOOK-01 | AD7 | A cancel that races an in-flight claim never ends with the booking `claimed` (CAS on state and revision at reserve completion; a failed CAS issues a compensating stop and quarantines unless the cleanup is proven); terminal bookings cannot be cancelled. Realtime test with a delayed real executor. |
   | G7-DISC | CV-15 | A3c2 | No v1 discovery code or test remains (`project_proposal`, `DiscoveryHandler`, the v1 `discover`, `tests/discovery/test_discovery.py`), under A3c2's own migration rows (Amendment 9, change 4). |

   CI-01 and CI-02 are closed (measured on main: the trusted `pull_request_target` denylist workflow, PR #53, with a
   main-only `denylist` environment secret, PR #54; `.github/workflows/denylist.yml`). SAST is Ruff `S` plus CodeQL in
   place of semgrep, as recorded by `AMENDMENT-4-rev3-DRAFT.md` and PR #51 (Ruff `S` measured in `ci.yml`; CodeQL has no
   workflow file in the repo and was not re-checked here); not redone here.
6. **State-machine definition of done (G8, the restart exit gate).** Each of the 14 machines the review found has, on
   main: (1) one enum of its states, (2) a transition table naming every (state, event) cell as a legal transition or an
   explicit rejection, (3) one transition function that is the only writer of that state, and (4) a named test
   `test_<machine>_machine` that enumerates every cell and runs a seeded random event sequence checking the machine's
   invariants (stdlib only; no new test dependency). A retired machine meets the gate by deletion: its writer and tests
   are gone under migration rows, recorded in the PROOF. The function names below are the review's proposals; a packet
   may rename them and records the final name in its PROOF. G8 is checked at A-ASM before step 7 (the flip).

   | # | Machine | Enum (proposed) | Transition function (proposed) | Owner |
   | --- | --- | --- | --- | --- |
   | 1 | Authority lane/lease/reservation aggregate | `AllocationState` | `Authority._transition_reservation` | AD6 |
   | 2 | Executor v1 start/stop/reboot record | retired | deleted | A5r |
   | 3 | Executor v2 fence/deadline/inhibitor | `FenceState` (the 7 contract states) | `ExecutorV2.transition_lane` | AD2 |
   | 4 | Controller recovery/quarantine | `RecoveryState` | `Authority._transition_recovery` | A8 |
   | 5 | Release/install/admission gate | `ReleasePhase` | `ReleaseController.transition_release` | AS7 |
   | 6 | Booking/check-in/recovery | `BookingState` (with `claiming`) | `transition_booking` | AD7 |
   | 7 | Chat occupant/lease/lane aggregate | `OccupantState` | `Authority._transition_occupant` | AS1 |
   | 8 | Idempotency/external-effect handshake | `RequestState` | `store.transition_request` | AD5 |
   | 9 | Queue/dependency/admission binding | `QueueState` | `Authority._transition_queue_entry` | AS2 |
   | 10 | Host power/inhibitor | `HostPowerState` x `InhibitorState` | `PowerCoordinator.transition_host` | AS8 |
   | 11 | Workload systemd unit | `UnitState` | `transition_unit` | AS3 |
   | 12 | Approval/challenge | `ApprovalState` | `transition_approval` | AS4 |
   | 13 | Inventory/discovery (occupancy stays an observation value) | `InventoryRevisionState` | `transition_inventory` | AS5 |
   | 14 | Client wait/handoff/release | `ClientPhase` | the `RpcClient.lifecycle` reducer | AS6 |

   AS1-AS8 are S packets placed after A10 and before A11, inside A-ASM's `A0a-A12` range. A12's Depends cell is pinned
   (item 7), so A12 does not formally depend on AS8; its brief says the waker drives host power through AS8's function.
   A8 gains one named test (`test_recovery_machine`, five in all). States that only managed mode or later milestones
   reach (fence `staging`/`starting`/`running`, approval `revoked`) are table cells whose events no milestone-A code
   sends yet; the table still names them (inferred: this is what "explicit" requires without building milestone C;
   for the fence, AD2's staging settles it by measuring whether holder mode reaches any of them).
7. **Lane D golden capture: pending.** When lane D is free (it is in use by other lab work now), the gauge holds lane D
   through the current authority and records `nvidia-smi` inventory, NUMA and device-minor output read-only with the A2
   record wrapper, exactly as A3i's other captures were made; nothing is started on the card. The capture lands in a
   lead-owned commit with one migration-map rewrite row for
   `tests/gpu/test_a3i_inventory.py::test_golden_captures_all_card_models` (its model set gains the RTX 3090). It does
   not block A-ASM (lane D is not in it); it must land before lane D's inventory is confirmed at C-ASM.
8. **Contract additions (applied in revision 2).**
   - **C13-1** (AD7): `contracts/booking-v1.schema.json`, `$defs.booking.properties.state.enum` gains `claiming`
     (between `blocked` and `claimed`). This is a v1 wire-shape change: `flightctl/client.py` checks the booking state
     against a fixed set (measured: line 1016) and the v1 transition oracle `validate_booking_transition`
     (`tests/contracts/validation.py`) has no `claiming` cell, so AD7 updates both (the oracle under its own migration
     row). No frozen test pins the enum: the whole suite stays green with it (measured, Checks).
   - **C13-2** (AD4, AD5): `contracts/v2/policy.schema.json`, a new required object `properties.limits` with four
     constants, in the style of `timing`: `max_request_bytes` = 1048576 (guess: the executor's own ceiling),
     `max_open_queue_entries_per_principal` = 64 (guess), `max_mutations_per_principal_per_min` = 60 (guess) and
     `terminal_retention_s` = 604800 (guess: 7 days). No value is measured; changing one needs a new amendment.
     `config/policy-v2.json.example` gains the same `limits` block, as Amendment 11 did for `max_end_margin_s`
     (the frozen `test_max_end_margin_is_a_contract_constant` validates that example; without the block it fails,
     measured).
   - Events are not deleted (C5a derives usage from them); their growth is bounded by the rate quota and their reads by
     paging (inferred).
9. **Traceability.** CONFORMANCE.tsv rows `amd13-1` to `amd13-9`; GATES.txt G0b, G7, G8 and WHO; OPEN-QUESTIONS.txt the
   decided points and the lane D owner action.

## Decided points (lead decision 7 Nov per John's caveat/cauterize rule, John may override)
- **Q13-1 decided: the full definition of done for every machine.** Four machines score 9 or lower in the review:
  client lifecycle 6, approval 8, inventory 8 and workload unit 9 (AS6, AS4, AS5, AS3). They keep the full G8 artefacts;
  the owner's caveat names all the machines, and a table without its test is not checked.
- **Q13-2 decided: deletion meets G8 for executor v1** (machine 2, A5r), with the deletion recorded in A5r's PROOF.

## Checks
- `tests/contracts_v2/test_v2_amendment13.py` pins the 15 new packet rows (A3c2, AD1-AD7, AS1-AS8), A7's Depends, the
  Seats cells and SEATS lines of the eleven repriced packets, the G8 table of 14 with owners, the G0b, G7 and G8 gate
  text, and the C13-1 and C13-2 contract values. It fails on `09f0ba7` and passes on this revision (measured; counts in
  the commit message).
- The whole suite stays green with the contract additions; `deploy/check_portability.py` with the external denylist
  passes on this branch.

## Dated decisions
| Date | Decision | By |
| --- | --- | --- |
| 2026-11-07 | D1-D6 as quoted above | owner |
| 2026-11-07 | Regression-guard rule; first guard in A5b2's hidden set | owner |
| 2026-11-07 | DB-01, CMD-01, BOOK-01, POW-01, the top three machines and CV-15 are must-fix before A7 | owner |
| 2026-11-07 | Restart definition of done: all 14 machines explicit (G8) | owner |
| 2026-11-07 | Placement: AD1-AD7 after A6b, A3c2 after A3c, AS1-AS8 after A10; one machine test per machine | lead (this amendment) |
| 2026-11-07 | CMD-01 maps to `reply_unparsable` (no new error code); events are paged, not deleted | lead (this amendment) |
| 2026-11-07 | Q13-1: full definition of done for all 14 machines; Q13-2: deletion meets G8 for executor v1 | lead decision per John's caveat/cauterize rule; John may override |
| 2026-11-07 | C13-1 and C13-2 applied with guessed values | lead (revision 2) |
