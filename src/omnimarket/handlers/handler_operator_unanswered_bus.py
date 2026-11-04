# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bus dependency adapter for the existing structural detector node.

Both transports use the same typed command and terminal topics from its contract.
The hook hosts the pure compute dependency on the canonical in-memory bus; a lab
falsifier supplies Redpanda. No capture classification or answer inference lives
in this adapter.
"""

from __future__ import annotations

import asyncio
import importlib
from functools import lru_cache
from typing import Any
from uuid import uuid4

from omnimarket.models.model_operator_stop_gate import ModelUnansweredComputeBinding
from omnimarket.models.operator_unanswered import (
    ModelUnansweredRequest,
    ModelUnansweredResult,
)


@lru_cache(maxsize=4)
def _resolve(handler_path: str, input_model: str) -> tuple[Any, Any]:
    module, _, name = handler_path.partition(":")
    handler = getattr(importlib.import_module(module), name)()
    module, _, name = input_model.rpartition(".")
    return handler, getattr(importlib.import_module(module), name)


class HandlerOperatorUnansweredBus:
    """DI for the pure dependency; the effect's claim and start stay on the bus."""

    def __init__(self, binding: ModelUnansweredComputeBinding, bus: Any = None) -> None:
        self.binding = binding
        self.bus = bus

    def handle(self, request: ModelUnansweredRequest) -> ModelUnansweredResult:
        return asyncio.run(self.handle_async(request))

    async def handle_async(
        self, request: ModelUnansweredRequest
    ) -> ModelUnansweredResult:
        from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
        from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

        binding = self.binding
        bus = self.bus or EventBusInmemory(
            environment=binding.group, group=binding.group
        )
        handler, input_type = _resolve(binding.handler, binding.input_model)
        correlation_id = uuid4()
        terminal: asyncio.Future[ModelUnansweredResult] = (
            asyncio.get_running_loop().create_future()
        )

        async def compute(message: Any) -> None:
            try:
                command = ModelEventEnvelope[
                    ModelUnansweredRequest
                ].model_validate_json(message.value)
                result = handler.handle(
                    input_type.model_validate(
                        command.payload.model_dump(mode="json", by_alias=True)
                    )
                )
                response = ModelEventEnvelope[ModelUnansweredResult](
                    correlation_id=command.correlation_id,
                    event_type=".".join(binding.terminal_topic.split(".")[2:-1]),
                    payload=result,
                )
                await bus.publish(
                    binding.terminal_topic,
                    None,
                    response.model_dump_json(by_alias=True).encode(),
                    None,
                )
            except Exception as exc:
                if not terminal.done():
                    terminal.set_exception(exc)

        async def complete(message: Any) -> None:
            response = ModelEventEnvelope[ModelUnansweredResult].model_validate_json(
                message.value
            )
            if response.correlation_id == correlation_id and not terminal.done():
                terminal.set_result(response.payload)

        if self.bus is None:
            await bus.start()
        subscriptions = []
        try:
            subscriptions.append(
                await bus.subscribe(
                    binding.command_topic, group_id=binding.group, on_message=compute
                )
            )
            subscriptions.append(
                await bus.subscribe(
                    binding.terminal_topic,
                    group_id=binding.group + "-terminal-" + correlation_id.hex,
                    on_message=complete,
                )
            )
            async with asyncio.timeout(binding.timeout_seconds):
                command = ModelEventEnvelope[ModelUnansweredRequest](
                    correlation_id=correlation_id,
                    event_type=".".join(binding.command_topic.split(".")[2:-1]),
                    payload=request,
                )
                await bus.publish(
                    binding.command_topic,
                    None,
                    command.model_dump_json(by_alias=True).encode(),
                    None,
                )
                return await terminal
        finally:
            for subscription in reversed(subscriptions):
                if callable(subscription):
                    await subscription()
                else:
                    await subscription.stop()
            if self.bus is None:
                await bus.close()
