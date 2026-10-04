# Amendment 6: executor_transport conformance moves from A4 to A5a

- **Date:** 4 Oct 2026.
- **Basis:** race R-A4 staging (lead's reference and hidden set), measured on main `8983935`.
- **Approved:** by the owner, 4 Oct 2026. It needs review before use, as `FROZEN.md` requires.

## Reproduced first (measured)
- `tests/conformance/test_executor_transport.py` (frozen) checks every reply against the v2 executor schema and expects
  v2 states: line 25 asserts `observed_state == "reserved"` after a reserve, line 59 `"free"` after a stop.
- The executor on main is the v1 contract (`flightctl/executor.py`): replies carry `schema_version` 1, its states are
  `unknown, free, starting, running, stopping, quarantined` (no `reserved`), and it echoes the raw token.
- The v2 executor is packet A5a, which depends on A4. So no A4 entry can pass "executor_transport conformance
  [strict]", whatever its transports do.

## Changes
1. **A4 ACCEPTANCE** drops "executor_transport conformance [strict]" and gains the named test
   `test_transport_failures_are_typed_and_bounded` [realtime] (the transports' status table and bound). A4's packet-index
   port becomes `-`: A4 delivers the transports, not a conformance-registered port.
2. **A5a ACCEPTANCE** gains "executor_transport conformance [strict]": A5a registers the transport rig over A4's
   `LocalSubprocessTransport` and its v2 executor, and updates the requests in A4's tests to v2. A5a's packet-index port
   becomes `executor_transport`.
3. **`tests/conformance/registry.py` `OWED_BY`:** `executor_transport` → `A5a`, so the skip message names the packet that
   owes it (`test_v2_plan.py` checks this map against the packet index).

4. **A4 SCOPE pins `tests/transport/test_a4_transport.py`**, and **this amendment adds four migration-map rows** (packet
   A5a, `rewrite`, each A4 named test kept under its own name). Revision 2 (Fable 5.1 review): rows are added by the
   lead's contracts-v2 stream, never by the packet that uses them, because `tools/migration_gate.py` reads the map from
   the head checkout and a packet could otherwise license its own edits (Amendment 2 precedent for A0c rows).

The ordering is unchanged: A-ASM still receives `executor_transport` before it runs (A5a precedes A-ASM).

## Checks
- `tests/contracts_v2/test_v2_amendment6.py`; the existing plan checks (`test_v2_plan.py`) stay green.
