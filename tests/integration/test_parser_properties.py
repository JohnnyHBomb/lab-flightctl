"""Bounded parser/property checks for the weekly security job."""

from __future__ import annotations

import json
import random
import copy
from pathlib import Path

import pytest
from jsonschema.exceptions import ValidationError

from deploy.check_portability import _load_denylist
from deploy.flightctl_release import ReleaseFailure, _validate_frozen_schema
from tests.contracts.validation import load_schema, validator

from .support import confirmed_inventory, digest, json_bytes, make_release, run_tool


def test_bounded_parser_properties() -> None:
    generator = random.Random(20260928)
    for _ in range(256):
        candidate = {
            "schema": generator.choice([1, 2, None, "1"]),
            "request_id": generator.choice(["req", "", None, 7]),
            "op": generator.choice(["status", "acquire", "unknown", None]),
            "hosts": [generator.choice([{}, [], None, "host"]) for _ in range(generator.randrange(3))],
            "lanes": [generator.choice([{}, [], None, "lane"]) for _ in range(generator.randrange(3))],
        }
        raw = json.dumps(candidate, separators=(",", ":")).encode("utf-8")
        parsed = json.loads(raw)

        # Exercise the product's frozen-document parser, not only the local
        # generator helpers. Invalid candidates must be rejected without
        # escaping the bounded loop.
        for kind, document in (
            ("inventory", parsed),
            ("policy", parsed),
        ):
            try:
                _validate_frozen_schema(document, kind)
            except ReleaseFailure:
                continue
            raise AssertionError(f"{kind} parser accepted a generated invalid candidate")


@pytest.mark.parametrize("schema_name,version_field", [
    ("rpc-envelope-v1.schema.json", "schema"),
    ("discovery-v1.schema.json", "schema_version"),
])
def test_bounded_rpc_and_discovery_schemas(schema_name, version_field) -> None:
    """Exercise the actual frozen schemas, including a valid control."""
    generator = random.Random(20260928)
    check = validator(schema_name)
    examples = load_schema(schema_name)["x-examples"]["valid"]
    for example in examples:
        check.validate(example)
    # The RPC schema also contains a valid stored idempotency record, which
    # has no wire-envelope version field.
    versioned = [example for example in examples if version_field in example]
    assert versioned
    for index in range(128):
        candidate = copy.deepcopy(generator.choice(versioned))
        if index % 2:
            candidate.pop(version_field)
        else:
            candidate[version_field] = generator.choice([None, "1", 2, -1, [], {}])
        with pytest.raises(ValidationError):
            check.validate(json.loads(json.dumps(candidate)))


def test_denylist_parser_is_bounded_and_fail_closed(tmp_path) -> None:
    generator = random.Random(20260928)
    for index in range(64):
        path = tmp_path / f"denylist-{index}.txt"
        if index % 3 == 0:
            path.write_bytes(b"\n# comment\n")
            expected_failure = True
        else:
            path.write_bytes((f"entry-{generator.randrange(1000)}\n").encode("ascii"))
            expected_failure = False
        try:
            entries = _load_denylist(path)
        except ValueError:
            assert expected_failure
        else:
            assert not expected_failure and entries


def test_product_inventory_parser_properties(tmp_path: Path) -> None:
    """Exercise stage's parser and semantic gate with bounded invalid documents."""

    mutations = (
        lambda document: document["controller"].pop("controller_id"),
        lambda document: document.pop("identity_mapping"),
        lambda document: document.__setitem__("unexpected", True),
        lambda document: document["controller"].__setitem__("auth_state", "unknown"),
        lambda document: document["hosts"][0].__setitem__("gpu_count", None),
        lambda document: document["lanes"][0].__setitem__("host_id", "missing-host"),
    )
    for index, mutate in enumerate(mutations):
        bundle = make_release(tmp_path / "releases", f"parser-{index}")
        document = confirmed_inventory()
        mutate(document)
        inventory_path = bundle / "inventory.json"
        inventory_path.write_bytes(json_bytes(document))
        manifest_path = bundle / "release.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["inventory"]["revision"] = document["revision"]
        manifest["inventory"]["sha256"] = digest(inventory_path.read_bytes())
        manifest_path.write_bytes(json_bytes(manifest))
        result = run_tool(tmp_path / f"state-{index}", "stage", str(bundle))
        assert result.returncode != 0
