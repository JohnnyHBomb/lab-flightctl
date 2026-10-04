# Amendment 5: contract and plan follow-ups from races R-A2 and R-A3

- **Date:** 4 Oct 2026.
- **Basis:**
  - the R-A3 race README "Plan notes";
  - the R-A3 hidden-set validation record, checklist item 3;
  - the R-A3 harvest log;
  - the R-A2 brief and A2's merged `flightctl/commands.py`;
  - the owner's approval of the A3 split (4 Oct).
- **Form:** two commits on the A3 merge (PR #19, c380e5f): this amendment, then "A3: conform to Amendment 5" (revision 2, below), plus the review fixes of revision 3. It needs review before use, as `FROZEN.md` requires.

## Reproduced first (measured on 8a7fd34 unless labelled; scratch `repro_amd5.py`)

1. **A3 is one packet.** The packet index has one A3 row delivering both `inventory_probe` and `occupancy_probe`, with seven acceptance items. Race R-A3 had to cut it into a part 1 (the OccupancyProbe real twin, four named tests). Its README records why the rest does not fit: golden captures only the gauge can record, and a hash-verified inventory that only A4u provides.
2. **`CommandResult` has seven keys.** A2's `LocalCommandRunner().run(["true"], ...)` returns eight; the extra one is `error`.
3. **One-string command lines.** A browser GPU process rewrites `/proc/<pid>/cmdline` into ONE string: 817 bytes with one NUL, measured during R-A3. So its argv0 is the whole line, and an allow-list entry can never equal it.
   - On this host, 56 processes hold such a single string with a space in it. Measured, read-only scan:
     - Chrome renderers: 703 bytes, one NUL;
     - a GPU process whose executable directory name contains a space (`<dir with a space>/<binary> --type=gpu-process ...`): 912 bytes, one NUL, **and its executable path contains a space**.
   - `readlink /proc/<pid>/exe` fails for another user's process (rc 1, measured), while `/proc/<pid>/cmdline` is readable.
4. **PIDS query form.** `x-real-commands.occupancy_process_types` named plain `nvidia-smi -q -d PIDS`. That form keys its sections by PCI bus id, which the occupancy gpus query does not carry; R-A3 measured this, and the race used `-q -d PIDS -i <uuid>`.
5. **Oracle against schema.** `occupancy_from_capture` returned `status: ok` observations that the schema refuses, for all 9 inputs:
   - a fractional utilization;
   - a NaN utilization;
   - a temperature of 131;
   - a temperature of -1;
   - memory.total 0;
   - a negative power draw;
   - a negative ECC count;
   - pid 0;
   - a context type outside C, G and C+G.
6. **G3 can pass vacuously.** In the race sandbox (no `/dev/nvidia*`), the real twin returns `unknown` and the strict conformance run passes its two real cases on that path (R-A3 README, measured). G3 did not require an `ok` observation.

## Changes

1. **A3 split** (the owner approved it on 4 Oct).
   - **A3, part 1:** the OccupancyProbe real twin. Ports: `occupancy_probe`. Its four named tests are those raced in R-A3:
     - `test_production_parser_agrees_with_oracle_on_all_captures`;
     - `test_small_unlisted_cuda_process_is_a_tenant`;
     - `test_noise_needs_allowlisted_identity_and_cap`;
     - `test_probe_real_process_timeout`.
   - **New A3i, part 2:** the InventoryProbe real twin, golden captures per card model (recorded read-only by the gauge: Titan RTX and RTX 8000 now, the T4 when its host is free), and the replay-backed fakes of both probes. Ports: `inventory_probe`. It owns `test_golden_captures_all_card_models` and the three `tests/fakes/fixtures/*.json` migration rows, now owned by A3i.
   - **Ordering:** A3b now depends on A3i.
   - **`test_expected_uuids_come_from_confirmed_inventory`** moves to A4u, because the hash-verified confirmed inventory is A4u's `flightctl/siteconfig.py`. A4u now also depends on A3, the prerequisite that the probe takes `uuids` from its caller.
   - **Also updated:** the registry's `OWED_BY` (`inventory_probe` → A3i), the crosswalk, and the assembly prerequisite check (A3i delivers `inventory_probe` before A-ASM; `test_v2_plan.py` checks it unchanged).
   - **Gauge test:** `test_desktop_user_legacy_jobs_remain_tenants` stays with the gauge at G3.
2. **`CommandResult` gains `error`** (`typed_error` or None), exactly as A2 returns it: None when the command ran and exited, whatever its returncode; the typed error for a failure the runner itself saw (spawn, timeout, unreachable host, ssh exit 255).
3. **One-string command lines: a matching rule, not a new argv0.**
   - argv0 stays the text before the first NUL.
   - An allow-list entry now matches when argv0 EQUALS it or BEGINS with it followed by a space (oracle `argv0_matches`, used by `noise_identity_ok`).
   - **Not the suggested "before the first space" rule:** it would break on executable paths with spaces. The measured 912-byte GPU process would be cut at the space inside its directory name.
   - **Not `/proc/<pid>/exe`:** it is unreadable across users, and the probe runs as the executor account, not the desktop user.
   - **Unchanged:** root and DynamicUser uids are never noise, the context must be G or C+G, the cap holds, and the trust boundary stays (only processes of the entry's uid can set this text). A shorter entry that ends at a space (e.g. `<dir>/Some` for `<dir>/Some App/...`) also matches; the operator writes the entries, and the uid is trusted either way.
   - **Revision 3 (review finding):** an argv0 longer than 4096 characters, or empty, is null and never noise, in the probe AND the oracle. The probe already did this; the oracle took the raw text, so with the prefix rule it could call a very long one-string line noise and return an observation the schema refuses (`argv0` maxLength 4096). Only U+0020 separates: a tab after the entry does not match.
4. **x-real-commands** names the per-card `nvidia-smi -q -d PIDS -i <uuid>`, one call per lane card, and says why. The A3 brief says the same.
5. **The oracle now refuses what the schema refuses.**
   - A value nvidia-smi does not print makes the observation `unknown`:
     - a fractional or non-finite number in an integer field;
     - a number outside the `gpu_sample` bounds, negative memory included;
     - pid 0;
     - a negative process memory.
   - A context type outside C, G and C+G is `null`, not `unknown`. That matches the race brief's DECIDED 6 and the merged part-1 code, and a null type is never noise, so the process stays a tenant (fail-safe).
   - A seeded 600-case corpus shows the oracle's output is always schema-valid.
6. **GATES G3:** the evidence for a probe's real twin must include at least one `status: ok` observation from the real host (schema-valid, and `occupancy_semantics` clean for an occupancy probe), because strict evidence alone can be vacuous without the hardware.

## A3 part 1 conforms (revision 2: this amendment now sits on the A3 merge, PR #19, c380e5f)

Revision 1 listed these changes for the then-unmerged `flightctl/gpu.py`. Revision 2 rebases the amendment onto c380e5f and makes them in a second commit, "A3: conform to Amendment 5":
- **Item 3:** `_noise_ok` matches when argv0 (not None) equals the entry or begins with the entry followed by a space.
- **Item 5:**
  - `_parse_gpus` applies the `gpu_sample` bounds (integers where the schema says integer, finite numbers, the schema's limits) and returns unknown on a violation;
  - `_parse_procs` refuses pid 0 and a negative used memory;
  - `_context_types` already mapped an unknown type to `None`.
- **Tests:** `tests/gpu/test_a3_amendment5.py` has 12 cases. 11 fail on c380e5f's `gpu.py` (measured) and agree with the oracle (the 12th is a regression guard: readable uid, unreadable cmdline, a tenant):
  - the one-string GPU-process command line from the measurement, whose path holds a space;
  - eight out-of-range or fractional gpu values;
  - pid 0;
  - a negative process memory.
- The merged A3 tests stay green, and the R-A3 hidden set's result is in the hand-back.

## Revision 3 (review fixes, 4 Oct)
- The oracle nulls an argv0 that is empty or longer than 4096 (change 3 above); the schema's `process_identity` text says so.
- The oracle accepts a pid only in ASCII digits, like the probe (a non-ASCII digit such as `٤` made it parse a pid the probe refuses; fail-safe, but a parse gap).
- `_noise_ok` in `flightctl/gpu.py` refuses an empty entry itself, like `argv0_matches` (it was unreachable through `occupancy()`, which refuses empty entries).
- Stale "A3" pointers now name A3i, and the round-5 plan test checks A4u's acceptance.
- **Follow-up, not in this amendment:** a whitespace pattern on the allow-list `argv0` (no leading or trailing space), so a useless entry such as `"browser "` is refused at config time. It needs the same rule in `occupancy()`, or the schema and the probe would disagree.

## Checks
- New tests: `tests/contracts_v2/test_v2_amendment5.py` (29 cases; revision 3 added two).
- The mutation checks, the full suite, lab-ci, the `pr` steps and the scans are reported in the hand-back.
