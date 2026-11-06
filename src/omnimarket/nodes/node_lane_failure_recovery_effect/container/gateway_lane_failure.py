# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed RPC through canonical node dispatch; deployment binds every endpoint."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from omnibase_core.cli.cli_run_node import publish_and_poll

from omnimarket.handlers.bus_binding.node_broker import resolve_dialable_broker
from omnimarket.models.bus_binding.model_node_broker import ModelNodeBroker
from omnimarket.models.error_normalizer import (
    ModelErrorClassNormalizeRequest,
    ModelErrorClassNormalizeResult,
)
from omnimarket.models.model_lane_failure import (
    ModelLaneFailureAdmission,
    ModelLaneFailureAdmissionRequest,
    ModelLaneFailureBindings,
    ModelLaneFailureEndpoint,
    ModelLaneFailureOwnerReply,
    ModelLaneFailureOwnerRequest,
    ModelLaneRecoveryAck,
    ModelLaneRecoveryContribution,
)


class LaneFailureGateway:
    def __init__(
        self,
        bindings: ModelLaneFailureBindings,
        *,
        publish: Callable[..., dict[str, Any] | None] = publish_and_poll,
        broker: Callable[[str], ModelNodeBroker] = resolve_dialable_broker,
        call: Callable[
            [ModelLaneFailureEndpoint, dict[str, Any]], Awaitable[dict[str, Any]]
        ]
        | None = None,
    ) -> None:
        self.bindings = bindings
        self.publish = publish
        self.broker = broker
        self.call = call if call is not None else self._threaded_call

    async def _threaded_call(
        self, endpoint: ModelLaneFailureEndpoint, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._call, endpoint, payload)

    def _call(
        self, endpoint: ModelLaneFailureEndpoint, payload: dict[str, Any]
    ) -> dict[str, Any]:
        binding = self.broker(endpoint.node)
        if binding.kind != "kafka":
            raise ValueError("in-memory execution requires an injected runtime gateway")
        response = self.publish(
            node_id=endpoint.node,
            payload=payload,
            timeout=self.bindings.timeout_seconds,
            bootstrap_servers=binding.bootstrap_servers or "",
            command_topic=endpoint.command_topic,
            response_topic=endpoint.terminal_topic,
            client_security=binding.client_security(),
        )
        if response is None:
            raise TimeoutError(
                f"{endpoint.node}: no terminal reply; replay with the same key"
            )
        result = response.get("payload", response)
        if not isinstance(result, dict):
            raise ValueError(f"{endpoint.node}: terminal payload must be an object")
        return result

    async def normalize(
        self, request: ModelErrorClassNormalizeRequest
    ) -> ModelErrorClassNormalizeResult:
        return ModelErrorClassNormalizeResult.model_validate(
            await self.call(self.bindings.normalizer, request.model_dump(mode="json"))
        )

    async def admit(
        self, request: ModelLaneFailureAdmissionRequest
    ) -> ModelLaneFailureAdmission:
        return ModelLaneFailureAdmission.model_validate(
            await self.call(self.bindings.admission, request.model_dump(mode="json"))
        )

    async def owner(
        self, request: ModelLaneFailureOwnerRequest
    ) -> ModelLaneFailureOwnerReply:
        return ModelLaneFailureOwnerReply.model_validate(
            await self.call(self.bindings.owner, request.model_dump(mode="json"))
        )

    async def contribute(
        self, request: ModelLaneRecoveryContribution
    ) -> ModelLaneRecoveryAck:
        return ModelLaneRecoveryAck.model_validate(
            await self.call(self.bindings.recovery, request.model_dump(mode="json"))
        )
