import importlib.util
import ipaddress
from pathlib import Path

from .validation import load_schema


ROOT = Path(__file__).parents[2]
SPEC = importlib.util.spec_from_file_location("portability", ROOT / "tools" / "check_portability.py")
assert SPEC and SPEC.loader
PORTABILITY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PORTABILITY)


def _address(parts: tuple[int, int, int, int]) -> str:
    return str(ipaddress.IPv4Address(bytes(parts)))


def test_portability_boundaries() -> None:
    shared_first = _address((100, 64, 0, 0))
    shared_last = _address((100, 127, 255, 255))
    shared_outside_low = _address((100, 63, 255, 255))
    shared_outside_high = _address((100, 128, 0, 0))
    private_first = _address((10, 0, 0, 0))
    private_last = _address((192, 168, 255, 255))
    outside_private = _address((9, 255, 255, 255))
    assert PORTABILITY.scan_text(f"address={shared_first} {shared_last}", "README.md")
    assert PORTABILITY.scan_text(f"address={shared_outside_low} {shared_outside_high}", "README.md") == []
    assert PORTABILITY.scan_text(f"address={private_first} {private_last}", "config/inventory.json.example") == []
    assert PORTABILITY.scan_text(f"address={private_first}", "config/other.example") == []
    assert PORTABILITY.scan_text(f"address={private_first}", "docs/sample.example")
    assert PORTABILITY.scan_text(f"address={outside_private}", "README.md") == []
    assert PORTABILITY.scan_text("host=site-a lane=lane-gpu0", "README.md") == []
    assert load_schema("common.schema.json")["$schema"].endswith("schema")


def test_portability_tree_is_clean() -> None:
    assert PORTABILITY.scan_tree(ROOT) == []
