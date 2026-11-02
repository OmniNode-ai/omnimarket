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
import os
from collections.abc import AsyncGenerator, Awaitable, Callable
from importlib.metadata import entry_points
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import pytest
import pytest_asyncio
import yaml
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
from omnibase_infra.event_bus.models.config.model_kafka_event_bus_config import (
    ModelKafkaEventBusConfig,
)
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.nodes.node_prompt_cost_estimate_compute import (
    EnumExportFormat,
    HandlerPromptCostEstimate,
    ModelPromptCostEstimateRequest,
    ModelPromptCostEstimateResult,
)
from omnimarket.nodes.node_prompt_cost_estimate_compute.handlers.contract_config import (
    OVERLAY_ID,
    load_config,
    load_topics,
)
from omnimarket.nodes.node_prompt_cost_estimate_compute.models import (
    ModelPromptCostEstimateTopics,
)
from tests.bus_lane_guard import assert_broker_in_declared_lane, resolve_test_bus_broker
from tests.node_overlay_support import install_node_overlay

FIXTURES = Path(__file__).parent / "fixtures"


@pytest_asyncio.fixture
async def local_redpanda_bus() -> AsyncGenerator[EventBusKafka, None]:
    """A real, separately declared test broker; never the ambient broker variable.

    The optional overlay follows tests.bus_lane_guard's lane declarations and
    adds a typed Kafka configuration. Without it the test uses the repository's
    kafka_integration_bus fixture. A configured but unreachable broker fails.
    """
    path = Path(os.environ["PROMPT_COST_TEST_BUS_OVERLAY"])
    broker = resolve_test_bus_broker(declaration_path=path, overlay_path=path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    config = ModelKafkaEventBusConfig.model_validate(raw["bus"])
    assert config.bootstrap_servers == broker
    assert_broker_in_declared_lane(
        config.bootstrap_servers, declaration_path=path, overlay_path=path
    )
    bus = EventBusKafka(config=config)
    await bus.start()
    try:
        yield bus
    finally:
        await bus.close()


@pytest.fixture
def profile_bus(request: pytest.FixtureRequest) -> ProtocolBus:
    fixture = (
        "local_redpanda_bus"
        if os.environ.get("PROMPT_COST_TEST_BUS_OVERLAY")
        else "kafka_integration_bus"
    )
    bus: ProtocolBus = request.getfixturevalue(fixture)
    return bus


def install_zero_spread(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    spread = load_config(environ={}).saving_range.model_dump(mode="json")
    for levels in spread.values():
        for level in levels:
            levels[level] = "0"
    install_node_overlay(monkeypatch, tmp_path, OVERLAY_ID, {"saving_range": spread})


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
        ModelPromptCostEstimateRequest(
            consent_given=True,
            export_format=EnumExportFormat.CSV,
            content="prompt,model,response\nwrite a python function,gpt-4o,an answer\n",
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
def test_node_is_registered_for_runtime_dispatch() -> None:
    matches = entry_points(group="onex.nodes").select(
        name="node_prompt_cost_estimate_compute"
    )
    assert len(matches) == 1
    assert next(iter(matches)).value == (
        "omnimarket.nodes.node_prompt_cost_estimate_compute"
    )


@pytest.mark.unit
@pytest.mark.parametrize("zero_spread", [False, True])
async def test_in_memory_profile_leg(
    zero_spread: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    if zero_spread:
        install_zero_spread(monkeypatch, tmp_path)
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
    assert over_bus[3].prompts_tokens_estimated == 1
    assert (over_bus[0].saving_usd_low == over_bus[0].saving_usd_high) is zero_spread


@pytest.mark.kafka
@pytest.mark.integration
@pytest.mark.parametrize("zero_spread", [False, True])
async def test_two_profile_same_result(
    profile_bus: ProtocolBus,
    zero_spread: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    if zero_spread:
        install_zero_spread(monkeypatch, tmp_path)
    memory = EventBusInmemory(environment="test", group="prompt-cost-estimate")
    await memory.start()
    try:
        in_memory = await run_profile(memory)
    finally:
        await memory.close()
    redpanda = await run_profile(profile_bus)
    assert redpanda == in_memory
    assert redpanda[0].prompts_priced == 3, "falsifier: the estimate priced rows"
    assert redpanda[3].prompts_tokens_estimated == 1
    assert (redpanda[0].saving_usd_low == redpanda[0].saving_usd_high) is zero_spread
