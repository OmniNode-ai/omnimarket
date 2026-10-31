# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the iLO 4 Redfish effect (OMN-21835).

The handler owns the decisions; every contact with the world goes through one of these ports, which
the composition root binds and a test replaces. No port returns a credential in a repr or a log line.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, JsonValue, SecretStr

from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_overlay import (
    ModelIloHost,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_redfish_result import (
    ModelIloApproval,
    ModelIloJournalRecord,
)

TransportFailureKind = Literal["tls", "connect", "timeout", "protocol"]


class RedfishTransportError(Exception):
    """The transport could not complete a request. ``kind`` is a code; the text never echoes the device."""

    def __init__(self, kind: TransportFailureKind, *, request_sent: bool) -> None:
        super().__init__(kind)
        self.kind: TransportFailureKind = kind
        # True when the request may have reached the device: the outcome of a write is then unknown.
        self.request_sent = request_sent


class ModelRedfishResponse(BaseModel):
    """One HTTP answer. ``headers`` keys are lower-cased."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: int
    headers: dict[str, str] = {}
    body: JsonValue = None


class ProtocolRedfishTransport(Protocol):
    """One TLS-pinned HTTPS connection target for one host."""

    def request(
        self,
        method: Literal["GET", "POST", "PATCH", "DELETE"],
        path: str,
        *,
        body: JsonValue = None,
        token: SecretStr | None = None,
    ) -> ModelRedfishResponse: ...


# Builds the transport for a host. It raises RedfishTransportError(kind="tls") when the pinned
# certificate cannot be loaded or does not match its pin.
TransportFactory = Callable[[ModelIloHost], ProtocolRedfishTransport]


class ProtocolCredentialSource(Protocol):
    """Reads a credential from the lab secret store, and only from it."""

    def get(self, key: str) -> SecretStr | None: ...


class CredentialSourceNotStoreError(Exception):
    """The value did not come from the lab secret store (for example, an environment variable)."""


class ProtocolApprovalSource(Protocol):
    """Finds the OPERATOR-CONSENT ledger row a write cites."""

    def find(self, ref: str) -> ModelIloApproval | None: ...


class ProtocolActionJournal(Protocol):
    """The per-host record of write attempts. ``append`` is durable before it returns."""

    def lookup(self, host: str, key: str) -> list[ModelIloJournalRecord]: ...

    def append(self, record: ModelIloJournalRecord) -> None: ...


Clock = Callable[[], datetime]
Sleeper = Callable[[float], None]
