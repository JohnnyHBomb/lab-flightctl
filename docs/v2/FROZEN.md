# Contracts v2: frozen

> **Amended:** `AMENDMENT-1.md` (the owner's decisions of 2 Oct 2026, in the next commit) changes Q1, the A7 certificate source and the sshd timing. It is subject to a Sol 6 mini-review. The owner-action list below is the one at freeze time; the current list is in `OPEN-QUESTIONS.txt`.

The v2 contract set is **frozen at this commit**: the single commit on top of `59bdd7f` on branch `contracts-v2` that adds this file. The freeze follows 9 Sol 6 review rounds.

**Final verdict (Sol 6, round 9): APPROVE WITH CHANGES.** The round-8 contract gaps are closed. Two install-audit counterexamples remain. They need no contract change, so they are carried to packet C7h as acceptance obligations.

**Any contract change after this commit needs a new review.** That covers anything under `contracts/v2/`, the rules in `docs/v2/SLICES.md` and `docs/v2/GATES.txt`, and the reference oracles in `tests/contracts_v2/validation.py`. Packets implement against this text. If a packet finds that the contract must change, it stops and asks for a review first; it does not edit the contract.

## Carried obligations (C7h)

Reference cases are strict-xfail tests in `tests/contracts_v2/test_v2_freeze.py`. They pass, and must then be un-marked, only when the C7h audit is fixed.

1. **`test_install_audit`**
   - It must reject an effective `exempt_group` that contains the operator account.
   - It must parse and check the authentication (PASSWD/NOPASSWD) and SETENV tags of **each** command in a list. That includes tags inherited by, or changed after, a comma, in both the effective (`sudo -l`) and the static (installed sudoers) audit.
   - The wildcard, alias and group cases stay in this test.
2. **`test_claim_clear_prompts_every_time`** (on-lab, owner-installed). On the owner-installed sudoers rule, an immediate second invocation must prompt after a successful first one. The `timestamp_timeout=0` behaviour is documented (sudoers(5), sudo 1.9.17p2), not yet measured.
3. **Traceability.** CONFORMANCE row `sol6r8-B5` is `specified`, and the two round-9 rows are `carried`. Their status is part of C7h's review. They become `contract-fixed` only when the cases above pass.

## Remaining owner actions (John)

- **Q1:** choose how friends' jobs and SSH sessions are isolated (`docs/v2/OPEN-QUESTIONS.txt`), before any friend SSH work.
- **Tailnet HTTPS:** enable HTTPS certificates for the tailnet and let the controller run `tailscale cert` (A7). Issued certificate names appear in public certificate-transparency logs.
- **claim-clear:** name the operator account (`helper.json` `operator_account`). Install its claim-clear sudoers rule without NOPASSWD and with `Defaults!<claim-clear path> timestamp_timeout=0` (`config/flightctl-claim-clear-sudoers.example`). Then enable the on-lab fresh-prompt proof.
- **sshd:** change `AuthorizedKeysFile` to add the root-owned managed keys directory, before C9.
- **Helper:** install flightctl-helper, its sudoers rule and its persistent claims directory on the lane hosts (C7h).
- **Alerts:** provide the SMTP relay credentials and a Slack incoming webhook for the site secrets file (C10).
- **Approver keys:** enrol the FIDO2 key(s) on the approver (C3).
