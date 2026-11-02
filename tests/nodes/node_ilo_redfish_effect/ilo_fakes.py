"""Mocked iLO 4 for the iLO Redfish effect tests (OMN-21835).

Nothing here talks to a network. The resource trees are SYNTHETIC: they follow the property and
resource names of the HPE iLO 4 RESTful API data model reference (lower-case ``/redfish/v1/systems/1/``
paths, ``Oem.Hp`` extensions, the ``#ComputerSystem.Reset`` action) and invent every value. They are
not captures of a device. The Gen8 and Gen9 differences they encode are the ones the design note marks
as runtime-discovered, not HPE-confirmed: a Gen9 system advertises a boot override, a Gen8 system does
not and offers ``Oem.Hp.BootOnNextServerReset`` on its virtual media instead. The first approved read
replaces these with scrubbed recordings.

Hosts use documentation-range addresses and invented serials.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal

from pydantic import JsonValue, SecretStr

from omnimarket.nodes.node_ilo_redfish_effect.handlers.protocols import (
    ModelRedfishResponse,
    RedfishTransportError,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_redfish_result import (
    ModelIloApproval,
    ModelIloJournalRecord,
)

TOKEN = "tok-3f9a-secret-session-value"
GEN9_SERIAL = "TST9SERIAL01"
GEN8_SERIAL = "TST8SERIAL01"
IMAGE_URL = "http://192.0.2.10/iso/installer.iso"
LICENSE_KEY = "ABCDE-12345-FGHIJ-67890-KLMNO"

Json = dict[str, JsonValue]


def _ref(path: str) -> Json:
    return {"@odata.id": path}


def _members(*paths: str) -> Json:
    return {"Members": [_ref(p) for p in paths]}


def _virtual_media(*, gen8: bool) -> dict[str, Json]:
    cd: Json = {
        "Id": "2",
        "MediaTypes": ["CD", "DVD"],
        "Inserted": False,
        "Image": None,
        "WriteProtected": True,
        "ConnectedVia": "NotConnected",
        "Actions": {
            "#HpiLOVirtualMedia.InsertVirtualMedia": {
                "target": "/redfish/v1/managers/1/virtualmedia/2/actions/hpilovirtualmedia.insertvirtualmedia/"
            },
            "#HpiLOVirtualMedia.EjectVirtualMedia": {
                "target": "/redfish/v1/managers/1/virtualmedia/2/actions/hpilovirtualmedia.ejectvirtualmedia/"
            },
        },
    }
    if gen8:
        cd["Oem"] = {"Hp": {"BootOnNextServerReset": False}}
    floppy: Json = {
        "Id": "1",
        "MediaTypes": ["Floppy", "USBStick"],
        "Inserted": False,
        "Image": None,
    }
    return {
        "/redfish/v1/managers/1/virtualmedia/": _members(
            "/redfish/v1/managers/1/virtualmedia/1/",
            "/redfish/v1/managers/1/virtualmedia/2/",
        ),
        "/redfish/v1/managers/1/virtualmedia/1/": floppy,
        "/redfish/v1/managers/1/virtualmedia/2/": cd,
    }


def ilo_resources(generation: Literal["gen8", "gen9"]) -> dict[str, Json]:
    """A synthetic iLO 4 resource tree for one generation."""
    gen8 = generation == "gen8"
    system: Json = {
        "Id": "1",
        "Model": "ProLiant DL380p Gen8" if gen8 else "ProLiant DL360 Gen9",
        "SKU": "TST-SKU-001",
        "SerialNumber": GEN8_SERIAL if gen8 else GEN9_SERIAL,
        "UUID": "00000000-0000-4000-8000-000000000001",
        "Manufacturer": "HPE",
        "PowerState": "On",
        "Status": {"State": "Enabled", "Health": "OK", "HealthRollup": "OK"},
        "ProcessorSummary": {"Count": 2, "Model": "Test CPU"},
        "MemorySummary": {"TotalSystemMemoryGiB": 64},
        "Processors": _ref("/redfish/v1/systems/1/processors/"),
        "LogServices": _ref("/redfish/v1/systems/1/logservices/"),
        "Actions": {
            "#ComputerSystem.Reset": {
                "target": "/redfish/v1/systems/1/actions/computersystem.reset/",
                "ResetType@Redfish.AllowableValues": [
                    "On",
                    "ForceOff",
                    "ForceRestart",
                    "Nmi",
                    "PushPowerButton",
                ],
            }
        },
    }
    if not gen8:
        system["Boot"] = {
            "BootSourceOverrideTarget": "None",
            "BootSourceOverrideEnabled": "Disabled",
            "BootSourceOverrideTarget@Redfish.AllowableValues": [
                "None",
                "Pxe",
                "Cd",
                "Hdd",
                "Usb",
                "BiosSetup",
                "UefiShell",
            ],
        }
    resources: dict[str, Json] = {
        "/redfish/v1/": {
            "RedfishVersion": "1.0.0",
            "Systems": _ref("/redfish/v1/systems/"),
            "Managers": _ref("/redfish/v1/managers/"),
            "Chassis": _ref("/redfish/v1/chassis/"),
            "Links": {"Sessions": _ref("/redfish/v1/sessions/")},
        },
        "/redfish/v1/systems/": _members("/redfish/v1/systems/1/"),
        "/redfish/v1/systems/1/": system,
        "/redfish/v1/systems/1/processors/": _members(
            "/redfish/v1/systems/1/processors/1/"
        ),
        "/redfish/v1/systems/1/processors/1/": {
            "Model": "Test CPU",
            "TotalCores": 8,
            "TotalThreads": 16,
            "MaxSpeedMHz": 3000,
            "Status": {"Health": "OK"},
        },
        "/redfish/v1/systems/1/logservices/": _members(
            "/redfish/v1/systems/1/logservices/iml/"
        ),
        "/redfish/v1/systems/1/logservices/iml/": {
            "Entries": _ref("/redfish/v1/systems/1/logservices/iml/entries/")
        },
        "/redfish/v1/systems/1/logservices/iml/entries/": {
            "Members": [
                {
                    "Created": "2026-01-01T00:00:00Z",
                    "Severity": "OK",
                    "Message": "Older entry",
                    "Oem": {"Hp": {"Class": 5}},
                },
                {
                    "Created": "2026-03-01T00:00:00Z",
                    "Severity": "Warning",
                    "Message": f"Added license {LICENSE_KEY} to the server",
                    "Oem": {"Hp": {"Class": 9}},
                },
                {
                    "Created": "2026-02-01T00:00:00Z",
                    "Severity": "Critical",
                    "Message": "Fan failed",
                    "Oem": {"Hp": {"Class": 7}},
                },
            ]
        },
        "/redfish/v1/managers/": _members("/redfish/v1/managers/1/"),
        "/redfish/v1/managers/1/": {
            "FirmwareVersion": "iLO 4 v2.82",
            "Model": "iLO 4",
            "VirtualMedia": _ref("/redfish/v1/managers/1/virtualmedia/"),
            "LogServices": _ref("/redfish/v1/managers/1/logservices/"),
            "Oem": {
                "Hp": {
                    "links": {
                        "LicenseService": _ref(
                            "/redfish/v1/managers/1/licenseservice/1/"
                        )
                    }
                }
            },
        },
        "/redfish/v1/managers/1/licenseservice/1/": {
            "LicenseType": "Perpetual",
            "LicenseKey": LICENSE_KEY,
        },
        "/redfish/v1/managers/1/logservices/": _members(
            "/redfish/v1/managers/1/logservices/iel/"
        ),
        "/redfish/v1/managers/1/logservices/iel/": {
            "Entries": _ref("/redfish/v1/managers/1/logservices/iel/entries/")
        },
        "/redfish/v1/managers/1/logservices/iel/entries/": {
            "Members": [
                {
                    "Created": "2026-03-02T00:00:00Z",
                    "Severity": "OK",
                    "Message": "iLO reset",
                }
            ]
        },
        "/redfish/v1/chassis/": _members("/redfish/v1/chassis/1/"),
        "/redfish/v1/chassis/1/": {
            "Model": "Test chassis",
            "SerialNumber": GEN8_SERIAL if gen8 else GEN9_SERIAL,
            "ChassisType": "RackMount",
            "Status": {"Health": "OK"},
            "Thermal": _ref("/redfish/v1/chassis/1/thermal/"),
            "Power": _ref("/redfish/v1/chassis/1/power/"),
        },
        "/redfish/v1/chassis/1/thermal/": {
            "Temperatures": [
                {"Name": "Inlet", "ReadingCelsius": 22, "Status": {"Health": "OK"}}
            ],
            "Fans": [{"FanName": "Fan 1", "Status": {"Health": "OK"}}],
        },
        "/redfish/v1/chassis/1/power/": {
            "PowerSupplies": [
                {"Name": "PSU 1", "PowerCapacityWatts": 460, "Status": {"Health": "OK"}}
            ]
        },
    }
    resources.update(_virtual_media(gen8=gen8))
    return copy.deepcopy(resources)


def dig(value: object, *path: str | int) -> object:
    """Walk dict keys and list indexes; the test-side way to read nested device JSON."""
    current = value
    for step in path:
        if isinstance(step, int):
            assert isinstance(current, list)
            current = current[step]
        else:
            assert isinstance(current, dict)
            current = current[step]
    return current


def put(root: Json, path: tuple[str, ...], value: JsonValue) -> None:
    """Set a nested key on a resource body."""
    node = root
    for step in path[:-1]:
        child = node[step]
        assert isinstance(child, dict)
        node = child
    node[path[-1]] = value


def _merge(target: Json, patch: Json) -> None:
    for key, value in patch.items():
        existing = target.get(key)
        if isinstance(value, dict) and isinstance(existing, dict):
            _merge(existing, value)
        else:
            target[key] = value


RESET_ACTION = "/redfish/v1/systems/1/actions/computersystem.reset/"
INSERT_ACTION = "/redfish/v1/managers/1/virtualmedia/2/actions/hpilovirtualmedia.insertvirtualmedia/"
EJECT_ACTION = (
    "/redfish/v1/managers/1/virtualmedia/2/actions/hpilovirtualmedia.ejectvirtualmedia/"
)
CD_MEMBER = "/redfish/v1/managers/1/virtualmedia/2/"
SYSTEM = "/redfish/v1/systems/1/"


class FakeIloTransport:
    """A stateful stand-in for one iLO. Records every call; mutates state on the documented writes."""

    def __init__(
        self,
        resources: dict[str, Json],
        *,
        accept_users: frozenset[str] = frozenset({"reader", "operator"}),
    ) -> None:
        self.resources = resources
        self.calls: list[tuple[str, str, JsonValue, bool]] = []
        self.accept_users = accept_users
        self.deny_writes_with: int | None = None
        self.fail_write_after_send = False
        self.no_effect_on_reset = False
        self.before_write: Callable[[str, str], None] | None = None
        self.session_logged_in = False

    # -- helpers for tests -----------------------------------------------------------------
    def write_calls(self) -> list[tuple[str, str, JsonValue]]:
        return [
            (m, p, b)
            for m, p, b, _ in self.calls
            if m in {"POST", "PATCH"} and "/sessions" not in p
        ]

    def methods(self) -> list[str]:
        return [m for m, _, _, _ in self.calls]

    # -- transport -------------------------------------------------------------------------
    def request(
        self,
        method: Literal["GET", "POST", "PATCH", "DELETE"],
        path: str,
        *,
        body: JsonValue = None,
        token: SecretStr | None = None,
    ) -> ModelRedfishResponse:
        self.calls.append((method, path, body, token is not None))
        if method == "POST" and path == "/redfish/v1/sessions/":
            assert isinstance(body, dict)
            if body.get("UserName") not in self.accept_users:
                return ModelRedfishResponse(status=401)
            self.session_logged_in = True
            return ModelRedfishResponse(
                status=201,
                headers={
                    "x-auth-token": TOKEN,
                    "location": "https://192.0.2.1/redfish/v1/sessions/abc/",
                },
                body={},
            )
        if method == "DELETE":
            self.session_logged_in = False
            return ModelRedfishResponse(status=200, body={})
        if path != "/redfish/v1/" and (
            token is None or token.get_secret_value() != TOKEN
        ):
            return ModelRedfishResponse(status=401)
        if method == "GET":
            resource = self.resources.get(path)
            if resource is None:
                return ModelRedfishResponse(status=404)
            return ModelRedfishResponse(status=200, body=copy.deepcopy(resource))
        return self._write(method, path, body)

    def _write(self, method: str, path: str, body: JsonValue) -> ModelRedfishResponse:
        if self.before_write is not None:
            self.before_write(method, path)
        if self.deny_writes_with is not None:
            return ModelRedfishResponse(
                status=self.deny_writes_with,
                body={
                    "error": {
                        "@Message.ExtendedInfo": [
                            {"MessageId": "iLO.0.10.InsufficientPrivilege"}
                        ]
                    }
                },
            )
        if self.fail_write_after_send:
            raise RedfishTransportError("timeout", request_sent=True)
        assert isinstance(body, dict)
        if method == "POST" and path == RESET_ACTION:
            reset = body["ResetType"]
            if not self.no_effect_on_reset:
                system = self.resources[SYSTEM]
                system["PowerState"] = "Off" if reset == "ForceOff" else "On"
            return ModelRedfishResponse(status=200, body={})
        if method == "POST" and path == INSERT_ACTION:
            self.resources[CD_MEMBER].update({"Inserted": True, "Image": body["Image"]})
            return ModelRedfishResponse(status=200, body={})
        if method == "POST" and path == EJECT_ACTION:
            self.resources[CD_MEMBER].update({"Inserted": False, "Image": None})
            return ModelRedfishResponse(status=200, body={})
        if method == "PATCH" and path in self.resources:
            _merge(self.resources[path], body)
            return ModelRedfishResponse(status=200, body={})
        return ModelRedfishResponse(status=404)


class MemoryJournal:
    def __init__(self) -> None:
        self.records: list[ModelIloJournalRecord] = []

    def lookup(self, host: str, key: str) -> list[ModelIloJournalRecord]:
        return [r for r in self.records if r.host == host and r.key == key]

    def append(self, record: ModelIloJournalRecord) -> None:
        self.records.append(record)


class MapApprovals:
    def __init__(self, *rows: ModelIloApproval) -> None:
        self.rows = {r.ref: r for r in rows}

    def find(self, ref: str) -> ModelIloApproval | None:
        return self.rows.get(ref)


class MapCredentials:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.asked: list[str] = []

    def get(self, key: str) -> SecretStr | None:
        self.asked.append(key)
        value = self.values.get(key)
        return SecretStr(value) if value is not None else None


NOW = datetime(2026, 11, 2, 12, 0, 0, tzinfo=UTC)


def clock() -> datetime:
    return NOW


def no_sleep(_: float) -> None:
    return None


Factory = Callable[..., FakeIloTransport]
