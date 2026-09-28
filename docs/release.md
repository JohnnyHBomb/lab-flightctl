# Flightctl release rehearsal

P6 supplies a portable release boundary. The command is:

    deploy/flightctl-release stage|drain|activate|smoke|rollback RELEASE

The command is intentionally file-backed until the assembled authority,
executor, client, adapters, and watcher packages are merged. The file backend
only records mutations in a configured state root; it never contacts a host,
starts systemd, probes a GPU, or kills a process.

## Release input

'RELEASE' names a directory or a manifest file. A directory contains exactly
one of 'release.json', 'manifest.json', or 'release-manifest.json'. The
manifest uses schema version 1 and must contain:

    {
      "schema_version": 1,
      "release_id": "release-1",
      "release_version": "1.0.0",
      "protocol": {"version": "rpc-v1", "sha256": "<64 hex characters>"},
      "state_compatibility": {"version": "state-v1", "sha256": "<64 hex characters>"},
      "inventory": {
        "path": "confirmed-inventory.json",
        "revision": 7,
        "sha256": "<64 hex characters>",
        "confirmed": true
      },
      "policy": {
        "path": "confirmed-policy.json",
        "revision": 4,
        "sha256": "<64 hex characters>",
        "confirmed": true
      },
      "artifacts": [
        {
          "path": "packages/future_namespace/__init__.py",
          "version": "1.0.0",
          "sha256": "<64 hex characters>"
        }
      ]
    }

Every artifact is listed individually. Directory discovery, globs, and
implicit package inclusion are not accepted; this makes future namespace
package files explicit. The bytes at every listed path must match its
manifest hash and version. The protocol and state-compatibility descriptors
also require an explicit version and hash. Inventory and policy descriptors
must carry the literal boolean `confirmed: true`; omission or another type is
not confirmation. The frozen P0 schemas and semantic checks are applied
before any release state is changed.

Inventory and policy documents are loaded from the manifest paths, or from
'FLIGHTCTL_INVENTORY' and 'FLIGHTCTL_POLICY'. The inventory must be confirmed,
complete, revision-matched, and hash-matched. Enabled lanes may only refer to
confirmed reachable hosts with known GPU observations. Unknown or unreachable
enabled lanes refuse staging. The policy must be v1, revision-matched, and
contain admission settings.

## Smoke gate order

Smoke order is data, not a machine-name rule. Supply an external JSON file
through 'FLIGHTCTL_ROLLOUT_CONFIG', or use a manifest rollout descriptor with
'path', 'sha256', and 'gate_order'. The staged manifest freezes the ordered
gate list. Each gate must return 'ok', 'success', or 'passed'; unknown,
timed-out, lost, or any other status fails smoke and closes admission.

## State and backup roots

Set 'FLIGHTCTL_RELEASE_STATE_DIR' to a private local rehearsal root. Set
'FLIGHTCTL_BACKUP_DIR' to the operator-configured backup target for a release
run. State, staged bundles, snapshots, and backup files are written with
owner-only modes. 'restore_backup' verifies every recorded hash before copying
into a fresh target root.

Staging is atomic and immutable. Activation pauses producers, drains workloads
and chat, confirms emptiness, snapshots state and reviewed inventory/policy,
disables legacy writers, installs executors before authority/clients/adapters
and the watcher, verifies release agreement, and reopens admission last. Any
failure closes admission and prevents later unsafe steps. A live legacy
occupant is protected and quarantined; no PID claim or pattern kill is used.

Rollback requires a unique snapshot matching the running newer release,
compatible state, identical reviewed inventory and policy, and an empty
runtime after a second drain. It restores matched artifacts and state,
reconciles hardware, preserves newer runtime keys, keeps legacy writers
disabled, and reopens only after agreement verification. A changed inventory,
incompatible state, active occupant, or mixed artifact set refuses rollback
and leaves the newer release closed.

The file-backed rehearsal is not assembled acceptance. Unknown occupancy is
quarantined rather than treated as empty, and smoke refuses to reopen after a
failed drain or a missing/unknown safety gate. Real containment, identity, key
transport, GPU unloading, and deadline survival remain later deployment gates.
