# Flightctl v1 contracts

This directory freezes portable data contracts for the controller work packages. It contains no controller runtime, network client, executor, cryptography, site inventory, or federation implementation. JSON is the wire/storage representation; timestamps are UTC ISO-8601 strings ending in `Z`, while persisted implementations may use epoch seconds.

## Authority, admission, and idempotency

`POST /v1/rpc` is the sole authority boundary. Every request carries an authenticated ingress context, an atomic admission marker, an approval selection, a versioned pipeline binding when applicable, an optional attenuated delegation, and an optional complete batch registration. The server derives actor/subject and operator elevation from the actual socket peer; forwarding headers are ignored. A batch registers every arm and dependency before any arm executes. The top-level RPC `lane` is the only lane selector; operation arguments do not repeat it.

The request has `request_id`, `idempotency_scope`, and a SHA-256 `request_fingerprint`. The durable idempotency record stores that scope, fingerprint, status, typed data, error, and storage time. Reusing a request ID with a different fingerprint is a `409 conflict`; a retry with the same scope and fingerprint returns the persisted result.

## RPC results and failures

`rpc-envelope-v1` does not accept an arbitrary response object. Complete results are discriminated by `data.kind`: `grant` (token, generation, typed lease and reservation), `pending`, `reachability`, `occupancy`, `projection` (calendar/free windows), `status`, `mutation`, `queue`, `booking`, `approval`, or `report`. A grant marks `adoption.mode` as `fresh-acquire` or `authenticated-adoption`; a fresh acquire and authenticated adoption are separate operations. Measurements carry `confirmed`, `estimate`, or `unknown`; estimates and unknowns carry a reason. A pending result is only valid with HTTP `202`.

| HTTP status | Meaning | Required result shape | CLI exit |
| --- | --- | --- | ---: |
| `200` | complete | typed data, `error: null` | `0` |
| `202` | pending | `data.kind: pending`, `error: null` | `5` |
| `403` | denied by policy or approval | `data: null`, typed error | `2` |
| `409` | busy, conflict, or stale generation | `data: null`, typed error | `1` |
| `503` | unavailable or unknown state | `data: null`, typed error | `3` |

Every failure response requires `code`, `message`, `retryable`, and `failure_class`. A JSON response is the CLI output when `--json` is selected; `4` is reserved for an explicit yield notice.

## Normative records

| Contract | Identity and required fields | States / invariants |
| --- | --- | --- |
| `common` | Namespaced principals, authenticated peer context, pipeline binding, approval selection, batch registration, typed manifests, assurance, transfer hooks, signed receipts, and key-rotation references. | Unsupported supplied security hooks deny. Absence preserves local operation. Canonical approval bytes use JCS-RFC8785, SHA-256, and `flightctl/approval/v1`. Local tokens and generation fences never become remote authority. |
| `inventory-v1` | Site/revision, controller/configuration, IANA timezone, identity mappings, hosts/devices, lanes and chat order. | Drafts and confirmed inventories may retain unreachable hosts and disabled lanes; an enabled lane is admissible only when its host is confirmed reachable. Confirmed no-GPU is count `0` with a reason. Semantic validation rejects duplicates, dangling references, bad ZoneInfo names, and incomplete confirmed configuration. |
| `rpc-envelope-v1` | Request/response envelopes, typed result union, typed failure, and persisted idempotency record. | `200/202/403/409/503` and exits are fixed above. Failure data is null and failure error is required. |
| `rpc-ops-v1` | Acquire/renew/release/claim, queue add/refresh/remove/list, book/cancel, approval request/approve/preempt, chat load/unload, cal/free/report/status. | `wait` is queue plus acquire. Release, renew, and preempt carry only the token; the server atomically looks up the generation and lane binding. Token adoption is the separate claim form carrying an authenticated token and generation; it is not a fresh acquire. Booking check-in claim is separate from token adoption. |
| `lease-v1` | Private lease has lane, generation, independent reservation, token, instance, principal/class/purpose, deadlines, booking, unit and invocation. | Lifecycle is `free → starting → running → stopping → free`; uncertainty is `quarantined`; expiry is never proof of empty hardware. Read forms have no token and require `token_redacted: true`. |
| `booking-v1` | Booking has lane (or explicit scheduler-wide null), reservation/generation record, revision, principal, purpose, UTC window, check-in, displacement and recovery record. | `scheduled → blocked → claimed` or `missed`; `completed`, `cancelled`, and `displaced` are terminal. Blocked check-in records recovery and does not move the end time. Time order is validated. |
| `queue-v1` | Queue entries have lane, independent reservation/generation record, principal/class/purpose, sequence, predecessor, boot-aware wait deadline and last-seen time. | `queued`, `eligible`, `claimed`, `expired`, `removed`; a successor with a predecessor is visible but ineligible. Refresh/expiry defaults are 60/600 seconds. |
| `occupant-v1` | Service occupants have lane, reservation/generation, token, instance, principal/class, pipeline, unit/invocation, deadline, and request accounting. | Private and redacted read forms are distinct. `active_requests` counts in-flight user requests; `completed_requests` and `last_completed_at` drive the 600-second inactivity rule; health traffic does not reset it. |
| `approval-v1` | Server-issued challenge and approval bind challenge identity/nonce, action-specific target, bounds, booking/revision, policy/manifest/payload hashes, destination/controller, canonical signed fields, expiry, proof, and verified evidence. | SSH security-key and WebAuthn encodings are separate. Touch/PIN facts come only from controller-verified evidence, never caller booleans. Consumption is atomic and one-use; replay, expiry, changed revision, target, audience, policy, manifest, or payload deny. |
| `event-v1` | Mutation events include actor/subject, site/controller, request/job/correlation IDs, lane/generation, reason, and redacted data. | Includes lease, booking, queue, approval `issued/approved/consumed/revoked`, booking `recovery`, discovery, displaced/expired, and chat loading/draining/unloaded states. Raw tokens are forbidden. |
| `pipeline-v1` | Pipeline ID/version/revision, purpose, content policy, availability, partner overrides, policy hash and update time. | Availability is `available`, `unavailable`, or `approval_required`; current purpose/content policy and current policy hash are always checked. |
| `executor-v1` | Reserve/start/beat/stop/inspect are discriminated. Start selects a typed workload after reservation acknowledgement; stop carries matching token/unit/invocation and stop authority. | Replies echo identity. Contradictory or uncertain replies are non-success and retain exclusion; protected stops require an approved forced-preemption reference; empty cgroup and GPU observations are required before release. |
| `discovery-v1` | A proposal embeds the full draft inventory/config/policy, observations, stable IDs, and a sorted semantic diff. | Controller/auth configuration is complete before a proposal is admissible. Unknown/unreachable hosts and disabled lanes are retained with admission denied; proposals never confirm, delete, stage, grant, or replace inventory. The machine-readable proposal-to-inventory projection is frozen in `discovery-v1.schema.json` and drops proposal-only review fields, host `admissible`, and lane `action`. |

## Approval binding

The signed field set is the fixed ordered list in `approval-v1.schema.json`: `id`, `action`, `requester`, `lane`, `booking_id`, `revision`, `target_generation`, `bounds`, `reason`, `nonce`, `expires`, `destination_site`, `controller_id`, `payload_hash`, `manifest_hash`, `policy_hash`, `challenge_id`, `challenge_nonce`. Build `[[field, value], ...]` in exactly that order, including explicit `null` values; serialize that array with JCS-RFC8785; encode it as UTF-8; prepend the UTF-8 bytes of `flightctl/approval/v1` followed by one zero byte; hash the resulting bytes with SHA-256; and sign the resulting digest bytes. A challenge is issued by the server in response to `approval-request`, which supplies neither challenge ID, nonce, nor expiry; the returned challenge carries all three. An approval must repeat those hashes and the challenge nonce/ID, and must match the action, requester, lane/booking/generation target, bounds, revision, destination, controller, payload and manifest. HTTPS/WebAuthn may transport the same semantics later; no federation trust or remote execution is implemented here.

## CLI mapping

The compatibility forms are lane-first and retain minute units:

```text
lanes.sh acquire <lane> "<purpose>" [ttl-min]
lanes.sh renew   <lane> <token> [ttl-min]
lanes.sh release <lane> <token>
lanes.sh wait    <lane> "<purpose>" [ttl-min] [max-wait-min]
```

The client converts TTL and wait minutes to seconds, uses the documented defaults (240-minute TTL and 10-minute bounded wait), and never accepts a positional generation. Release/renew/preempt send only the token; the controller enriches the request from its token-to-lease lookup. Adoption uses the separate authenticated `claim` form with `LANE_TOKEN` and generation validation, while a fresh `run` starts with `acquire`. `run` vectors cover both paths and release. Added commands are `cal`, `free`, `book`, `cancel`, `preempt`, `approve`, and `chat`.

## Schema use and fakes

The schemas use draft 2020-12, stable local IDs and relative `$ref`s rooted at this directory. The test validator uses an in-memory `referencing.Registry`; it never performs network resolution. Semantic checks add uniqueness, references, ZoneInfo, ordering, replay, policy, booking recovery, deterministic discovery ordering and redaction rules that JSON Schema cannot express.

The fake interfaces are Protocol-only and return typed `Mapping[str, object]` shapes. Fakes provide call logs and scripted success, denial, lost reply, delay, timeout, invocation mismatch, unknown failure, partial GPU output, multidevice output, cgroup occupancy, GPU occupancy, clock jumps and reboot. They never invoke SSH, systemd, GPUs, processes or a network.
