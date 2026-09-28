import copy

from .validation import SCHEMA_FILES, examples, roundtrip


def test_roundtrip_records() -> None:
    for path in SCHEMA_FILES:
        for instance in examples(path.name)["valid"]:
            assert roundtrip(copy.deepcopy(instance), path.name) == instance
