import pytest


def pytest_configure(config: pytest.Config) -> None:
    # harness markers are also used by tests/contracts_v2/test_v2_harness.py when tests/conformance is not collected
    for line in (
        "port(name): the port this parametrised case exercises (evidence attribution)",
        "strict_missing(port): strict run without a real twin; fails at setup",
    ):
        config.addinivalue_line("markers", line)
