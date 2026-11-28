# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Adapters of the delegation check probe node: the live responder reader, an absent machine and in-memory doubles."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omnimarket.handlers.routing.delegation_check_responder_chain import (
    ModelChainResolution,
    ModelResponderReading,
    reading_from_models_body,
)
from omnimarket.handlers.routing.responder_json_read import read_responder_json
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    CheckId,
    ModelDelegateObservation,
    ModelRoutingCollection,
    ModelSurfaceObservation,
)
from omnimarket.nodes.node_delegation_check_probe_effect.protocols.protocol_delegation_check import (
    CollectorAbsentError,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.transport import (
    served_models_url,
)


class HttpResponderReader:
    """Reads the served-model list of a local responder and, when it reports slots, how many are processing."""

    def __init__(self, timeout_s: float) -> None:
        self._timeout_s = timeout_s

    def read(self, base_url: str) -> ModelResponderReading:
        body = read_responder_json(served_models_url(base_url), self._timeout_s)
        parsed = body if isinstance(body, dict) else None
        slots = read_responder_json(f"{base_url}/slots", self._timeout_s)
        busy = (
            sum(1 for s in slots if isinstance(s, dict) and s.get("is_processing"))
            if isinstance(slots, list)
            else None
        )
        capacity = len(slots) if isinstance(slots, list) and slots else None
        return reading_from_models_body(base_url, parsed, busy=busy, capacity=capacity)


class AbsentCustomerMachine:
    """A machine with no collector: every check that needs one reads PRECONDITION-ABSENT, never green."""

    def __init__(self, detail: str) -> None:
        self._detail = detail

    def run_delegation(
        self, check_id: CheckId, responder: ModelChainResolution
    ) -> ModelDelegateObservation:
        raise CollectorAbsentError("customer_machine", self._detail)

    def collect_routing(
        self, responder: ModelChainResolution
    ) -> ModelRoutingCollection:
        raise CollectorAbsentError("customer_machine", self._detail)

    def collect_surface(self) -> ModelSurfaceObservation:
        raise CollectorAbsentError("customer_machine", self._detail)

    def versions(self) -> dict[str, str]:
        return {}


class InMemoryResponderReader:
    def __init__(self, readings: Mapping[str, ModelResponderReading]) -> None:
        self._readings = dict(readings)

    def read(self, base_url: str) -> ModelResponderReading:
        return self._readings.get(
            base_url, ModelResponderReading(base_url=base_url, answered=False)
        )


class InMemoryCustomerMachine:
    def __init__(
        self,
        *,
        delegation: ModelDelegateObservation | None = None,
        routing: ModelRoutingCollection | None = None,
        surface: ModelSurfaceObservation | None = None,
    ) -> None:
        self._delegation = delegation
        self._routing = routing
        self._surface = surface
        self.delegated_through: list[ModelChainResolution] = []

    def run_delegation(
        self, check_id: CheckId, responder: ModelChainResolution
    ) -> ModelDelegateObservation:
        self.delegated_through.append(responder)
        if self._delegation is None:
            raise CollectorAbsentError("delegation", "no delegation observation")
        return self._delegation

    def collect_routing(
        self, responder: ModelChainResolution
    ) -> ModelRoutingCollection:
        if self._routing is None:
            raise CollectorAbsentError("routing", "no routing collection")
        return self._routing

    def collect_surface(self) -> ModelSurfaceObservation:
        if self._surface is None:
            raise CollectorAbsentError("surface", "no surface observation")
        return self._surface

    def versions(self) -> dict[str, str]:
        return {}


class InMemoryCheckPublisher:
    """Records what would go on the bus; ``accept`` False models a bus that refuses."""

    def __init__(self, *, accept: bool = True) -> None:
        self.events: list[dict[str, Any]] = []
        self._accept = accept

    def publish(
        self,
        *,
        topic: str,
        event_type: str,
        payload: Mapping[str, object],
        event_id: str,
        correlation_id: str | None = None,
        partition_key: str | None = None,
    ) -> bool:
        if self._accept:
            self.events.append(
                {
                    "topic": topic,
                    "event_type": event_type,
                    "payload": dict(payload),
                    "event_id": event_id,
                    "correlation_id": correlation_id,
                    "partition_key": partition_key,
                }
            )
        return self._accept
