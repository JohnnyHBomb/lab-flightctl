"""Contract set v2: every schema is a valid 2020-12 schema with local refs, and its examples behave."""

import pytest

from .validation import V2_SCHEMA_FILES, assert_invalid, assert_valid, examples, load, validator

NAMES = [path.name for path in V2_SCHEMA_FILES]


def test_v2_set_is_complete() -> None:
    expected = {
        "common", "inventory", "policy", "quota", "accounts", "usage", "executor", "lease", "rpc-envelope", "rpc-ops",
        "event", "approval", "job", "session", "template", "endpoint", "storage", "power", "window", "notification",
        "adapters", "gpu-probe", "unit", "discovery", "legacy-compat", "snapshot", "helper-config",
    }
    assert {name.removesuffix(".schema.json") for name in NAMES} == expected


@pytest.mark.parametrize("name", NAMES)
def test_v2_schema_examples(name: str) -> None:
    schema = load(V2_SCHEMA_FILES[NAMES.index(name)])
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    validator(name)  # check_schema plus local registry
    sample = examples(name)
    assert sample["valid"], f"{name} has no valid example"
    assert sample["invalid"], f"{name} has no invalid example"
    for instance in sample["valid"]:
        assert_valid(instance, name)
    for instance in sample["invalid"]:
        assert_invalid(instance, name)


@pytest.mark.parametrize("name", NAMES)
def test_v2_refs_stay_local(name: str) -> None:
    refs: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "$ref" and isinstance(value, str):
                    refs.append(value)
                elif key != "x-examples":
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(load(V2_SCHEMA_FILES[NAMES.index(name)]))
    for ref in refs:
        assert ref.startswith(("#", "../", "common.schema.json", "https://flightctl.local/")) or ".schema.json" in ref.split("#")[0], ref
        assert "://" not in ref or ref.startswith("https://flightctl.local/"), f"{name}: non-local $ref {ref}"
