import os
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def executable_shims_use_test_python(monkeypatch: pytest.MonkeyPatch) -> None:
    # Executable fakes need the same declared schema dependency as pytest.
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", ""))
