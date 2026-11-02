# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read verbs of the iLO 4 Redfish effect (OMN-21835).

Every function here only issues GET requests. A resource that a generation does not advertise is
reported under ``unavailable`` rather than guessed, because HPE's iLO 4 documentation does not give a
per-generation resource matrix (design note, U1).
"""

from __future__ import annotations

from pydantic import JsonValue

from omnimarket.nodes.node_ilo_redfish_effect.handlers.redfish_client import (
    JsonObject,
    ModelIloEndpoints,
    RedfishSession,
    api_path,
    as_list,
    as_object,
    link_of,
    odata_ids,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.safety import IdentityReading
from omnimarket.nodes.node_ilo_redfish_effect.models.enum_ilo_redfish import (
    EnumIloLogKind,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_overlay import (
    ModelIloHost,
)

_MAX_LOG_PAGES = 5
_SYSTEM_FIELDS = (
    "Model",
    "SKU",
    "SerialNumber",
    "UUID",
    "PowerState",
    "BiosVersion",
    "HostName",
    "Manufacturer",
    "SystemType",
)


def _pick(body: JsonObject, names: tuple[str, ...]) -> dict[str, JsonValue]:
    return {name: body[name] for name in names if name in body}


def _status(body: JsonObject) -> dict[str, JsonValue]:
    status = as_object(body.get("Status"))
    return {
        key: status[key]
        for key in ("State", "Health", "HealthRollup", "HealthRollUp")
        if key in status
    }


def read_discover(
    session: RedfishSession,
    root: JsonObject,
    endpoints: ModelIloEndpoints,
    host: ModelIloHost,
) -> dict[str, JsonValue]:
    manager = session.get(endpoints.manager)
    system = session.get(endpoints.system)
    license_link = _license_link(manager)
    license_body = session.get_optional(license_link)
    ilo_license = (
        {"LicenseType": license_body.get("LicenseType")}
        if license_body is not None
        else None
    )
    advertised: list[JsonValue] = [
        name
        for name in (
            "Processors",
            "Memory",
            "EthernetInterfaces",
            "LogServices",
            "Bios",
        )
        if link_of(system, name) is not None
    ]
    return {
        "generation_declared": host.generation.value,
        "redfish_version": root.get("RedfishVersion"),
        "systems": [endpoints.system],
        "managers": [endpoints.manager],
        "chassis": list(endpoints.chassis),
        "ilo_firmware": manager.get("FirmwareVersion"),
        "ilo_model": manager.get("Model"),
        "license": ilo_license if ilo_license is not None else "not advertised",
        "advertised_system_links": advertised,
    }


def _license_link(manager: JsonObject) -> str | None:
    hp = as_object(as_object(manager.get("Oem")).get("Hp"))
    return link_of(as_object(hp.get("links")), "LicenseService") or link_of(
        as_object(hp.get("Links")), "LicenseService"
    )


def read_identity_data(
    reading: IdentityReading, manager: JsonObject, host: ModelIloHost
) -> dict[str, JsonValue]:
    return {
        "model": reading.model,
        "serial": reading.serial,
        "ilo_firmware": manager.get("FirmwareVersion"),
        "generation_declared": host.generation.value,
        "generation_consistent": reading.generation_consistent,
        "product_matches_overlay": reading.product_matches,
        "serial_matches_overlay": reading.serial_matches,
        "overlay_serial_set": host.expected_serial is not None,
    }


def read_inventory(
    session: RedfishSession, endpoints: ModelIloEndpoints
) -> dict[str, JsonValue]:
    system = session.get(endpoints.system)
    unavailable: list[JsonValue] = []
    data: dict[str, JsonValue] = {"system": _pick(system, _SYSTEM_FIELDS)}
    for name in ("ProcessorSummary", "MemorySummary"):
        if name in system:
            data[name] = system[name]
    for name, fields in (
        ("Processors", ("Model", "TotalCores", "TotalThreads", "MaxSpeedMHz")),
        ("EthernetInterfaces", ("Name", "MACAddress", "SpeedMbps")),
    ):
        path = link_of(system, name)
        if path is None:
            unavailable.append(name)
            continue
        data[name] = [
            {**_pick(member, fields), "Status": _status(member)}
            for member in session.members(path)
        ]
    chassis_items: list[JsonValue] = []
    for chassis_path in endpoints.chassis:
        chassis = session.get(chassis_path)
        chassis_items.append(
            {
                **_pick(
                    chassis, ("Model", "SerialNumber", "ChassisType", "Manufacturer")
                ),
                "Status": _status(chassis),
            }
        )
    data["chassis"] = chassis_items
    data["unavailable"] = unavailable
    return data


def read_health(
    session: RedfishSession, endpoints: ModelIloEndpoints
) -> dict[str, JsonValue]:
    system = session.get(endpoints.system)
    chassis_health: list[JsonValue] = []
    unavailable: list[JsonValue] = []
    for chassis_path in endpoints.chassis:
        chassis = session.get(chassis_path)
        item: dict[str, JsonValue] = {"Status": _status(chassis)}
        thermal = session.get_optional(link_of(chassis, "Thermal"))
        if thermal is None:
            unavailable.append("Thermal")
        else:
            item["temperatures"] = [
                {**_pick(t, ("Name", "ReadingCelsius")), "Status": _status(t)}
                for t in map(as_object, as_list(thermal.get("Temperatures")))
            ]
            item["fans"] = [
                {"Name": f.get("FanName") or f.get("Name"), "Status": _status(f)}
                for f in map(as_object, as_list(thermal.get("Fans")))
            ]
        power = session.get_optional(link_of(chassis, "Power"))
        if power is None:
            unavailable.append("Power")
        else:
            item["power_supplies"] = [
                {**_pick(p, ("Name", "PowerCapacityWatts")), "Status": _status(p)}
                for p in map(as_object, as_list(power.get("PowerSupplies")))
            ]
        chassis_health.append(item)
    return {
        "system": {"PowerState": system.get("PowerState"), "Status": _status(system)},
        "chassis": chassis_health,
        "unavailable": unavailable,
    }


def read_power_status(
    session: RedfishSession, endpoints: ModelIloEndpoints
) -> dict[str, JsonValue]:
    system = session.get(endpoints.system)
    return {"PowerState": system.get("PowerState"), "Status": _status(system)}


def read_logs(
    session: RedfishSession,
    endpoints: ModelIloEndpoints,
    kind: EnumIloLogKind,
    limit: int,
) -> dict[str, JsonValue]:
    """Newest-first entries of the IML (under the system) or the iLO event log (under the manager).

    Entry paging on iLO 4 is not documented (design note, U5), so pages are followed through the
    ``Members@odata.nextLink`` property when one is present, to a fixed page bound, and the newest
    ``limit`` entries are kept by their ``Created`` time.
    """
    owner = endpoints.system if kind is EnumIloLogKind.IML else endpoints.manager
    body = session.get(owner)
    services_path = link_of(body, "LogServices")
    if services_path is None:
        return {
            "kind": kind.value,
            "entries": [],
            "unavailable": "LogServices not advertised",
        }
    wanted = "iml" if kind is EnumIloLogKind.IML else "iel"
    service_path = next(
        (
            p
            for p in odata_ids(session.get(services_path))
            if p.rstrip("/").lower().endswith(wanted)
        ),
        None,
    )
    if service_path is None:
        return {
            "kind": kind.value,
            "entries": [],
            "unavailable": f"{wanted} log service not listed",
        }
    entries_path = link_of(session.get(service_path), "Entries")
    if entries_path is None:
        return {
            "kind": kind.value,
            "entries": [],
            "unavailable": "Entries not advertised",
        }
    collected: list[JsonObject] = []
    next_path: str | None = entries_path
    for _ in range(_MAX_LOG_PAGES):
        if next_path is None:
            break
        page = session.get(next_path)
        collected.extend(as_object(m) for m in as_list(page.get("Members")))
        next_link = page.get("Members@odata.nextLink")
        next_path = None
        if isinstance(next_link, str):
            next_path = api_path(next_link)
    collected.sort(key=lambda e: str(e.get("Created") or ""), reverse=True)
    shown: list[JsonValue] = [
        {
            "Created": e.get("Created"),
            "Severity": e.get("Severity"),
            "Message": e.get("Message"),
            "Class": as_object(as_object(e.get("Oem")).get("Hp")).get("Class"),
        }
        for e in collected[:limit]
    ]
    return {"kind": kind.value, "entries": shown, "fetched": len(collected)}
