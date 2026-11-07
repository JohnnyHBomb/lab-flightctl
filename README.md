# lab-flightctl contracts

This repository freezes portable Flightctl v1 contracts for parallel controller work. It intentionally contains schemas, typed dependency-injection protocols, side-effect-free fakes, examples, vectors, and checks; it does not contain a controller runtime, deployment, federation, credentials, real host calls, or GPU work. The v2 work below adds controller code as a library; nothing is deployed yet.

Run the acceptance checks with Python 3.12 and the declared test dependencies:

```sh
python -m pytest -q
python tools/check_portability.py .
```

Contract set v2 (being implemented; see Status below) is in [contracts/v2/README.md](contracts/v2/README.md); its slice plan, proof gates and traceability are in [docs/v2/](docs/v2/SLICES.md).

## Status (7 Nov 2026)

- **Merged to main:** milestone A packets A0a, A0b, A0c, A1, A2, A3, A3i, A3b, A3c, A4, A4u, A5a, A5a2, A5a2b, A5a3 (part 1), A5b1 (PR #49, `e836642`), A6, A6b, A11 and A11b. The suite at #49 is 1421 passed, 54 skipped, 4 xfailed.
- **Not live:** nothing from v2 runs on a lab host yet, there is no v2 release and no tag. The lab still allocates GPUs with the `lanes.sh` script. Library, not deployed.
- **Next milestone:** the rest of milestone A up to the A-ASM assembly (A5b2 first). Milestones B and C stay closed until the pilot is live.
- **Restart decisions (7 Nov 2026):** the pilot runs on one designated lab host; lane T is a single card; a golden capture is taken now for the remaining desktop card type; this repository stays public, and on-lab proofs run through the local gauge only, never a self-hosted runner; packet races use two arms for M packets and one arm plus a blind review for S packets; scope is milestone A through A-ASM only.

The v1 contract index is in [contracts/README.md](contracts/README.md). JSON Schema documents are in `contracts/`; neutral templates are in `config/`; fake implementations and fixtures are in `tests/fakes/`; executable contract vectors are in `tests/contracts/vectors/`. `ROSTER-REPORT.md` records the local verification evidence and any environment blockers.

## Licence

Licensed under either of

- Apache License, Version 2.0 ([LICENSE-APACHE](LICENSE-APACHE) or <https://www.apache.org/licenses/LICENSE-2.0>)
- MIT licence ([LICENSE-MIT](LICENSE-MIT) or <https://opensource.org/licenses/MIT>)

at your option (SPDX: `MIT OR Apache-2.0`).

Unless you explicitly state otherwise, any contribution intentionally submitted for inclusion in this work by you, as defined in the Apache-2.0 licence, shall be dual licensed as above, without any additional terms or conditions.
