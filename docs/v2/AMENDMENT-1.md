# Amendment 1 to the frozen contracts v2

- **Date:** 2 Oct 2026.
- **Basis:** the owner's decisions Q1–Q4.
- **Form:** a second commit on top of the frozen commit. The frozen commit is unchanged.
- **Review:** subject to a Sol 6 mini-review, as `FROZEN.md` requires for any contract change.

## The owner's decisions (in intent)

- **Q1, friend sessions:** answer (c) for release 1. In R1 there are no friend SSH sessions and no friend shell jobs. Friends get what agents get: API-queued jobs under a per-job DynamicUser. The infrastructure for (a) is still specified and built behind a feature flag `friend_sessions` (default off). That covers per-friend unix accounts, the C7h helper with claims and quarantine, the session contract, and C9, which is built and tested but not enabled.
- **Q2, HTTPS:** use a private CA instead of `tailscale cert`. HashiCorp Vault PKI comes later; step-ca is the likely first CA. The contract stays CA-agnostic: certificate, key and chain files, plus rotation. Remove the `tailscale cert` dependency and its certificate-transparency owner action. Add an owner action to distribute the root CA to friend devices. Peer identity stays the socket peer via `tailscale whois`. The reason for the change: names issued by `tailscale cert` appear in public certificate-transparency logs.
- **Q3, operator account:** the owner named the account. The name is site data, so it appears only in the lab site-config drafts, not in this repo. The claim-clear sudoers rule goes on that account. The install audit requires that account to have no NOPASSWD route reaching claim-clear.
- **Q4, the sshd `AuthorizedKeysFile` change:** it "has to be done very carefully to avoid lockout". Friend sessions are off in R1, so the change is deferred to C9 enablement and done through a lockout-safe runbook. Hosts differ: some run the system OpenSSH sshd; others are reached via Tailscale SSH, which ignores `authorized_keys`.

## What changed

| Area | Change |
| --- | --- |
| `adapters.schema.json` | `features.friend_sessions` is required and defaults to false; it needs the `session_gateway` port. `sessions` may be on only with it (`amendment1_adapters_semantics`). A new required `tls` block holds `mode: private-ca`, `server_name` (also the WebAuthn rp_id), the `cert_file`, `key_file` and `chain_file` paths, and `rotation` (`check_interval_s` < `alert_before_s` < `renew_before_s`). |
| `helper-config.schema.json` | `friend_sessions` is required and, per host, must equal the global flag AND that host's per-host gate; the helper also carries `friend_sessions_global`, `friend_sessions_host` and `c9_binding` (rev 7/8; `friend_sessions_consistent`). When it is false, the helper refuses the paths that create friend work (`session-open`, `unit-start --account`). Cleanup paths stay available for friend work created earlier (rev 2; `helper_subcommand_enabled`). Agent work is unaffected. |
| `inventory.schema.json` | `hosts[].ssh_server` is one of `openssh`, `tailscale-ssh`, `none` or `unknown`. C9 may be enabled only where it is `openssh` (`c9_host_eligible`). |
| SLICES A7 | The private-CA certificate replaces `tailscale cert`. Verification at start and on change uses the `tls_cert_decision` oracle. Reload keeps the listener. Clients get a `ca_file`. There are two new acceptance tests: rotation, and refusal of a bad file set. |
| SLICES C7h | The helper mirror flag is noted. The friend paths are still built and tested. |
| SLICES C9 | Its R1 status is "built and tested, not enabled". Acceptance runs against a dedicated test sshd, never the system sshd. Enablement is limited to OpenSSH hosts; a Tailscale-SSH path would need its own amendment. There is a pre-enable runbook: both key paths listed, `sshd -t`, an open second root session plus confirmed console access, a timed automatic rollback, reload rather than restart, and a test login before the rollback is cancelled. |
| SLICES C11b and C-ASM | `rp_id` is `tls.server_name`. R1 friends use API-queued jobs. C-ASM verifies the flag is off on every host. |
| OPEN-QUESTIONS | Q1 is answered. The tailnet-certificate action is replaced by private-CA actions: run the CA and its renewer, and distribute the root. Q3 is recorded. A dedicated executor account becomes an owner action. The sshd action is deferred to the runbook. |
| CONFORMANCE | The `sol6-B4` row is updated. Rows `amd1-Q1` to `amd1-Q4` are added with status `amended`. |
| Tests | `tests/contracts_v2/test_v2_amendment1.py`. |

## Revision 2 (Sol 6 mini-review, REVIEW-sol6-amd1.md: REQUEST CHANGES; amended before push)

1. **TLS trust anchor.**
   - `adapters.tls.trust_anchor` is required: `root_ca_files`, plus an optional `pinned_root_sha256`.
   - A chain rooted anywhere else is refused (`tls_cert_decision`).
   - **Incumbent expiry:** a bad new set is refused, and the incumbent is kept only while it is still valid. When the incumbent expires with no valid replacement, the listener stops serving TLS and alerts (`tls_listener_action`). An expired certificate is never served.
2. **C9 runbook: discover, don't assume.**
   - Step 3 discovers the effective `AuthorizedKeysFile`, `AuthorizedKeysCommand` and `AuthorizedKeysCommandUser` with `sshd -T`, and with `sshd -T -C user=…,host=…,addr=…` for every relevant user. It stops on per-user `Match` overrides or a `none` key file.
   - Step 4 writes every existing effective path, in order, followed by the managed directory. It then verifies the new `sshd -T` output before the reload (`sshd_effective`, `sshd_keys_plan`, `sshd_change_verified`).
   - A host using `/etc/ssh/keys/%u` keeps that path.
3. **Q1 precision.** With the flag off, only `session-open` and `unit-start --account` are refused. `session-close`, `unit-stop`, `output-collect`, `claim-release`, `claim-reconcile` and the read-only `account-check` stay available for friend work created earlier.
4. **OPEN-QUESTIONS.** The old "name the operator account" action now reads "install the rule on the decided account". The root CA files feed `trust_anchor`.
5. **Sol's carried obligations as named tests:**
   - A7: `test_trust_anchor_only_configured_root`, `test_chain_rooted_elsewhere_refused`, `test_incumbent_expiry_stops_tls_and_alerts`, and the existing rotation test.
   - C7h: `test_friend_flag_off_refuses_creation_allows_cleanup`, plus the two carried round-9 sudo cases.
   - C9: `test_authority_refuses_friend_session_on_ineligible_host` and `test_runbook_against_effective_sshd_config`.

## Revision 3 (Sol 6 mini-review round 2, REVIEW-sol6-amd1-r2.md: REQUEST CHANGES; amended before push)

1. **The incumbent is re-validated, not only date-checked.** `tls_listener_action` now takes the incumbent's `tls_cert_decision` re-run under the CURRENT `server_name`, trust-anchor roots, pins and time. This happens on every file change, every config change and every check interval.
   - Sol's case: root A is removed from `trust_anchor` while A's certificate is still date-valid. With no valid candidate the listener now stops TLS and alerts; with a valid candidate it serves the candidate.
   - Variants tested: a removed pin and a changed `server_name`.
   - A mutation check confirms that the round-2 date-only rule fails these tests.
   - New A7 live-listener test: `test_anchor_change_revalidates_incumbent`.
2. **No literal `AuthorizedKeysFile` value.** OPEN-QUESTIONS no longer gives one; it points to the C9 effective-path runbook. A test refuses any literal `AuthorizedKeysFile` value in docs, contracts or config, so the runbook's discovered-path template is the only way to form it.

## Revision 4 (Sol 6 mini-review round 3, REVIEW-sol6-amd1-r3.md: REQUEST CHANGES; amended before push)

**Defect.** C9 runbook step 7 needed a managed-key friend login before step 8 enables `friend_sessions`. With the flag off, the helper refuses `session-open`, which is the only path that writes keys.

**Fix: a bounded session probe**, chosen over a per-host enable window because it never turns any friend feature on. It follows the claim-clear pattern:
- a separate root-only program, `/usr/local/libexec/flightctl-session-probe`;
- reached only through the operator's own re-authenticating sudo rule (`config/flightctl-session-probe-sudoers.example`), with SUDO_USER/SUDO_UID checked;
- one key, for helper.json `probe_account` only (`fc-probe[-x]`, which must not be a friend, the operator or the executor);
- a TTL of at most 900 s, carried in the key itself as `expiry-time`;
- `restrict` plus a forced `probe-ok` command;
- the key is removed on expiry, on revoke, and by the runbook's rollback timer.

The probe is refused while `friend_sessions` is on, for any other account, for any caller other than the operator, and while an unexpired probe key exists. Every call is an audit event. The executor and friends must have no sudo route to it.

**Runbook.** Steps 5, 7 and 8 now use the probe: the rollback removes the probe key, step 7 logs in with the probe and then revokes it, and step 8 records the audit events and confirms the key is gone.

**New acceptance tests:**
- C7h: `test_session_probe_bounds`.
- C9: `test_authority_refuses_session_open_when_flag_off` and `test_runbook_probe_login_with_flag_off`.

**Reference oracles:** `session_probe` and `probe_remove` (`probe_remove` was replaced by the root-only `probe_sweep` in revision 5).

## Revision 5 (Sol 6 mini-review round 4, REVIEW-sol6-amd1-r4.md: two probe defects, both confirmed; amended before push)

1. **`expiry-time` time zone.** The local sshd(8) man page says an unsuffixed value is read in the system time zone, with format `YYYYMMDDHHMM[SS][Z]`.
   - The probe now writes UTC with a `Z` suffix.
   - Tests parse the written key line and evaluate it as sshd would (`sshd_expiry_effective`) under UTC, America/New_York, Pacific/Chatham, Asia/Kolkata and Pacific/Kiritimati. The effective expiry is always at most issue + 900 s.
   - A mutation that drops the `Z` is caught.
2. **Rollback revocation path.** A root timer has no sudo context, so it could not pass the operator check on the add path. Removal is now `flightctl-session-probe --sweep --expired|--all` (`probe_sweep`):
   - root-only (real and effective UID 0) and remove-only, so it can never add or extend a key;
   - limited to the `probe_account` key: another account is refused, and a non-probe line at that path is left in place;
   - idempotent and audited.
   - Units: `flightctl-session-probe-sweep.service` and `.timer` (every minute, `--expired`). The rollback unit and the operator's step-7 revoke use `--all`.
   - The add path keeps the operator-only check and is refused from a root unit.
   - Runbook steps 5 and 7 use the sweep.
   - Mutation checks: an add path that accepts a root context, and a sweep that ignores the account, are each caught.

## Revision 6 (Sol 6 mini-review round 5, REVIEW-sol6-amd1-r5.md)

Every `--sweep` outcome is now an audit event: `probe-swept-<mode>`, `sweep-none-present`, `sweep-skipped-unexpired`, `sweep-refused-non-probe` and `sweep-refused`. No sweep returns silently, which matches C7h's rule that every session-probe call is audited. A mutation that makes a no-op silent is caught.

## Revision 7 (Sol 6 mini-review round 6, REVIEW-sol6-amd1-r6.md)

1. **Per-host enablement.**
   - Inventory hosts gain `friend_sessions_enabled` (default false) and `c9_proof`: proof id, artefact SHA-256, date, runbook revision, and three outcomes that must all be true (sshd verified, probe login ok, probe key removed).
   - The schema allows `friend_sessions_enabled: true` only on an `openssh` host with a proof.
   - The authority (`authority_admits_friend_work`) and each host's helper (`friend_sessions` = global AND per-host, `helper_flag_expected`) both require the global flag AND the host's own flag.
   - Runbook step 8 sets the per-host flag only. The global flag never enables a host by itself.
   - Tested with Sol's two-host case. A global-only gate is caught by a mutation check.
2. **A-ASM.** The "enable tailnet HTTPS certificates" step and its owner action now name the private-CA prerequisite (certificate, key, chain and trust anchor in place). A doc-wide test forbids `tailscale cert` and tailnet HTTPS instructions in active packet text. They are allowed only in the historical FROZEN record, in the change log, or as the certificate-transparency rationale.
3. **Self-check of the amendment diff.** I searched it for other global-versus-per-host and stale-instruction patterns:
   - C9 R1-status and HOSTS text, the C7h helper note, C-ASM's check, README item 66, ADAPTERS.md, OPEN-QUESTIONS Q1 and both schema descriptions all now state the per-host rule.
   - I found no other active instruction to enable tailnet certificates, and no remaining `--revoke` or `probe_remove` instruction.
   - TLS is controller-only. The probe account and sudo rule are already scoped to "hosts being enabled". The sshd change is already per host.

## Revision 8 (Sol 6 mini-review round 7, REVIEW-sol6-amd1-r7.md)

1. **The helper sees both gates.** helper.json carries `friend_sessions_global`, `friend_sessions_host`, their conjunction `friend_sessions`, and `c9_binding`.
   - Friend creation is refused unless all three flags are true and the live host facts match the binding. Either flag false means off.
   - Sol's case (helper flag true, global flag false) is refused at the helper. All flag combinations are tested.
   - `friend_sessions_consistent` (per host) must pass before activation.
2. **The proof is bound to its host.** `c9_proof` binds `host_id`, the sshd host-key SHA-256, the machine-id SHA-256 and the effective `sshd -T` SHA-256. Inventory records the observed values.
   - Admission requires the binding to equal the observed values, and the artefact to be present in the authority's store with the same hash and host.
   - Sol's copy case (host B with host A's proof) is refused, including when the host id is edited.
3. **Per-host consistency replaces global equality.** The old global-equality rule is replaced by the per-host `friend_sessions_consistent`. A mixed deployment where only proven hosts are on is valid.
4. **Adversarial self-pass over the C9 enablement path** (kept as tests):
   - all 8 helper flag combinations, plus a binding mismatch;
   - authority gates in order (global, openssh, host flag, proof shape, host id, host key, machine-id, sshd -T, artefact, future date);
   - proof reuse across hosts;
   - stale proof after an sshd config change, which I decided invalidates the proof through the `sshd -T` hash;
   - a host re-image (machine-id and host key change);
   - a clock rollback. Proofs dated more than 300 s in the future are refused. Found and fixed: a wall-clock rollback could extend a probe key, so the probe now also stores a monotonic deadline and boot id, and the sweep removes the key when either clock says it expired, or after a reboot.

## Revision 9 (Sol 6 mini-review round 8, REVIEW-sol6-amd1-r8.md)

All three of Sol's counterexamples were reproduced on 7a3a3f3 before fixing (measured, scratch `repro_r9.py`):
- the authority refused, but a helper holding a stale global copy still accepted session-open after the site flag was turned off;
- the binding had no per-user sshd component, so a `Match User` change for a friend account went undetected;
- with the monotonic value omitted, the sweep fell back to wall time and kept the key after a rollback.

1. **Fail-closed disable** (`friend_sessions_disable`, `authority_admits_any_work`).
   - Friend work is refused at once.
   - Every helper copy is written false and its refusal is read back before the site flag is reported off.
   - An unreachable host stays disable-pending and receives no work.
   - Chosen over a signed enable token: it adds no key material or crypto dependency to the root helper, and no cross-host clock dependence.
2. **User-specific binding.**
   - `sshd_effective_sha256` is now `sshd_effective_digest`: the global `sshd -T` plus `sshd -T -C user=…` for the probe account and every friend account.
   - `sshd_host_key_sha256` covers ALL host keys (`sshd_host_keys_sha256`).
   - Check and creation are one helper operation with a re-measure at the commit point (`helper_create_friend_work`).
   - A changed bound setting, a rotated or added key, a re-image or a new friend account requires the full C9 runbook again.
3. **Monotonic source required.** `mono_now` and `boot_id` are required arguments of `session_probe` and `probe_sweep`, with no wall-clock fallback. A record without them is swept as expired.
4. **Security review before enablement.** C9 now says that enabling friend sessions on any host requires a dedicated security review of the then-current C9 implementation before activation. R1 ships with every friend flag off.

**Not closed (stated residuals):**
- (a) During a disable, a host that cannot be reached keeps its stale helper copy until it is reached. It is exploitable only by a direct local helper call from the executor account on that host, which the authority no longer drives.
- (b) `sshd -T` reads the configuration files. A running daemon whose loaded configuration differs from its files (changed, reloaded, then reverted without a reload) is not detected by the binding.
- (c) A configuration change made after a creation's commit point affects only later creations; the friend's existing session keeps whatever sshd allowed at login.
- (d) All of this is reference-oracle and contract text. The real helper, authority and runbook tooling are unbuilt, so the on-lab tests and the new pre-enablement security review carry the proof.

## Revision 10 (Sol 6 mini-review round 9, REVIEW-sol6-amd1-r9.md)

**Reproduced first.** In the oracle, the bound digest was unchanged by a `Match Address` change for another client (`repro_r10.py`). On the real OpenSSH 10.5p1 `sshd -T -C`, with a scratch config, a throwaway host key and no daemon (measured): the sampled address 192.0.2.5 kept `.ssh/authorized_keys /etc/ssh/flightctl-keys/%u`, while address 192.0.2.9 got `/etc/ssh/other-keys/%u` through an Include-hidden `Match Address`. The global output was unchanged. (The measured run used tailnet-range addresses; RFC 5737 documentation addresses are shown here for portability.)

**Fix: refuse, not enumerate.** Refusing is smaller and sound. Every Match criterion (User, Group, Host, LocalAddress, LocalPort, Version, RDomain, Address) can change key selection, and the set of permitted client contexts cannot be enumerated reliably.
- `sshd_context_problems` walks the configuration from `/etc/ssh/sshd_config`, expanding `Include` (which sshd_config(5) also permits inside a Match).
- Context is sticky and conservative: from the first `Match` line in expansion order, including `Match all`, every line is conditional. A file included from a conditional context is conditional throughout.
- Any directive from `SSHD_KEY_RELEVANT` in a conditional context makes the host ineligible. So does an unresolvable literal `Include`.
- The binding digest now also covers the Include-expanded file set (paths and SHA-256 of contents), so any configuration file change invalidates the proof and the helper binding.
- `c9_proof` gains the required outcome `no_conditional_key_settings: true`.

**The list, against sshd_config(5)'s Match-permitted keywords** (local man page, OpenSSH 10.5p1):
- **Key sources:** `AuthorizedKeysFile`, `AuthorizedKeysCommand`, `AuthorizedKeysCommandUser`.
- **Principal/CA authorisation:** `TrustedUserCAKeys`, `AuthorizedPrincipalsFile`, `AuthorizedPrincipalsCommand`, `AuthorizedPrincipalsCommandUser`.
- **Key acceptance:** `PubkeyAuthentication`, `RevokedKeys`.
- **Ways in that bypass the managed key path:** `AuthenticationMethods`, `PasswordAuthentication`, `KbdInteractiveAuthentication`, `PermitEmptyPasswords`, `PAMServiceName` (selects the PAM stack used by keyboard-interactive), `HostbasedAuthentication`, `HostbasedUsesNameFromPacketOnly`, `IgnoreRhosts`, `GSSAPIAuthentication`, `KerberosAuthentication`.
- **Not listed** (they only restrict, or do not select a credential): AllowUsers/DenyUsers/AllowGroups/DenyGroups, RefuseConnection, MaxAuthTries, PubkeyAcceptedAlgorithms, PubkeyAuthOptions, CASignatureAlgorithms, HostbasedAcceptedAlgorithms, ForceCommand, ChrootDirectory, PermitRootLogin (root is never a friend), ExposeAuthInfo, and the forwarding, session and timeout keywords.

**Also found by measurement and fixed:** OpenSSH 10.5p1 prints `sshd -T` keywords in mixed case (`AuthorizedKeysFile`). The revision-2 `sshd_effective` parser matched lower case only, so on that release it would have found no key settings and stopped every runbook. It now compares keywords case-insensitively, and a test uses the measured line.

**Residuals Sol accepted, now named obligations of the C9 pre-enablement security review:** `review-obligation-stale-copy`, `review-obligation-loaded-vs-file` and `review-obligation-post-commit` (SLICES C9 and CONFORMANCE).

## Revision 11 (Sol 6 mini-review round 10, REVIEW-sol6-amd1-r10.md)

1. **Keyword aliases.** Reproduced first: `Match Address 192.0.2.9` with `ChallengeResponseAuthentication yes` returned no problems.
   - **Sweep evidence** (measured on OpenSSH 10.5p1; scratch `alias_sweep.py`, output `alias-sweep-20261002.json`):
     - every keyword-shaped string in the sshd binary was set to a non-default value under `sshd -T -f <scratch>`, and the changed canonical keyword recorded; 105 keywords were accepted;
     - `ChallengeResponseAuthentication` and `SkeyAuthentication` map to `KbdInteractiveAuthentication`, both accepted inside Match and effective there. `SkeyAuthentication` is not in the man page.
     - `DSAAuthentication` maps to `PubkeyAuthentication` and is rejected inside Match by this release. `HostDSAKey` maps to `HostKey`.
     - `PubkeyAcceptedKeyTypes` and `HostbasedAcceptedKeyTypes` are recognised, but no tested value was accepted; mapped by name (inferred).
     - `AuthorizedKeysFile2` is deprecated and ignored on 10.5p1, but mapped conservatively to `AuthorizedKeysFile` because older releases honoured it.
     - The local sshd_config(5) documents only `ChallengeResponseAuthentication`. No OpenSSH source is installed here.
   - **The fix:**
     - aliases are canonicalised before the Match check (`SSHD_KEYWORD_ALIASES`);
     - any keyword in a Match context that is neither Match-permitted (`SSHD_MATCH_KEYWORDS`, from the local man page) nor a known alias is refused, so aliases from other OpenSSH releases fail closed.
2. **Runbook sync.**
   - Step 3 now names the probe account, the Include-expanded file set and all host keys.
   - Step 8 names the digest functions, their inputs, every `c9_proof` field and all four outcomes, including `no_conditional_key_settings`.
   - `test_r11_runbook_matches_the_proof_schema` fails if step 8 or the oracle's outcome list drifts from the `c9_proof` schema.

## Blast radius

- **Site configuration files:** every `adapters.json` must now carry `features.friend_sessions` and `tls`, and every `helper.json` must carry `friend_sessions`. Files without them fail schema validation, which is the fail-closed outcome. The repo examples are updated. The lab drafts are updated on the lab side.
- **Packets:**
  - A7 changes its certificate source and gains two acceptance tests.
  - C9 keeps its scope but is not enabled in R1, and its real-SSH tests run on a test sshd.
  - C-ASM drops the friend-session scenario from R1.
  - No packet is removed, so no BLAST-RADIUS entry is needed.
- **Unchanged:** v1 contracts and the legacy `lanes.sh` path.
- **Conflict found:** the operator account must differ from the executor account (`helper_config_semantics`). The executor's only sudo grant must also be the helper (`sudoers_audit`). Today's lab drafts point the executor at the owner's own account. A dedicated executor account is therefore listed as an owner action before C7h's install. I did not resolve this by relaxing either rule.
- **Observed evidence for Q3:** on the controller only, read-only. I ran the `sudo -l` capture taken earlier today through the operator audit:
  - the account's route to claim-clear requires a password;
  - none of its four NOPASSWD grants reaches claim-clear;
  - the only failure is the missing `timestamp_timeout=0`, which is expected because the rule is not installed.

  Lane hosts were not captured, and `exempt_group` is a carried C7h check.
