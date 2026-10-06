# lab-flightctl contracts

This repository freezes portable Flightctl v1 contracts for parallel controller work. It intentionally contains schemas, typed dependency-injection protocols, side-effect-free fakes, examples, vectors, and checks; it does not contain a controller runtime, deployment, federation, credentials, real host calls, or GPU work.

Run the acceptance checks with Python 3.12 and the declared test dependencies:

```sh
python -m pytest -q
python tools/check_portability.py .
```

Contract set v2 (frozen for review, not yet implemented) is in [contracts/v2/README.md](contracts/v2/README.md); its slice plan, proof gates and traceability are in [docs/v2/](docs/v2/SLICES.md). Status: library, not deployed.

The v1 contract index is in [contracts/README.md](contracts/README.md). JSON Schema documents are in `contracts/`; neutral templates are in `config/`; fake implementations and fixtures are in `tests/fakes/`; executable contract vectors are in `tests/contracts/vectors/`. `ROSTER-REPORT.md` records the local verification evidence and any environment blockers.

## Licence

Licensed under either of

- Apache License, Version 2.0 ([LICENSE-APACHE](LICENSE-APACHE) or <https://www.apache.org/licenses/LICENSE-2.0>)
- MIT licence ([LICENSE-MIT](LICENSE-MIT) or <https://opensource.org/licenses/MIT>)

at your option (SPDX: `MIT OR Apache-2.0`).

Unless you explicitly state otherwise, any contribution intentionally submitted for inclusion in this work by you, as defined in the Apache-2.0 licence, shall be dual licensed as above, without any additional terms or conditions.
