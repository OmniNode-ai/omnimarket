# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the delegation check probe node: the responder reader, the customer machine and the result publisher."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from omnimarket.handlers.routing.delegation_check_responder_chain import (
    ModelChainResolution,
    ModelResponderReading,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    CheckId,
    ModelDashboardObservation,
    ModelDelegateObservation,
    ModelRoutingCollection,
    ModelSurfaceObservation,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_gateway_check import (
    ModelGatewayObservation,
)


class CollectorAbsentError(RuntimeError):
    """The machine has no collector for what a check needs; the check reads PRECONDITION-ABSENT, never green."""

    def __init__(self, name: str, detail: str) -> None:
        super().__init__(f"{name}: {detail}")
        self.name = name
        self.detail = detail


class ProtocolResponderReader(Protocol):
    def read(self, base_url: str) -> ModelResponderReading: ...


class ProtocolCustomerMachine(Protocol):
    """A disposable no-checkout customer machine. A method raises ``CollectorAbsentError`` when it has no collector."""

    def run_delegation(
        self, check_id: CheckId, responder: ModelChainResolution
    ) -> ModelDelegateObservation: ...

    def collect_routing(
        self, responder: ModelChainResolution
    ) -> ModelRoutingCollection: ...

    def collect_surface(self) -> ModelSurfaceObservation: ...

    def versions(self) -> dict[str, str]: ...


@runtime_checkable
class ProtocolDashboardMachine(Protocol):
    """A customer machine that can also walk the customer path to the local dashboard (C30)."""

    def collect_dashboard(
        self, responder: ModelChainResolution
    ) -> ModelDashboardObservation: ...


class ProtocolCheckResultPublisher(Protocol):
    def publish(
        self,
        *,
        topic: str,
        event_type: str,
        payload: Mapping[str, object],
        event_id: str,
        correlation_id: str | None = None,
        partition_key: str | None = None,
    ) -> bool: ...


@runtime_checkable
class ProtocolGatewayClient(Protocol):
    """The dev gateway as a customer reaches it: the address and tenant the settings store names, one request at a time.

    ``auth`` is ``none`` (no credential), ``invalid`` (a credential no tenant owns) or ``tenant`` (the test tenant's own).
    """

    gateway_url: str
    tenant_slug: str
    # The provider the tenant's own key reaches, when the row names one; own-key checks read the receipt against it.
    chosen_provider: str | None

    def call(
        self,
        method: str,
        path: str,
        *,
        auth: str,
        body: dict[str, Any] | None = None,
    ) -> ModelGatewayObservation: ...
