"""Schema loading plus semantic checks that JSON Schema cannot express."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    import jsonschema
    from jsonschema import FormatChecker
    from referencing import Registry, Resource
except ModuleNotFoundError:  # pragma: no cover - recorded as an environment blocker
    jsonschema = None  # type: ignore[assignment]
    FormatChecker = None  # type: ignore[assignment,misc]
    Registry = None  # type: ignore[assignment,misc]
    Resource = None  # type: ignore[assignment,misc]


ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "contracts"
SCHEMA_FILES = tuple(sorted(SCHEMA_DIR.glob("*.schema.json")))


class ContractError(ValueError):
    """A schema or semantic contract violation."""


def require_jsonschema() -> Any:
    if jsonschema is None:
        raise RuntimeError("jsonschema is required for contract validation; install the declared test dependency")
    return jsonschema


def _schema_path(name: str | Path) -> Path:
    candidate = Path(name)
    if candidate.exists():
        return candidate
    if candidate.suffix != ".json":
        candidate = Path(f"{name}.schema.json")
    path = SCHEMA_DIR / candidate.name
    if not path.exists():
        raise FileNotFoundError(path)
    return path


def load_schema(name: str | Path) -> dict[str, Any]:
    with _schema_path(name).open(encoding="utf-8") as handle:
        return json.load(handle)


def _registry() -> Any:
    if Registry is None or Resource is None:
        raise RuntimeError("referencing is required for local contract references")
    resources: dict[str, Any] = {}
    for path in SCHEMA_FILES:
        schema = load_schema(path)
        resource = Resource.from_contents(schema)
        resources[schema["$id"]] = resource
        resources[f"https://flightctl.local/contracts/{path.name}"] = resource
    return Registry().with_resources(resources.items())


def validator(name: str | Path) -> Any:
    package = require_jsonschema()
    schema = load_schema(name)
    checker = FormatChecker()
    cls = package.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema, registry=_registry(), format_checker=checker)


def validate_definition(instance: Any, name: str | Path, definition: str) -> None:
    """Validate an instance against a named ``$defs`` entry in a real schema."""
    package = require_jsonschema()
    schema = load_schema(name)
    wrapper = {
        "$schema": schema["$schema"],
        "$id": "https://flightctl.local/contracts/inline-validation.schema.json",
        "$ref": f"{Path(name).name}#/$defs/{definition}",
    }
    cls = package.validators.validator_for(wrapper)
    cls.check_schema(wrapper)
    errors = sorted(cls(wrapper, registry=_registry(), format_checker=FormatChecker()).iter_errors(instance), key=lambda error: list(error.path))
    if errors:
        first = errors[0]
        path = ".".join(str(part) for part in first.path) or "$"
        raise ContractError(f"{name}#{definition}: {path}: {first.message}")


def validate_instance(instance: Any, name: str | Path) -> None:
    errors = sorted(validator(name).iter_errors(instance), key=lambda error: list(error.path))
    if errors:
        first = errors[0]
        path = ".".join(str(part) for part in first.path) or "$"
        raise ContractError(f"{name}: {path}: {first.message}")


def assert_valid(instance: Any, name: str | Path) -> Any:
    validate_instance(instance, name)
    return instance


def assert_invalid(instance: Any, name: str | Path) -> None:
    try:
        validate_instance(instance, name)
    except ContractError:
        return
    raise AssertionError(f"expected {name} instance to be invalid")


def examples(name: str | Path) -> dict[str, list[Any]]:
    return load_schema(name).get("x-examples", {"valid": [], "invalid": []})


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError(f"invalid UTC time: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ContractError(f"time is not UTC: {value!r}")
    return parsed.astimezone(timezone.utc)


def _unique(items: Iterable[Mapping[str, Any]], key: str, label: str) -> list[str]:
    seen: set[Any] = set()
    errors: list[str] = []
    for item in items:
        value = item.get(key)
        if value in seen:
            errors.append(f"duplicate {label}: {value}")
        seen.add(value)
    return errors


def inventory_semantics(inventory: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    try:
        ZoneInfo(str(inventory.get("timezone")))
    except (ZoneInfoNotFoundError, TypeError):
        errors.append(f"unknown IANA timezone: {inventory.get('timezone')}")
    hosts = list(inventory.get("hosts", []))
    lanes = list(inventory.get("lanes", []))
    errors.extend(_unique(hosts, "host_id", "host_id"))
    errors.extend(_unique(lanes, "lane_id", "lane_id"))
    errors.extend(_unique(inventory.get("identity_mapping", []), "external_id", "external_id"))
    host_map = {host.get("host_id"): host for host in hosts}
    device_map: dict[str, Mapping[str, Any]] = {}
    for host in hosts:
        devices = host.get("devices", [])
        errors.extend(_unique(devices, "device_id", f"device_id on {host.get('host_id')}"))
        for device in devices:
            device_id = device.get("device_id")
            if device_id in device_map:
                errors.append(f"duplicate device_id: {device_id}")
            device_map[device_id] = device
            for field in ("vendor", "model", "vram_bytes", "driver"):
                if device.get(field) is None and field not in device.get("unknown_reasons", {}):
                    errors.append(f"unknown device measurement lacks reason: {device_id}.{field}")
                if device.get(field) is not None and field in device.get("unknown_reasons", {}):
                    errors.append(f"known device measurement has unknown reason: {device_id}.{field}")
        count = host.get("gpu_count")
        if count is None and not host.get("gpu_count_reason"):
            errors.append(f"unknown gpu_count lacks reason: {host.get('host_id')}")
        if count == 0 and (host.get("devices") or "no gpu" not in str(host.get("gpu_count_reason", "")).lower()):
            errors.append(f"confirmed no-GPU host is inconsistent: {host.get('host_id')}")
        if isinstance(count, int) and count > 0 and count != len(devices):
            errors.append(f"gpu_count does not match devices: {host.get('host_id')}")
    for lane in lanes:
        host_id = lane.get("host_id")
        if host_id not in host_map:
            errors.append(f"dangling lane host_id: {host_id}")
        elif lane.get("enabled") and host_map[host_id].get("reachability") != "confirmed":
            errors.append(f"enabled lane uses unavailable host: {host_id}")
        for device_id in lane.get("device_ids", []):
            if device_id not in device_map:
                errors.append(f"dangling lane device_id: {device_id}")
            elif host_id in host_map and device_id not in {d.get("device_id") for d in host_map[host_id].get("devices", [])}:
                errors.append(f"lane device belongs to another host: {device_id}")
    lane_ids = {lane.get("lane_id") for lane in lanes}
    for lane_id in inventory.get("chat_lane_order", []):
        if lane_id not in lane_ids:
            errors.append(f"dangling chat lane: {lane_id}")
    for mapping in inventory.get("identity_mapping", []):
        principal = mapping.get("principal", {})
        if principal.get("site_id") != inventory.get("site_id"):
            errors.append(f"identity mapping crosses site: {mapping.get('external_id')}")
    if inventory.get("stage") == "confirmed":
        controller = inventory.get("controller", {})
        if not controller.get("endpoint") or not controller.get("account") or controller.get("auth_state") != "configured":
            errors.append("confirmed inventory has unresolved controller")
        for host in hosts:
            if host.get("reachability") == "confirmed" and host.get("observed_at") is None:
                errors.append(f"confirmed inventory host lacks observation: {host.get('host_id')}")
            for device in host.get("devices", []):
                for field in ("vendor", "model", "vram_bytes", "driver"):
                    if device.get(field) is None:
                        errors.append(f"confirmed inventory has unknown device: {device.get('device_id')}.{field}")
    return errors


def validate_inventory(inventory: Mapping[str, Any]) -> None:
    validate_instance(inventory, "inventory-v1.schema.json")
    errors = inventory_semantics(inventory)
    if errors:
        raise ContractError("inventory semantics: " + "; ".join(errors))


def rpc_semantics(operation: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    op = operation.get("op")
    args = operation.get("args", {})
    if op == "acquire" and args.get("est_s", 0) > args.get("max_s", 0):
        errors.append("acquire estimate exceeds max")
    if op in {"renew", "release"} and operation.get("lane") is None:
        errors.append(f"{op} requires a lane")
    if op == "chat-unload" and operation.get("lane") is None:
        errors.append("chat-unload requires a lane")
    admission = operation.get("admission", {})
    approval = admission.get("approval", {})
    if op in {"preempt", "approve"} and not approval.get("required"):
        errors.append(f"{op} requires atomic approval selection")
    if op == "preempt" and approval.get("approval_id") != args.get("approval_id"):
        errors.append("preempt approval selection does not match request")
    if op == "acquire" and args.get("pipeline_ref") is not None:
        pipeline = admission.get("pipeline")
        if not pipeline or pipeline.get("pipeline_id") != args.get("pipeline_ref"):
            errors.append("acquire pipeline binding does not match pipeline_ref")
    batch = admission.get("batch")
    if batch is not None:
        arms = batch.get("arms", [])
        arm_ids = [arm.get("arm_id") for arm in arms]
        if len(arm_ids) != len(set(arm_ids)):
            errors.append("batch registration has duplicate arm IDs")
        registered = set(arm_ids)
        for arm in arms:
            if arm.get("predecessor") is not None and arm.get("predecessor") not in registered:
                errors.append("batch predecessor was not registered")
            if not set(arm.get("dependencies", [])).issubset(registered):
                errors.append("batch dependency was not registered")
        if not batch.get("registered_before_execution") or not batch.get("all_arms_visible"):
            errors.append("batch registration is not complete before execution")
    return errors


def validate_rpc(operation: Mapping[str, Any]) -> None:
    validate_instance(operation, "rpc-ops-v1.schema.json")
    errors = rpc_semantics(operation)
    if errors:
        raise ContractError("RPC semantics: " + "; ".join(errors))


def validate_state_transition(previous: str, current: str) -> None:
    allowed = {
        "free": {"starting", "quarantined"},
        "starting": {"running", "stopping", "quarantined"},
        "running": {"stopping", "quarantined"},
        "stopping": {"free", "quarantined"},
        "quarantined": {"starting", "quarantined"},
    }
    if current not in allowed.get(previous, set()):
        raise ContractError(f"invalid lane transition: {previous} -> {current}")


def validate_approval(approval: Mapping[str, Any], *, now: str | None = None, consume: bool = False) -> None:
    validate_instance(approval, "approval-v1.schema.json")
    if "id" not in approval:
        return
    if approval.get("challenge_nonce") != approval.get("nonce"):
        raise ContractError("approval challenge nonce mismatch")
    if approval.get("challenge_policy_hash") != approval.get("policy_hash"):
        raise ContractError("approval challenge policy hash mismatch")
    if approval.get("challenge_manifest_hash") != approval.get("manifest_hash"):
        raise ContractError("approval challenge manifest hash mismatch")
    state = approval.get("state")
    approved = approval.get("approved_at") is not None
    evidence = approval.get("verified_evidence")
    if approved and (approval.get("approver") is None or approval.get("proof") is None or evidence is None):
        raise ContractError("approved approval must carry approver, proof, and verified evidence")
    if state == "issued" and approved:
        raise ContractError("issued approval cannot be approved")
    if state in {"approved", "consumed"} and not approved:
        raise ContractError("approved state lacks approval evidence")
    if approval.get("consumed_at") is not None and approval.get("state") != "consumed":
        raise ContractError("consumed timestamp has non-consumed state")
    if now is not None and _parse_time(now) >= _parse_time(approval["expires"]):
        raise ContractError("approval expired")
    if consume and (approval.get("consumed_at") is not None or approval.get("state") != "approved"):
        raise ContractError("approval replay")


def validate_approval_binding(
    approval: Mapping[str, Any],
    *,
    destination_site: str,
    controller_id: str,
    payload_hash: str,
    policy_hash: str,
    revision: int | None = None,
    generation: int | None = None,
    action: str | None = None,
    requester: Mapping[str, Any] | None = None,
    manifest_hash: str | None = None,
    lane: Mapping[str, Any] | None = None,
    booking_id: str | None = None,
    now: str = "2026-09-27T20:01:00Z",
) -> None:
    validate_approval(approval, now=now, consume=True)
    for field, expected in (("destination_site", destination_site), ("controller_id", controller_id), ("payload_hash", payload_hash), ("policy_hash", policy_hash), ("manifest_hash", manifest_hash)):
        if approval.get(field) != expected:
            raise ContractError(f"approval binding mismatch: {field}")
    if action is not None and approval.get("action") != action:
        raise ContractError("approval binding mismatch: action")
    if requester is not None and approval.get("requester") != requester:
        raise ContractError("approval binding mismatch: requester")
    if revision is not None and approval.get("revision") != revision:
        raise ContractError("approval binding mismatch: revision")
    if generation is not None and approval.get("target_generation") != generation:
        raise ContractError("approval binding mismatch: generation")
    if lane is not None and approval.get("lane") != lane:
        raise ContractError("approval binding mismatch: lane")
    if booking_id is not None and approval.get("booking_id") != booking_id:
        raise ContractError("approval binding mismatch: booking_id")


def validate_signed_manifest(manifest: Mapping[str, Any], *, destination_site: str, policy_hash: str) -> None:
    validate_definition(manifest, "common.schema.json", "signed_manifest")
    if manifest["destination_site"] != destination_site:
        raise ContractError("manifest destination mismatch")
    if manifest["policy_hash"] != policy_hash:
        raise ContractError("manifest policy mismatch")


def validate_delegation_bounds(parent: Mapping[str, Any], child: Mapping[str, Any]) -> None:
    validate_definition(parent, "common.schema.json", "delegation")
    validate_definition(child, "common.schema.json", "delegation")
    for delegation in (parent, child):
        binding = delegation["binding"]
        if binding["audience"] != delegation["audience"] or binding["expires"] != delegation["expires"] or binding["nonce"] != delegation["nonce"]:
            raise ContractError("delegation canonical binding mismatch")
        attenuation = binding["attenuation"]
        if attenuation["max_depth"] != delegation["max_depth"] or set(attenuation["allowed_operations"]) != set(delegation["allowed_operations"]):
            raise ContractError("delegation attenuation binding mismatch")
        if attenuation["resource_ceiling"] != delegation["resource_ceiling"]:
            raise ContractError("delegation resource binding mismatch")
    if child["issuer"] != parent["delegate"]:
        raise ContractError("delegation issuer is not the parent delegate")
    for field in ("audience", "destination_site"):
        if child[field] != parent[field]:
            raise ContractError(f"delegation {field} mismatch")
    if not set(child["allowed_operations"]).issubset(parent["allowed_operations"]):
        raise ContractError("delegation operation attenuation widened")
    if not set(child["allowed_pipelines"]).issubset(parent["allowed_pipelines"]):
        raise ContractError("delegation pipeline attenuation widened")
    if child["resource_ceiling"]["max_s"] > parent["resource_ceiling"]["max_s"] or child["resource_ceiling"]["max_vram_bytes"] > parent["resource_ceiling"]["max_vram_bytes"]:
        raise ContractError("delegation resource attenuation widened")
    if _parse_time(child["expires"]) > _parse_time(parent["expires"]):
        raise ContractError("delegation expiry attenuation widened")
    if child["max_depth"] >= parent["max_depth"]:
        raise ContractError("delegation depth attenuation widened")


def pipeline_admission(
    pipeline: Mapping[str, Any],
    purpose: str,
    *,
    approval: Mapping[str, Any] | None = None,
    policy_hash: str | None = None,
    destination_site: str | None = None,
    content_labels: Iterable[str] | None = None,
    requester: Mapping[str, Any] | None = None,
    action: str = "pipeline",
    revision: int | None = None,
    generation: int | None = None,
    lane: Mapping[str, Any] | None = None,
    booking_id: str | None = None,
    payload_hash: str | None = None,
    manifest_hash: str | None = None,
    controller_id: str = "controller-a",
    now: str = "2026-09-27T20:01:00Z",
) -> bool:
    availability = pipeline.get("availability")
    if destination_site is not None:
        availability = pipeline.get("partner_overrides", {}).get(destination_site, availability)
    if availability == "unavailable":
        return False
    if not purpose or purpose != pipeline.get("purpose"):
        return False
    if policy_hash is not None and pipeline.get("policy_hash") != policy_hash:
        return False
    if content_labels is not None and not set(content_labels).issubset(set(pipeline.get("content_policy", []))):
        return False
    if availability == "approval_required":
        if approval is None or payload_hash is None or destination_site is None or requester is None:
            return False
        try:
            validate_approval_binding(
                approval,
                destination_site=destination_site,
                controller_id=controller_id,
                payload_hash=payload_hash,
                policy_hash=str(pipeline.get("policy_hash")),
                manifest_hash=manifest_hash,
                action=action,
                requester=requester,
                revision=revision,
                generation=generation,
                lane=lane,
                booking_id=booking_id,
                now=now,
            )
        except ContractError:
            return False
    elif approval is not None:
        return False
    return True


def validate_pipeline_config(config: Mapping[str, Any]) -> None:
    validate_instance(config, "pipeline-v1.schema.json")
    if "pipelines" in config:
        ids = [item["pipeline_id"] for item in config["pipelines"]]
        if len(ids) != len(set(ids)):
            raise ContractError("duplicate pipeline_id")


def validate_booking(booking: Mapping[str, Any]) -> None:
    validate_instance(booking, "booking-v1.schema.json")
    if "booking_id" not in booking:
        return
    start = _parse_time(booking["start"])
    end = _parse_time(booking["end"])
    if start >= end:
        raise ContractError("booking end must be after start")
    state = booking["state"]
    recovery = booking["recovery"]["state"]
    if state == "missed" and recovery not in {"none", "no-show-reopened"}:
        raise ContractError("missed booking has invalid recovery state")
    if state == "blocked" and recovery != "blocked-check-in":
        raise ContractError("blocked check-in must record recovery state")
    if state == "displaced" and booking["displacement"] is None:
        raise ContractError("displaced booking lacks displacement approval reference")
    if state == "claimed" and booking["checked_in_at"] is None:
        raise ContractError("claimed booking lacks check-in")


def validate_booking_transition(previous: str, current: str, *, blocked: bool = False) -> None:
    allowed = {
        "scheduled": {"blocked", "claimed", "missed", "cancelled", "displaced"},
        "blocked": {"claimed", "missed", "cancelled", "displaced"},
        "claimed": {"completed", "recovery"},
        "recovery": {"completed", "missed"},
        "missed": {"recovery"},
        "completed": set(),
        "cancelled": set(),
        "displaced": set(),
    }
    if current not in allowed.get(previous, set()) or (current == "blocked" and not blocked):
        raise ContractError(f"invalid booking transition: {previous} -> {current}")


def validate_discovery(proposal: Mapping[str, Any]) -> None:
    validate_instance(proposal, "discovery-v1.schema.json")
    host_ids = [item["host_id"] for item in proposal["hosts"]]
    lane_ids = [item["lane_id"] for item in proposal["lanes"]]
    if len(host_ids) != len(set(host_ids)) or len(lane_ids) != len(set(lane_ids)):
        raise ContractError("discovery proposal has duplicate stable IDs")
    if proposal["stage"] != "draft":
        raise ContractError("discovery output must remain a draft")
    try:
        ZoneInfo(proposal["timezone"])
    except ZoneInfoNotFoundError as exc:
        raise ContractError(f"unknown discovery timezone: {proposal['timezone']}") from exc
    host_map = {host["host_id"]: host for host in proposal["hosts"]}
    device_map: dict[str, str] = {}
    for host in proposal["hosts"]:
        count = host["gpu_count"]
        if count is None and not host["gpu_count_reason"]:
            raise ContractError(f"unknown discovery gpu_count lacks reason: {host['host_id']}")
        if count == 0 and (host["devices"] or "no gpu" not in str(host["gpu_count_reason"]).lower()):
            raise ContractError(f"discovery no-GPU host is inconsistent: {host['host_id']}")
        if isinstance(count, int) and count > 0 and count != len(host["devices"]):
            raise ContractError(f"discovery gpu_count does not match devices: {host['host_id']}")
        if host["reachability"] != "confirmed" and host["admissible"]:
            raise ContractError(f"unknown discovery host cannot be admissible: {host['host_id']}")
        if host["reachability"] == "confirmed" and host["observed_at"] is None:
            raise ContractError(f"confirmed discovery host lacks observation time: {host['host_id']}")
        for device in host["devices"]:
            if device["device_id"] in device_map:
                raise ContractError(f"discovery duplicate device ID: {device['device_id']}")
            device_map[device["device_id"]] = host["host_id"]
            reasons = device["unknown_reasons"]
            for field in ("vendor", "model", "vram_bytes", "driver"):
                if device[field] is None and field not in reasons:
                    raise ContractError(f"unknown discovery measurement lacks reason: {device['device_id']}.{field}")
                if device[field] is not None and field in reasons:
                    raise ContractError(f"known discovery measurement has unknown reason: {device['device_id']}.{field}")
    for lane in proposal["lanes"]:
        if lane["host_id"] not in host_map:
            raise ContractError(f"dangling discovery lane host: {lane['host_id']}")
        if lane["enabled"] and not host_map[lane["host_id"]]["admissible"]:
            raise ContractError(f"enabled discovery lane uses inadmissible host: {lane['host_id']}")
        for device_id in lane["device_ids"]:
            if device_id not in device_map:
                raise ContractError(f"dangling discovery device: {device_id}")
            if device_map[device_id] != lane["host_id"]:
                raise ContractError(f"discovery device belongs to another host: {device_id}")
    lane_set = set(lane_ids)
    if any(lane_id not in lane_set for lane_id in proposal["chat_lane_order"]):
        raise ContractError("dangling discovery chat lane")
    diff_keys = [item["sort_key"] for item in proposal["diff"]]
    if diff_keys != sorted(diff_keys):
        raise ContractError("discovery diff is not deterministic")
    for entry in proposal["diff"]:
        if entry["sort_key"] != f"{entry['kind']}/{entry['id']}":
            raise ContractError("discovery diff sort key mismatch")
    controller = proposal["controller"]
    if not controller.get("endpoint") or not controller.get("account") or controller.get("auth_state") != "configured":
        raise ContractError("discovery lacks complete controller authentication")
    if proposal["status"] == "confirmed" and any(item["review_required"] for item in proposal["diff"]):
        raise ContractError("review-required discovery cannot be confirmed")


def roundtrip(instance: Any, name: str | Path) -> Any:
    encoded = json.dumps(instance, sort_keys=True, separators=(",", ":"))
    decoded = json.loads(encoded)
    validate_instance(decoded, name)
    return decoded


def validate_document(instance: Any, name: str | Path) -> None:
    validate_instance(instance, name)
    stem = Path(name).name
    if stem == "inventory-v1.schema.json":
        errors = inventory_semantics(instance)
    elif stem == "rpc-ops-v1.schema.json":
        errors = rpc_semantics(instance)
    elif stem == "booking-v1.schema.json":
        validate_booking(instance)
        errors = []
    elif stem == "discovery-v1.schema.json":
        validate_discovery(instance)
        errors = []
    else:
        errors = []
    if errors:
        raise ContractError("semantic validation: " + "; ".join(errors))
