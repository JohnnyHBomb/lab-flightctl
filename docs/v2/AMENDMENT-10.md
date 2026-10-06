# Amendment 10: an allow-list argv0 entry may not begin or end with ASCII whitespace

- **Date:** 6 Oct 2026. **Approved:** the owner approved the rule on 6 Oct 2026: ASCII whitespace only, a standalone
  amendment, the conforming commit written by the lead (not raced). It needs review before use, as `FROZEN.md` requires.
- **Basis:** the "Follow-up, not in this amendment" line of Amendment 5 (revision 3): "a whitespace pattern on the allow-list
  `argv0` (no leading or trailing space) ... It needs the same rule in `occupancy()`, or the schema and the probe would
  disagree." Measured on main `5aefe88` unless labelled.
- **Form:** one commit: this amendment, the two schema items, the oracle, and the conforming changes in `flightctl/gpu.py`
  (A3) and `flightctl/siteconfig.py` (A4u). Both packets are merged, so the conforming change goes with the contract change.

## Reproduced first (measured on 5aefe88)
1. **The schemas accept any 1-4096 character argv0.** The allow-list item in `gpu-probe.schema.json` (occupancy
   `thresholds.noise_allowlist`) and in `inventory.schema.json` (lane `external_tenant.noise_allowlist`) was
   `{"type": "string", "minLength": 1, "maxLength": 4096}`. The `noise_allowlist` blocks in `executor.schema.json` are examples,
   not schema.
2. **The code checks the same and no more.** `NvidiaOccupancyProbe.occupancy()` requires a `str` of 1-4096 characters.
   `_bind_lane` in `flightctl/siteconfig.py` only checks that argv0 is a `str`. The oracle `argv0_matches` requires a non-empty `str`.
3. **A whitespace-edged entry cannot match what its author meant.** The Amendment 5 rule is: argv0 equals the entry, or begins
   with the entry followed by U+0020. So the entry `"browser "` matches only argv0 `"browser "` or `"browser  ..."` (two spaces),
   never `"browser"` or `"browser --type=gpu-process"`, and `argv0_matches("browser ", "browser ")` returned True. The entry
   `" x"` matches only an argv0 that begins with a space. A trailing tab, LF or CR is equally useless, because only U+0020
   separates. All 13 new test cases in `test_v2_amendment10.py` fail on `5aefe88` (see Checks).
4. **The effect is fail-closed (inferred).** Such an entry matches a subset of what the clean entry matches, so the process
   stays a tenant and admission is blocked. This is hygiene, not safety: it costs an operator a confusing "why is the browser a
   tenant" session.

## Changes
1. **Rule.** An allow-list `argv0` entry must not begin or end with U+0020 or U+0009 to U+000D (tab, LF, VT, FF, CR).
   - Whitespace inside an entry stays legal: executable paths contain spaces (measured in Amendment 5).
   - Non-ASCII whitespace (for example U+00A0, U+2003, U+0085) and U+001C to U+001F are NOT refused. They are not separators
     under Amendment 5 either, and JSON Schema engines disagree on what `\s` means (the owner's decision: ASCII only).
   - The length rules (1-4096) are unchanged.
2. **Schemas** (`gpu-probe.schema.json`, `inventory.schema.json`, the same text in both): the allow-list item's `argv0` gains
   `"pattern": "^[^ \\t-\\r]([\\s\\S]*[^ \\t-\\r])?(?![\\s\\S])"` and a description.
   - The end anchor is `(?![\s\S])`, not `$`: Python's `re` lets `$` match before a final newline, so `$` would accept `"x\n"`.
   - `^` is required because a JSON Schema `pattern` is not anchored. The syntax is ECMA 262, so other validators accept it.
3. **Probe** (`flightctl/gpu.py`): `ARGV0_EDGE_WHITESPACE = " \t\n\v\f\r"`; `occupancy()` refuses an entry whose first or last
   character is in it (`ValueError`, "argv0 must not start or end with whitespace"), after the length check.
4. **Site config** (`flightctl/siteconfig.py`, `_bind_lane`): an entry that is empty or begins or ends with a character of the
   same constant is refused with `SiteConfigRefused` naming the lane, so a bad `inventory.json` stops at load time before the
   probe is asked. Before this, the loader passed the entry on and the probe raised a bare `ValueError` (an empty entry did the
   same; it is refused here too because the same check covers it).
5. **Oracle** (`tests/contracts_v2/validation.py`, `argv0_matches`): returns False for an entry with edge ASCII whitespace, as it
   already did for an empty entry. A hand-written observation can then not claim noise through a refused entry.
6. **Traceability.** New CONFORMANCE row `amd10-1` (`contract-fixed`; the probe and the loader conform in the same commit).
   `amd5-3` gets a note pointing at `amd10-1`. `AMENDMENT-5.md` is left unchanged (history); this amendment closes its follow-up.

## Existing data (measured, read-only)
No `noise_allowlist` entry is refused in `config/inventory-v2.json.example` or in the lead's inventory drafts outside the repo
(1 entry each, `"browser"`). No confirmed v2 site inventory was found on the controller host.

## Checks
- New tests: `tests/contracts_v2/test_v2_amendment10.py`.
  - Both schemas, the probe and the lane binding refuse leading or trailing space, tab, LF, VT, FF and CR, and accept inner
    whitespace, a 4096-character entry and non-ASCII whitespace at either edge.
  - One table test: for every character up to U+3100, the entries `"a" + c` and `c + "a"` are accepted by the schema pattern,
    the probe and the oracle alike, and refused exactly when `c` is ASCII whitespace.
  - The end anchor: the pattern refuses `"x\n"`, and the `$` mutant accepts it.
  - The oracle: `argv0_matches("browser ", "browser ")` is False; `argv0_matches("browser x", "browser")` stays True.
- Base-fail check (measured): the 13 cases, copied onto `5aefe88` unchanged, all fail there. Their inner-whitespace and
  non-ASCII cases are regression guards inside the same tests.
- Mutants (measured): dropping the trailing-edge check from the probe, the loader or the oracle, or `$` for the end anchor,
  each fails at least one new test.
- The full suite, portability, the migration gate and lab-ci are reported in the hand-back.
