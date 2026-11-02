# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node boots and answers on the in-memory binding and on the Redpanda overlay binding.

Only the overlay's ``transport`` differs. The request goes onto the contract's command
topic, the binding runs the one handler, and the verdict is read off the decided topic;
it must equal the in-process verdict. The Redpanda case runs against the declared test
lane and is opt-in (kafka and integration markers), like the other durable-bus proofs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_infra.event_bus.models.model_event_headers import ModelEventHeaders
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.nodes.node_question_guard_compute import NodeQuestionGuardCompute
from omnimarket.nodes.node_question_guard_compute.models import (
    EnumQuestionGuardTransport,
    EnumQuestionGuardVerdict,
    ModelQuestionGuardRequest,
    ModelQuestionGuardResult,
    ModelQuestionGuardTopics,
)
from omnimarket.nodes.node_question_guard_compute.overlay import (
    load_question_guard_rules,
    load_topics,
    load_transport,
)
from omnimarket.nodes.node_question_guard_compute.protocols import (
    ProtocolQuestionGuardBus,
)
from tests.node_overlay_support import install_node_overlay

from .test_question_guard import RESTORE_ASK, RULING_ROWS, SUBAGENT_ASK, _recorded


class InMemoryBus:
    """The in-memory binding: a topic is a list of subscribers, publishing calls them in order."""

    def __init__(self) -> None:
        self.subscribers: dict[
            str, list[Callable[[ModelEventMessage], Awaitable[None]]]
        ] = {}

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> object:
        message = ModelEventMessage(
            topic=topic,
            key=key,
            value=value,
            headers=ModelEventHeaders(
                source="question-guard-test",
                event_type=topic,
                timestamp=datetime.now(UTC),
            ),
        )
        for callback in list(self.subscribers.get(topic, [])):
            await callback(message)
        return None

    async def subscribe_ungrouped(
        self,
        topic: str,
        on_message: Callable[[ModelEventMessage], Awaitable[None]],
    ) -> Callable[[], Awaitable[None]]:
        self.subscribers.setdefault(topic, []).append(on_message)

        async def unsubscribe() -> None:
            self.subscribers[topic].remove(on_message)

        return unsubscribe


def _scenarios() -> dict[str, str]:
    recorded = _recorded()
    return {
        "real-options": recorded["real_options"][0]["text"],
        "no-choice": recorded["no_choice"][0]["text"],
        "restore": RESTORE_ASK,
        "subagents": SUBAGENT_ASK,
    }


def test_contract_topics() -> None:
    assert load_topics() == ModelQuestionGuardTopics(
        command="onex.cmd.omnimarket.question-guard-requested.v1",
        decided="onex.evt.omnimarket.question-guard-decided.v1",
    )


def _unique(base: ModelQuestionGuardTopics) -> ModelQuestionGuardTopics:
    suffix = uuid4().hex[:12]
    return ModelQuestionGuardTopics(
        command=f"{base.command.removesuffix('.v1')}.{suffix}.v1",
        decided=f"{base.decided.removesuffix('.v1')}.{suffix}.v1",
    )


async def _decide_over(
    bus: ProtocolQuestionGuardBus,
    topics: ModelQuestionGuardTopics,
    node: NodeQuestionGuardCompute,
    request: ModelQuestionGuardRequest,
) -> ModelQuestionGuardResult:
    decided: asyncio.Queue[ModelQuestionGuardResult] = asyncio.Queue()

    async def on_command(message: ModelEventMessage) -> None:
        result = node.handle(
            ModelQuestionGuardRequest.model_validate_json(message.value)
        )
        await bus.publish(topics.decided, None, result.model_dump_json().encode())

    async def observe(message: ModelEventMessage) -> None:
        await decided.put(ModelQuestionGuardResult.model_validate_json(message.value))

    stop_observing = await bus.subscribe_ungrouped(topics.decided, observe)
    stop_binding = await bus.subscribe_ungrouped(topics.command, on_command)
    try:
        await bus.publish(topics.command, None, request.model_dump_json().encode())
        return await asyncio.wait_for(decided.get(), timeout=30)
    finally:
        await stop_binding()
        await stop_observing()


async def _assert_parity(
    bus: ProtocolQuestionGuardBus,
    transport: EnumQuestionGuardTransport,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_node_overlay(
        monkeypatch, tmp_path, "question_guard", {"transport": transport.value}
    )
    assert load_transport() is transport, "the overlay selects the bus binding"
    node = NodeQuestionGuardCompute(rules=load_question_guard_rules())
    topics = _unique(load_topics())
    refused = 0
    for name, text in _scenarios().items():
        request = ModelQuestionGuardRequest(reply_text=text, ruling_rows=RULING_ROWS)
        direct = node.handle(request)
        over_bus = await _decide_over(bus, topics, node, request)
        assert over_bus == direct, name
        refused += direct.verdict is EnumQuestionGuardVerdict.REFUSE
    assert refused == 2, "falsifier: the two refusal scenarios must refuse"


async def test_question_guard_answers_on_the_in_memory_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    await _assert_parity(
        InMemoryBus(), EnumQuestionGuardTransport.IN_MEMORY, monkeypatch, tmp_path
    )


@pytest.mark.kafka
@pytest.mark.integration
async def test_question_guard_answers_on_the_redpanda_overlay_binding(
    kafka_integration_bus: ProtocolQuestionGuardBus,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    await _assert_parity(
        kafka_integration_bus,
        EnumQuestionGuardTransport.REDPANDA,
        monkeypatch,
        tmp_path,
    )
