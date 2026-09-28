from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"
CONFIG = ROOT / "config"
VECTORS = Path(__file__).resolve().parent / "contracts" / "vectors"


@pytest.fixture
def repo_root() -> Path:
    return ROOT


@pytest.fixture
def contracts_dir() -> Path:
    return CONTRACTS
