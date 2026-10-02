# Migration from contract set v1 to v2

## Principles

- **Version, don't break.** v1 schemas, vectors and tests stay byte-identical until the packet that
  replaces their consumer lands. A packet that moves a module to v2 also deletes or rewrites the v1 tests
  that pin the old behaviour. It lists them in its report, and the gauge checks that list against the
  table below.
- **One authority, two paths for one release.** The authority serves `POST /v2/rpc` natively and
  `POST /v1/rpc` through an adapter that translates v1 envelopes to v2 (`ingress` dropped, `extend_s`
  turned into `ttl_s`, preempt-by-token refused with a message naming `preempt <lease_id>`). The `/v1`
  path is removed one release after cut-over of the last lane.
- **Executor and authority upgrade together per host.** executor-v2 is a semantic change, so v1 and v2
  executors never talk to the same authority. A host runs v2 only, and the executor refuses
  `schema_version != 2` with a typed `unsupported_version` error.
- **The legacy shim is the compatibility surface for legacy callers**, not `/v1`. Legacy `lanes.sh`
  callers never spoke the v1 RPC.

## Data migration (SQLite, one-shot, run by the release tool with admission closed)

| v1 record | v2 record | Rule |
| --- | --- | --- |
| lease (token, unit, invocation set after reserve) | lease v2 | `token_sha256 = sha256(token)`; the raw token is dropped. `run_id` is minted. `unit = flightctl-<lane>-g<gen>.service`. `expires_at = approved_max_end = max_end`. `holder_binding = ttl-only`. Only drained leases are migrated: the release tool refuses a migration with live leases (plan r4: never migrate under occupants). |
| booking, queue, approval, pipeline | unchanged | Copied. |
| occupant | endpoint | Migrated only when `unloaded`, otherwise refused (drain first). |
| event (no seq) | event v2 | `seq` assigned in `event_id` insertion order, `refs` from `data`, `actor` from the principal subject. |
| inventory-v1 `identity_mapping` | accounts document | One principal per mapping. The role becomes `kind` (`operator`, `agent`, `service`). Agents get `sponsor` = the operator principal until the owner assigns one. |
| inventory-v1 device | inventory v2 device | `uuid`, `pci_bus_id`, `numa_node`, `drives_display` come from a fresh InventoryProbe run. They are null with a reason when not probed, and semantics then refuse to enable the lane. |
| common-v1 policy | policy v2 + quotas | `admission` constants move to `timing`. `quotas` was never valid in v1 (Grok 6); defaults come from the quotas document. |

## v1 tests that become obsolete, and where their obligation goes

**The checked, authoritative form is `docs/v2/migration-map.tsv`** (Sol 6 B7). The gauge enforces it per
packet as GATES G1b: a packet may touch only the pre-existing tests mapped to it, and every rewrite names its
replacement test. `tests/contracts_v2/test_v2_plan.py` validates the map. The table below explains it.

Tests marked **rewrite** keep their obligation under a v2 name in the owning packet. Tests marked
**delete** pin the defect itself.

| v1 test (file) | Pins | Action | Owning packet |
| --- | --- | --- | --- |
| `tests/authority/test_revision2.py` renew assertions (approved maximum 409, around line 704) | Renew can never succeed (Grok 2) | rewrite: renew succeeds inside the ceiling and is 409 only past `approved_max_end` | A5b1, A5b2 |
| `tests/contracts/vectors/cli.json` + `tests/client/test_vectors.py` "lane-first positional renew TTL minutes to seconds" (expects `approved maximum reached`) | Same | rewrite vector: `renewed <lane> to HH:MM` (shim), mutation on the new CLI | A5b1, A5b2, A10 |
| `tests/contracts/vectors/rpc.json` renew/preempt token forms | Preempt by token, `extend_s` | stays for the `/v1` adapter until it is removed; v2 vectors in `tests/contracts_v2` | A7b |
| `tests/authority/test_revision2.py::test_revision2_restart_reconciliation_and_corrupt_store_keep_admission_closed` | Permanent freeze after restart (G27) | rewrite: per-lane reconcile reopens a lane after inspect; a corrupt store still keeps admission closed | A8 |
| `tests/executor/test_executor.py` `identity()` helper (`unit=None` at reserve, absolute deadline with `boot_id`) and the tests built on it: `test_reserve_start_fence`, `test_clock_reboot_and_same_boot_deadline`, `test_reboot_reconcile_rejects_partial_and_reanchors_deadline`, `test_repeated_reboots_preserve_absolute_deadline`, `test_deadline_enforcement_calls_before_and_at_boundaries` | Cross-host absolute deadlines (G02); identity assigned after reserve (Grok 1) | rewrite against executor-v2: relative deadlines anchored locally, full identity at reserve. The reboot cases survive with "own boot changed = reboot, reconcile" | A5a |
| `tests/executor/test_executor.py::test_fake_isolation_guard` and `tests/executor/systemd_adapter.py` | Workaround for the frozen fake | replaced by the per-unit FakeRunner and `tests/conformance/test_workload_runner.py`; delete the package-local adapter | A6 |
| `tests/contracts/test_fakes.py::test_fake_interfaces` (systemd part: unscripted call = success, instance-wide clear) | Grok 5 fake semantics | rewrite: unscripted means unknown and occupancy is per unit. The GPU part moves to the occupancy probe fakes | A6, A3 |
| `tests/fakes/fixtures/*.json` (`raw` = private CSV `name,memory,driver,vendor`) and the `discovery.py` NVIDIA/AMD parsers they feed (`tests/discovery/test_discovery.py::test_raw_discovery_parsing_and_multidevice_lane`, `test_raw_transport_inputs_use_production_parsers_and_projection`) | Grok 4: not real `nvidia-smi` output | rewrite with golden captures of the real query on every lab card model; AMD cases deleted (no AMD compute lane) | A3, A3b |
| `tests/integration/test_p6_scaffold.py` assembled gate requiring `ondemand/qwen-od-proxy.sh` (around line 567) | Grok 8: names a file no package owns | rewrite: the assembled gate becomes the A-ASM/B-ASM/C-ASM acceptance runs (GATES G6); the scaffold tests stay as file-backend rehearsals | A0c |
| `tests/client/test_client.py` default wait 600 s for `lanes.sh` | The new CLI keeps 10 min; the legacy shim restores 120 min | unchanged for the new CLI; the shim vectors cover 120 | A10 |
| `tests/roster/*` (repo `roster/*.sh`) | Shell lifecycle against a client the repo never shipped | frozen as reference and retired from the plan (BLAST-RADIUS.txt R1); tests stay green until the retirement packet deletes them with the scripts | B3 |

Every other v1 test keeps passing unchanged: authority admission, idempotency, queue FIFO, bookings,
approval crypto, chat accounting, discovery diff and determinism, release scaffold, portability. The
v2 packets must not break them, and lab-ci runs them all on every packet (GATES G2).

## Rollback

Every packet is revertible on its own until its assembly packet runs. After cut-over, rollback to legacy
follows CUTOVER-RUNBOOK (lab side): close admission, drain, restore the renamed legacy `lanes.sh`, and
remove the forced-command keys. The v2 SQLite stays read-only for audit and is never resurrected into
legacy JSON while an occupant remains.
