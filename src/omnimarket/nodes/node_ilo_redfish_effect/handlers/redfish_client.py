# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Session and discovery over a Redfish transport (OMN-21835).

Endpoints are discovered by following ``@odata.id`` links from the service root, never assumed. HPE's
guidance for iLO 4 (2.30 and later, Redfish 1.0) is to follow links and to send ``OData-Version: 4.0``,
which the transport does. A session is opened with one POST of the account to the session service and
closed with a DELETE of the URI iLO returned, so the password crosses the wire once per run.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, JsonValue, SecretStr

from omnimarket.nodes.node_ilo_redfish_effect.handlers.protocols import (
    ModelRedfishResponse,
    ProtocolRedfishTransport,
    RedfishTransportError,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.enum_ilo_redfish import (
    EnumIloRefusal,
)

ROOT_PATH = "/redfish/v1/"
_OK = range(200, 300)

JsonObject = dict[str, JsonValue]


class IloRefusalError(Exception):
    """A request step cannot go on. Carries a refusal code and a short, device-free detail."""

    def __init__(self, refusal: EnumIloRefusal, detail: str | None = None) -> None:
        super().__init__(refusal.value)
        self.refusal = refusal
        self.detail = detail


class ModelIloEndpoints(BaseModel):
    """The URIs discovered from the root."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    system: str
    manager: str
    chassis: tuple[str, ...]


def as_object(value: JsonValue) -> JsonObject:
    return value if isinstance(value, dict) else {}


def as_list(value: JsonValue) -> list[JsonValue]:
    return value if isinstance(value, list) else []


def api_path(link: str) -> str | None:
    """Reduce a link to an API path, or None when it leaves the Redfish tree.

    A device-supplied link is never followed to another host: only its path is used, on the pinned
    connection.
    """
    parts = urlsplit(link)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    if not parts.path.lower().startswith("/redfish/v1"):
        return None
    return path


def link_of(body: JsonObject, name: str) -> str | None:
    """The ``@odata.id`` of a named link, from the body, its ``Links`` or its legacy ``links``."""
    for container in (body, as_object(body.get("Links")), as_object(body.get("links"))):
        target = container.get(name)
        if isinstance(target, dict):
            for key in ("@odata.id", "href"):
                value = target.get(key)
                if isinstance(value, str):
                    return api_path(value)
    return None


def odata_ids(collection: JsonObject) -> list[str]:
    """Member URIs of a collection body (Redfish ``Members``, legacy ``links.Member``)."""
    found: list[str] = []
    members = as_list(collection.get("Members"))
    legacy = as_list(as_object(collection.get("links")).get("Member"))
    for entry in members or legacy:
        entry_obj = as_object(entry)
        value = entry_obj.get("@odata.id") or entry_obj.get("href")
        if isinstance(value, str):
            path = api_path(value)
            if path is not None:
                found.append(path)
    return found


def message_id(body: JsonValue) -> str | None:
    """The iLO message registry id of an error body (a code like ``iLO.0.10.InsufficientPrivilege``)."""
    error = as_object(as_object(body).get("error"))
    for info in as_list(error.get("@Message.ExtendedInfo")):
        value = as_object(info).get("MessageId")
        if isinstance(value, str):
            return value[:120]
    return None


class RedfishSession:
    """A logged-in view of one iLO."""

    def __init__(self, transport: ProtocolRedfishTransport) -> None:
        self._transport = transport
        self._token: SecretStr | None = None
        self._location: str | None = None

    # -- anonymous root -------------------------------------------------------------------
    def read_root(self) -> JsonObject:
        response = self._send("GET", ROOT_PATH, authenticated=False)
        self._expect_ok(response)
        return as_object(response.body)

    # -- session --------------------------------------------------------------------------
    def login(self, root: JsonObject, username: SecretStr, password: SecretStr) -> None:
        sessions = link_of(root, "Sessions")
        if sessions is None:
            service = link_of(root, "SessionService")
            if service is not None:
                sessions = link_of(self.get_anonymous_or_fail(service), "Sessions")
        if sessions is None:
            raise IloRefusalError(EnumIloRefusal.ENDPOINT_MISSING, "session service")
        response = self._send(
            "POST",
            sessions,
            body={
                "UserName": username.get_secret_value(),
                "Password": password.get_secret_value(),
            },
            authenticated=False,
        )
        if response.status in (401, 403):
            raise IloRefusalError(EnumIloRefusal.AUTH_FAILED)
        if response.status not in _OK:
            raise IloRefusalError(
                EnumIloRefusal.DEVICE_ERROR, f"session http {response.status}"
            )
        token = response.headers.get("x-auth-token")
        if not token:
            raise IloRefusalError(EnumIloRefusal.DEVICE_ERROR, "session without token")
        self._token = SecretStr(token)
        location = response.headers.get("location")
        self._location = api_path(location) if location else None

    def get_anonymous_or_fail(self, path: str) -> JsonObject:
        response = self._send("GET", path, authenticated=False)
        self._expect_ok(response)
        return as_object(response.body)

    def logout(self) -> None:
        """Best effort: a failed logout leaves a session that iLO expires on its own."""
        if self._token is None or self._location is None:
            return
        try:
            self._send("DELETE", self._location)
        except IloRefusalError:
            pass
        finally:
            self._token = None

    # -- authenticated calls --------------------------------------------------------------
    def get(self, path: str) -> JsonObject:
        response = self._send("GET", path)
        self._expect_ok(response)
        return as_object(response.body)

    def get_optional(self, path: str | None) -> JsonObject | None:
        if path is None:
            return None
        response = self._send("GET", path)
        if response.status == 404:
            return None
        self._expect_ok(response)
        return as_object(response.body)

    def members(self, collection_path: str) -> list[JsonObject]:
        return [self.get(member) for member in odata_ids(self.get(collection_path))]

    def send_write(
        self,
        method: Literal["POST", "PATCH"],
        path: str,
        body: JsonValue,
    ) -> ModelRedfishResponse:
        """A write call. Transport errors propagate: the caller knows whether the request was sent."""
        return self._transport.request(method, path, body=body, token=self._token)

    # -- internals ------------------------------------------------------------------------
    def _send(
        self,
        method: Literal["GET", "POST", "PATCH", "DELETE"],
        path: str,
        *,
        body: JsonValue = None,
        authenticated: bool = True,
    ) -> ModelRedfishResponse:
        try:
            return self._transport.request(
                method,
                path,
                body=body,
                token=self._token if authenticated else None,
            )
        except RedfishTransportError as exc:
            refusal = (
                EnumIloRefusal.TLS_FAILED
                if exc.kind == "tls"
                else EnumIloRefusal.CONNECT_FAILED
            )
            raise IloRefusalError(refusal, exc.kind) from None

    @staticmethod
    def _expect_ok(response: ModelRedfishResponse) -> None:
        if response.status in _OK:
            return
        if response.status == 401:
            raise IloRefusalError(EnumIloRefusal.AUTH_FAILED)
        if response.status == 403:
            raise IloRefusalError(
                EnumIloRefusal.INSUFFICIENT_PRIVILEGE, message_id(response.body)
            )
        if response.status == 404:
            raise IloRefusalError(EnumIloRefusal.ENDPOINT_MISSING, "http 404")
        raise IloRefusalError(EnumIloRefusal.DEVICE_ERROR, f"http {response.status}")


def discover(session: RedfishSession, root: JsonObject) -> ModelIloEndpoints:
    """Follow the root's links to the one system, the one manager and the chassis."""
    systems_link = link_of(root, "Systems")
    managers_link = link_of(root, "Managers")
    if systems_link is None or managers_link is None:
        raise IloRefusalError(
            EnumIloRefusal.ENDPOINT_MISSING, "systems or managers link"
        )
    systems = odata_ids(session.get(systems_link))
    managers = odata_ids(session.get(managers_link))
    if len(systems) != 1 or len(managers) != 1:
        raise IloRefusalError(
            EnumIloRefusal.AMBIGUOUS_ENDPOINT,
            f"systems={len(systems)} managers={len(managers)}",
        )
    chassis_link = link_of(root, "Chassis")
    chassis = tuple(odata_ids(session.get(chassis_link))) if chassis_link else ()
    return ModelIloEndpoints(system=systems[0], manager=managers[0], chassis=chassis)
