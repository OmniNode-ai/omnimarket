# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The same estimate on the in-memory bus and on the Redpanda bus (OMN-22126, AC4).

The request goes onto the contract's command topic, a binding runs the one
handler the way the runtime does for a contract, and the result is read off the
contract's terminal topic. Only the bus differs between the two profiles.

``test_two_profile_same_result`` runs both legs and compares them; it needs the
declared test broker, so it carries the kafka and integration markers like the
other durable-bus proofs and is deselected, not skipped, where no broker runs.
``test_in_memory_profile_leg`` is the in-memory leg on its own, compared with a
direct call of the handler, and runs everywhere.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import pytest
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.nodes.node_prompt_cost_estimate_compute import (
    EnumExportFormat,
    HandlerPromptCostEstimate,
    ModelPromptCostEstimateRequest,
    ModelPromptCostEstimateResult,
)
from omnimarket.nodes.node_prompt_cost_estimate_compute.handlers.contract_config import (
    load_topics,
)
from omnimarket.nodes.node_prompt_cost_estimate_compute.models import (
    ModelPromptCostEstimateTopics,
)

FIXTURES = Path(__file__).parent / "fixtures"


class ProtocolBus(Protocol):
    async def publish(self, topic: str, key: bytes | None, value: bytes) -> object: ...

    async def subscribe_ungrouped(
        self,
        topic: str,
        on_message: Callable[[ModelEventMessage], Awaitable[None]],
    ) -> Callable[[], Awaitable[None]]: ...


def requests() -> list[ModelPromptCostEstimateRequest]:
    owner = (FIXTURES / "cloud_owner_tokens_in_out.csv").read_text(encoding="utf-8")
    cofounder = (FIXTURES / "cofounder_no_model.csv").read_text(encoding="utf-8")
    return [
        ModelPromptCostEstimateRequest(
            consent_given=True, export_format=EnumExportFormat.CSV, content=owner
        ),
        ModelPromptCostEstimateRequest(
            consent_given=True,
            export_format=EnumExportFormat.CSV,
            content=cofounder,
            declared_model="gpt-4o",
        ),
        ModelPromptCostEstimateRequest(
            consent_given=True, export_format=EnumExportFormat.CSV, content=cofounder
        ),
    ]


def unique_topics(base: ModelPromptCostEstimateTopics) -> ModelPromptCostEstimateTopics:
    """Topics unique to this run, so a topic holds no record from an earlier run."""
    suffix = uuid4().hex[:12]
    return ModelPromptCostEstimateTopics(
        command=f"{base.command.removesuffix('.v1')}.{suffix}.v1",
        completed=f"{base.completed.removesuffix('.v1')}.{suffix}.v1",
    )


async def estimate_over(
    bus: ProtocolBus, request: ModelPromptCostEstimateRequest
) -> ModelPromptCostEstimateResult:
    topics = unique_topics(load_topics())
    handler = HandlerPromptCostEstimate()
    completed: asyncio.Queue[ModelPromptCostEstimateResult] = asyncio.Queue()

    async def on_command(message: ModelEventMessage) -> None:
        result = handler.handle(
            ModelPromptCostEstimateRequest.model_validate_json(message.value)
        )
        await bus.publish(topics.completed, None, result.model_dump_json().encode())

    async def observe(message: ModelEventMessage) -> None:
        await completed.put(
            ModelPromptCostEstimateResult.model_validate_json(message.value)
        )

    stop_observing = await bus.subscribe_ungrouped(topics.completed, observe)
    stop_binding = await bus.subscribe_ungrouped(topics.command, on_command)
    try:
        await bus.publish(topics.command, None, request.model_dump_json().encode())
        return await asyncio.wait_for(completed.get(), timeout=30)
    finally:
        await stop_binding()
        await stop_observing()


async def run_profile(bus: ProtocolBus) -> list[ModelPromptCostEstimateResult]:
    return [await estimate_over(bus, request) for request in requests()]


@pytest.mark.unit
def test_the_contract_declares_the_command_and_result_topics() -> None:
    assert load_topics() == ModelPromptCostEstimateTopics(
        command="onex.cmd.omnimarket.prompt-cost-estimate-requested.v1",
        completed="onex.evt.omnimarket.prompt-cost-estimate-completed.v1",
    )


@pytest.mark.unit
async def test_in_memory_profile_leg() -> None:
    bus = EventBusInmemory(environment="test", group="prompt-cost-estimate")
    await bus.start()
    try:
        over_bus = await run_profile(bus)
    finally:
        await bus.close()
    direct = [HandlerPromptCostEstimate().handle(request) for request in requests()]
    assert over_bus == direct
    assert over_bus[0].prompts_priced == 3, "falsifier: the estimate priced rows"
    assert over_bus[2].prompts_unpriced == 3, "falsifier: the model-less CSV"


@pytest.mark.kafka
@pytest.mark.integration
async def test_two_profile_same_result(kafka_integration_bus: ProtocolBus) -> None:
    memory = EventBusInmemory(environment="test", group="prompt-cost-estimate")
    await memory.start()
    try:
        in_memory = await run_profile(memory)
    finally:
        await memory.close()
    redpanda = await run_profile(kafka_integration_bus)
    assert redpanda == in_memory
    assert redpanda[0].prompts_priced == 3, "falsifier: the estimate priced rows"
