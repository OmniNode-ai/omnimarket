# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for RuntimeDelegationDispatchPort."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
from omnibase_infra.event_bus.topic_constants import (
    TOPIC_DELEGATION_COMPLETED,
    TOPIC_DELEGATION_FAILED,
    TOPIC_DELEGATION_REQUEST,
)

from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    RuntimeDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_event import (
    ModelDelegationEvent,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationResult,
)


async def _publish_terminal_response(
    bus: EventBusInmemory,
    message: ModelEventMessage,
    *,
    topic: str,
    result: ModelDelegationResult,
    received_requests: list[ModelDelegationRequest] | None = None,
) -> None:
    envelope = ModelEventEnvelope[ModelDelegationRequest].model_validate_json(
        message.value
    )
    if received_requests is not None:
        received_requests.append(envelope.payload)
    terminal = ModelDelegationEvent(
        topic=topic,
        payload=result,
    )
    response = ModelEventEnvelope[ModelDelegationEvent](
        payload=terminal,
        correlation_id=result.correlation_id,
        envelope_timestamp=datetime.now(UTC),
        event_type=topic,
        source_tool="delegate-skill-port-test",
    )
    await bus.publish(
        topic,
        None,
        response.model_dump_json().encode("utf-8"),
        None,
    )


@pytest.mark.unit
async def test_runtime_dispatch_port_round_trips_internal_delegation_result() -> None:
    bus = EventBusInmemory(environment="test", group="delegate-skill-port")
    received_requests: list[ModelDelegationRequest] = []
    original_correlation_id = uuid4()
    await bus.start()

    async def on_command(message: ModelEventMessage) -> None:
        await _publish_terminal_response(
            bus,
            message,
            topic=TOPIC_DELEGATION_COMPLETED,
            result=ModelDelegationResult(
                correlation_id=original_correlation_id,
                task_type="test",
                model_used="Qwen3-Coder-30B",
                endpoint_url="https://qwen.local",
                content="delegated content",
                quality_passed=True,
                quality_score=1.0,
                latency_ms=42,
                prompt_tokens=8,
                completion_tokens=13,
                total_tokens=21,
                fallback_to_claude=False,
                failure_reason="",
            ),
            received_requests=received_requests,
        )

    try:
        await bus.subscribe(
            TOPIC_DELEGATION_REQUEST,
            group_id=f"delegate-skill-port-test-{uuid4()}",
            on_message=on_command,
        )
        port = RuntimeDelegationDispatchPort(event_bus=bus)
        result = await port.dispatch(
            prompt="Write tests",
            task_type="test",
            correlation_id=original_correlation_id,
            max_tokens=512,
            source_file_path="tests/fixtures/example.py",
            source_session_id="session-1",
            wait=True,
            quality_contract_mode="replace_task_class",
            acceptance_criteria=("exactly_two_sentences",),
        )
    finally:
        await bus.close()

    assert result["status"] == "completed"
    assert result["model_used"] == "Qwen3-Coder-30B"
    assert result["content"] == "delegated content"
    assert len(received_requests) == 1

    request = received_requests[0]
    assert request.correlation_id == original_correlation_id
    assert request.task_type == "test"
    assert request.source_file_path == "tests/fixtures/example.py"
    assert request.source_session_id == "session-1"
    assert request.max_tokens == 512
    assert request.quality_contract_mode == "replace_task_class"
    assert request.acceptance_criteria == ("exactly_two_sentences",)
    assert request.emitted_at


@pytest.mark.unit
async def test_runtime_dispatch_port_unwraps_delegation_event_payload() -> None:
    bus = EventBusInmemory(environment="test", group="delegate-skill-port")
    original_correlation_id = uuid4()
    await bus.start()

    async def on_command(message: ModelEventMessage) -> None:
        await _publish_terminal_response(
            bus,
            message,
            topic=TOPIC_DELEGATION_FAILED,
            result=ModelDelegationResult(
                correlation_id=original_correlation_id,
                task_type="test",
                model_used="",
                endpoint_url="",
                content="",
                quality_passed=False,
                quality_score=0.0,
                latency_ms=0,
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                fallback_to_claude=False,
                failure_reason="configured endpoint missing",
            ),
        )

    try:
        await bus.subscribe(
            TOPIC_DELEGATION_REQUEST,
            group_id=f"delegate-skill-port-test-{uuid4()}",
            on_message=on_command,
        )
        port = RuntimeDelegationDispatchPort(event_bus=bus)
        result = await port.dispatch(
            prompt="Write tests",
            task_type="test",
            correlation_id=original_correlation_id,
            max_tokens=512,
            source_file_path="tests/fixtures/example.py",
            source_session_id="session-1",
            wait=True,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
        )
    finally:
        await bus.close()

    assert result["status"] == "failed"
    assert result["failure_reason"] == "configured endpoint missing"
    assert result["error_message"] == "configured endpoint missing"
    assert result["terminal_topic"] == "onex.evt.omnibase-infra.delegation-failed.v1"


@pytest.mark.unit
async def test_runtime_dispatch_port_ignores_early_empty_pattern_b_terminal() -> None:
    bus = EventBusInmemory(environment="test", group="delegate-skill-port")
    original_correlation_id = uuid4()
    await bus.start()

    async def on_command(message: ModelEventMessage) -> None:
        await bus.publish(
            "onex.evt.omnibase-infra.pattern-b-dispatch-completed.v1",
            None,
            b'{"payload":{"status":"completed","payload":{}}}',
            None,
        )
        await _publish_terminal_response(
            bus,
            message,
            topic=TOPIC_DELEGATION_FAILED,
            result=ModelDelegationResult(
                correlation_id=original_correlation_id,
                task_type="test",
                model_used="Qwen3-Coder-30B",
                endpoint_url="https://qwen.local",
                content="scored failure content",
                quality_passed=False,
                quality_score=0.0,
                latency_ms=84,
                prompt_tokens=68,
                completion_tokens=17,
                total_tokens=85,
                fallback_to_claude=False,
                failure_reason="TASK_MISMATCH",
                tokens_to_compliance=85,
                compliance_attempts=1,
            ),
        )

    try:
        await bus.subscribe(
            TOPIC_DELEGATION_REQUEST,
            group_id=f"delegate-skill-port-test-{uuid4()}",
            on_message=on_command,
        )
        port = RuntimeDelegationDispatchPort(event_bus=bus)
        result = await port.dispatch(
            prompt="Write tests",
            task_type="test",
            correlation_id=original_correlation_id,
            max_tokens=512,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
        )
    finally:
        await bus.close()

    assert result["status"] == "failed"
    assert result["content"] == "scored failure content"
    assert result["quality_score"] == 0.0
    assert result["prompt_tokens"] == 68
    assert result["completion_tokens"] == 17
    assert result["total_tokens"] == 85
    assert result["tokens_to_compliance"] == 85
    assert result["compliance_attempts"] == 1
