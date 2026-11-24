# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the delegation check probe node: the responder reader, the customer machine and the result publisher."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from omnimarket.handlers.routing.delegation_check_responder_chain import (
    ModelChainResolution,
    ModelResponderReading,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    CheckId,
    ModelDelegateObservation,
    ModelRoutingCollection,
    ModelSurfaceObservation,
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
