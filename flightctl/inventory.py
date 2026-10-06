"""Inventory v2 card binding (A3b): device records from the InventoryProbe and the inventory cross-reference rules.

device_from_probe maps one probed card (gpu-probe.schema.json#/$defs/inventory_device) to an inventory v2 device
record (inventory.schema.json#/$defs/device). inventory_problems is the production twin of the contract oracle
inventory_semantics: the rules JSON Schema cannot express (v1 rules carried forward plus the G07 card binding).
Nothing is cached and no argument is mutated.
"""

_MIB = 1048576
_UNKNOWN_REASONS = {
    "numa_node": "the inventory probe read no NUMA node for this card",
    "device_minor": "the inventory probe read no device minor for this card",
    "drives_display": "the inventory probe does not observe display use",
}


def device_from_probe(device_id, card, drives_display=None):
    """A new device record for `card`, with one unknown reason for each of numa_node, device_minor and
    drives_display that is None."""
    record = {"device_id": device_id, "vendor": "nvidia", "model": card["name"],
              "vram_bytes": card["memory_total_mib"] * _MIB, "driver": card["driver_version"], "uuid": card["uuid"],
              "pci_bus_id": card["pci_bus_id"], "numa_node": card["numa_node"], "device_minor": card["device_minor"],
              "drives_display": drives_display}
    record["unknown_reasons"] = {key: reason for key, reason in _UNKNOWN_REASONS.items() if record[key] is None}
    return record


def inventory_problems(inventory):
    """The cross-reference problems of a schema-valid inventory v2 document, one free-text line each; [] = none."""
    problems = []
    hosts = {host["host_id"]: host for host in inventory["hosts"]}
    if len(hosts) != len(inventory["hosts"]):
        problems.append("duplicate host_id")
    devices = {}
    for host in inventory["hosts"]:
        for device in host["devices"]:
            device_id = device["device_id"]
            if device_id in devices:
                problems.append(f"duplicate device_id {device_id}")
            devices[device_id] = (host["host_id"], device)
            problems += [f"device {device_id}: {key} is null without an unknown reason" for key in ("uuid", "pci_bus_id", "numa_node")
                         if device[key] is None and key not in device["unknown_reasons"]]
    lane_ids = [lane["lane_id"] for lane in inventory["lanes"]]
    if len(set(lane_ids)) != len(lane_ids):
        problems.append("duplicate lane_id")
    claimed = {}
    for lane in inventory["lanes"]:
        lane_id, host_id = lane["lane_id"], lane["host_id"]
        if host_id not in hosts:
            problems.append(f"lane {lane_id}: unknown host {host_id}")
            continue
        for device_id in lane["device_ids"]:
            owner = devices.get(device_id)
            if owner is None or owner[0] != host_id:
                problems.append(f"lane {lane_id}: device {device_id} is not on host {host_id}")
                continue
            if device_id in claimed:
                problems.append(f"device {device_id} is in lanes {claimed[device_id]} and {lane_id}")
            claimed[device_id] = lane_id
            if lane["enabled"] and owner[1]["uuid"] is None:
                problems.append(f"enabled lane {lane_id}: device {device_id} has no uuid (the occupancy probe cannot bind it)")
        reachability = hosts[host_id]["reachability"]
        if lane["enabled"] and inventory["stage"] == "confirmed" and reachability not in ("confirmed", "asleep"):
            problems.append(f"enabled lane {lane_id} of a confirmed inventory: host {host_id} has reachability {reachability}")
    problems += [f"endpoint_lane_order names unknown lane {lane_id}" for lane_id in inventory["endpoint_lane_order"]
                 if lane_id not in lane_ids]
    if inventory["stage"] == "confirmed" and inventory["controller"]["auth_state"] != "configured":
        problems.append("a confirmed inventory needs a controller whose auth_state is configured")
    return problems
