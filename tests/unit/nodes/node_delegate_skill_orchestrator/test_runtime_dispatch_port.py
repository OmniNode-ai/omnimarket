# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for RuntimeDelegationDispatchPort."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.adapters.codex.runtime_client import (
    ModelDispatchBusCommand,
    ModelDispatchBusTerminalResult,
    default_command_topic,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    RuntimeDelegationDispatchPort,
)


async def _publish_terminal_response(
    bus: EventBusInmemory,
    message: ModelEventMessage,
    *,
    status: str,
    payload: dict[str, object],
    error_message: str | None = None,
    received_commands: list[ModelDispatchBusCommand] | None = None,
) -> None:
    envelope = ModelEventEnvelope[ModelDispatchBusCommand].model_validate_json(
        message.value
    )
    if received_commands is not None:
        received_commands.append(envelope.payload)
    terminal = ModelDispatchBusTerminalResult(
        correlation_id=envelope.payload.correlation_id,
        status=status,
        payload=payload,
        error_message=error_message,
    )
    response = ModelEventEnvelope[ModelDispatchBusTerminalResult](
        payload=terminal,
        correlation_id=terminal.correlation_id,
        envelope_timestamp=datetime.now(UTC),
        event_type=envelope.payload.response_topic,
        source_tool="delegate-skill-port-test",
    )
    await bus.publish(
        envelope.payload.response_topic,
        None,
        response.model_dump_json().encode("utf-8"),
        None,
    )


@pytest.mark.unit
async def test_runtime_dispatch_port_round_trips_internal_delegation_result() -> None:
    bus = EventBusInmemory(environment="test", group="delegate-skill-port")
    received_commands: list[ModelDispatchBusCommand] = []
    original_correlation_id = uuid4()
    await bus.start()

    async def on_command(message: ModelEventMessage) -> None:
        await _publish_terminal_response(
            bus,
            message,
            status="completed",
            payload={
                "correlation_id": str(original_correlation_id),
                "task_type": "test",
                "model_used": "Qwen3-Coder-30B",
                "endpoint_url": "https://qwen.local",
                "content": "delegated content",
                "quality_passed": True,
                "quality_score": 1.0,
                "latency_ms": 42,
                "prompt_tokens": 8,
                "completion_tokens": 13,
                "total_tokens": 21,
                "fallback_to_claude": False,
                "failure_reason": "",
            },
            received_commands=received_commands,
        )

    try:
        await bus.subscribe(
            default_command_topic(),
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
    assert len(received_commands) == 1

    command = received_commands[0]
    assert command.command_name == "node_delegation_orchestrator"
    assert command.correlation_id != original_correlation_id
    assert UUID(str(command.payload["correlation_id"])) == command.correlation_id
    assert command.payload["task_type"] == "test"
    assert command.payload["source_file_path"] == "tests/fixtures/example.py"
    assert command.payload["source_session_id"] == "session-1"
    assert command.payload["max_tokens"] == 512
    assert command.payload["quality_contract_mode"] == "replace_task_class"
    assert command.payload["acceptance_criteria"] == ["exactly_two_sentences"]
    assert command.payload["emitted_at"]


@pytest.mark.unit
async def test_runtime_dispatch_port_unwraps_delegation_event_payload() -> None:
    bus = EventBusInmemory(environment="test", group="delegate-skill-port")
    original_correlation_id = uuid4()
    await bus.start()

    async def on_command(message: ModelEventMessage) -> None:
        await _publish_terminal_response(
            bus,
            message,
            status="failed",
            payload={
                "topic": "onex.evt.omnibase-infra.delegation-failed.v1",
                "payload": {
                    "correlation_id": str(original_correlation_id),
                    "task_type": "test",
                    "model_used": "",
                    "endpoint_url": "",
                    "content": "",
                    "quality_passed": False,
                    "quality_score": 0.0,
                    "latency_ms": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "fallback_to_claude": False,
                    "failure_reason": "configured endpoint missing",
                },
            },
            error_message="configured endpoint missing",
        )

    try:
        await bus.subscribe(
            default_command_topic(),
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
