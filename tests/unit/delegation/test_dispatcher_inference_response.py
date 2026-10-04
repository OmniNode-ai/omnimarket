# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Direct tests for the delegation inference response dispatcher."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from omnibase_core.enums import EnumNodeKind
from omnibase_core.models.delegation.wire import ModelBaselineIntent
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.protocols.event_bus.protocol_event_bus import ProtocolEventBus
from omnibase_infra.enums import EnumDispatchStatus, EnumMessageCategory
from omnibase_infra.errors import InfraUnavailableError
from omnibase_infra.models.dispatch.model_dispatch_result import ModelDispatchResult
from pydantic import BaseModel

from omnimarket.nodes.node_delegation_orchestrator.dispatchers.dispatcher_inference_response import (
    TOPIC_ID_INFERENCE_RESPONSE,
    DispatcherInferenceResponse,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)

pytestmark = pytest.mark.unit


def _make_mock_bus() -> MagicMock:
    bus = MagicMock(spec=ProtocolEventBus)
    bus.publish_envelope = AsyncMock()
    return bus


def _make_envelope(payload: object, correlation_id: UUID) -> ModelEventEnvelope[object]:
    return ModelEventEnvelope(
        envelope_id=uuid4(),
        payload=payload,
        correlation_id=correlation_id,
        envelope_timestamp=datetime.now(UTC),
    )


@pytest.fixture
def response() -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=uuid4(),
        content="def test_x(): pass",
        model_used="qwen3-coder-30b",
        llm_call_id="chatcmpl-test",
        latency_ms=100,
        prompt_tokens=50,
        completion_tokens=20,
        total_tokens=70,
    )


@pytest.fixture
def intents(response: ModelInferenceResponseData) -> list[BaseModel]:
    return [
        ModelBaselineIntent(
            correlation_id=response.correlation_id,
            task_type="test",
            baseline_cost_usd=0.01,
        ),
        ModelBaselineIntent(
            correlation_id=response.correlation_id,
            task_type="document",
            baseline_cost_usd=0.02,
        ),
    ]


@pytest.fixture
def handler(intents: list[BaseModel]) -> MagicMock:
    stub = MagicMock(spec=HandlerDelegationWorkflow)
    stub.handle_inference_response.return_value = intents
    return stub


@pytest.fixture
def dispatcher(handler: MagicMock) -> DispatcherInferenceResponse:
    return DispatcherInferenceResponse(handler, event_bus=None)


def test_dispatcher_id(dispatcher: DispatcherInferenceResponse) -> None:
    assert dispatcher.dispatcher_id == "dispatcher.delegation.inference-response"


def test_category(dispatcher: DispatcherInferenceResponse) -> None:
    assert dispatcher.category == EnumMessageCategory.EVENT


def test_message_types(dispatcher: DispatcherInferenceResponse) -> None:
    assert dispatcher.message_types == {
        "ModelInferenceResponseData",
        "omnibase-infra.inference-response",
    }


def test_node_kind(dispatcher: DispatcherInferenceResponse) -> None:
    assert dispatcher.node_kind == EnumNodeKind.ORCHESTRATOR


@pytest.mark.asyncio
@pytest.mark.parametrize("with_bus", [False, True], ids=["no-bus", "stub-bus"])
@pytest.mark.parametrize("as_dict", [False, True], ids=["model", "dict"])
async def test_handle_response(
    handler: MagicMock,
    response: ModelInferenceResponseData,
    intents: list[BaseModel],
    with_bus: bool,
    as_dict: bool,
) -> None:
    bus = _make_mock_bus() if with_bus else None
    dispatcher = DispatcherInferenceResponse(handler, event_bus=bus)
    envelope: ModelEventEnvelope[object] | dict[str, object]
    if as_dict:
        envelope = {
            "payload": response.model_dump(mode="json"),
            "__debug_trace": {"correlation_id": str(response.correlation_id)},
        }
    else:
        envelope = _make_envelope(response, response.correlation_id)

    result = await dispatcher.handle(envelope)

    assert isinstance(result, ModelDispatchResult)
    assert result.status == EnumDispatchStatus.SUCCESS
    assert result.topic == TOPIC_ID_INFERENCE_RESPONSE
    assert result.dispatcher_id == dispatcher.dispatcher_id
    assert result.correlation_id == response.correlation_id
    assert result.output_events == intents
    assert result.error_message is None
    assert result.completed_at is not None
    assert result.completed_at >= result.started_at
    assert result.duration_ms is not None
    assert result.duration_ms >= 0.0
    handler.handle_inference_response.assert_called_once_with(response)
    received = handler.handle_inference_response.call_args.args[0]
    assert isinstance(received, ModelInferenceResponseData)
    if as_dict:
        assert received is not response
    else:
        assert received is response
    if bus is not None:
        bus.publish_envelope.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["unexpected", 42, [], None])
async def test_invalid_payload_type(
    dispatcher: DispatcherInferenceResponse,
    handler: MagicMock,
    payload: object,
) -> None:
    correlation_id = uuid4()

    result = await dispatcher.handle(_make_envelope(payload, correlation_id))

    assert isinstance(result, ModelDispatchResult)
    assert result.status == EnumDispatchStatus.INVALID_MESSAGE
    assert result.error_message == (
        f"Expected ModelInferenceResponseData, got {type(payload).__name__}"
    )
    assert result.correlation_id == correlation_id
    assert result.output_events == []
    assert result.duration_ms == 0.0
    assert result.completed_at == result.started_at
    handler.handle_inference_response.assert_not_called()
    assert dispatcher.get_circuit_breaker_state()["failures"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"content": "missing required fields"}])
async def test_invalid_dict_payload(
    dispatcher: DispatcherInferenceResponse,
    handler: MagicMock,
    payload: dict[str, object],
) -> None:
    correlation_id = uuid4()

    result = await dispatcher.handle(_make_envelope(payload, correlation_id))

    assert result.status == EnumDispatchStatus.INVALID_MESSAGE
    assert result.error_message
    assert "ModelInferenceResponseData" in result.error_message
    assert result.correlation_id == correlation_id
    assert result.output_events == []
    handler.handle_inference_response.assert_not_called()
    assert dispatcher.get_circuit_breaker_state()["failures"] == 0


@pytest.mark.asyncio
async def test_handler_failure(
    dispatcher: DispatcherInferenceResponse,
    handler: MagicMock,
    response: ModelInferenceResponseData,
) -> None:
    handler.handle_inference_response.side_effect = RuntimeError("handler failed")

    result = await dispatcher.handle(_make_envelope(response, response.correlation_id))

    assert result.status == EnumDispatchStatus.HANDLER_ERROR
    assert result.error_message == "RuntimeError: handler failed"
    assert result.correlation_id == response.correlation_id
    assert result.output_events == []
    handler.handle_inference_response.assert_called_once_with(response)
    state = dispatcher.get_circuit_breaker_state()
    assert state["failures"] == 1
    assert state["state"] == "closed"


@pytest.mark.asyncio
async def test_success_resets_circuit_breaker(
    dispatcher: DispatcherInferenceResponse,
    handler: MagicMock,
    response: ModelInferenceResponseData,
    intents: list[BaseModel],
) -> None:
    handler.handle_inference_response.side_effect = [
        RuntimeError("handler failed"),
        intents,
    ]
    envelope = _make_envelope(response, response.correlation_id)

    failed = await dispatcher.handle(envelope)
    assert failed.status == EnumDispatchStatus.HANDLER_ERROR
    assert dispatcher.get_circuit_breaker_state()["failures"] == 1

    recovered = await dispatcher.handle(envelope)

    assert recovered.status == EnumDispatchStatus.SUCCESS
    assert recovered.output_events == intents
    assert handler.handle_inference_response.call_count == 2
    state = dispatcher.get_circuit_breaker_state()
    assert state["failures"] == 0
    assert state["state"] == "closed"


@pytest.mark.asyncio
async def test_circuit_opens_after_three_failures(
    dispatcher: DispatcherInferenceResponse,
    handler: MagicMock,
    response: ModelInferenceResponseData,
) -> None:
    handler.handle_inference_response.side_effect = RuntimeError("handler failed")
    envelope = _make_envelope(response, response.correlation_id)
    assert dispatcher.get_circuit_breaker_state()["threshold"] == 3

    for failure_count in range(1, 4):
        result = await dispatcher.handle(envelope)
        assert result.status == EnumDispatchStatus.HANDLER_ERROR
        assert result.error_message == "RuntimeError: handler failed"
        assert result.output_events == []
        assert handler.handle_inference_response.call_count == failure_count
        state = dispatcher.get_circuit_breaker_state()
        assert state["failures"] == failure_count
        assert state["state"] == ("open" if failure_count == 3 else "closed")

    handler.handle_inference_response.side_effect = None
    blocked = await dispatcher.handle(envelope)

    assert blocked.status == EnumDispatchStatus.HANDLER_ERROR
    assert blocked.error_message
    assert "Circuit breaker is open" in blocked.error_message
    assert blocked.correlation_id == response.correlation_id
    assert blocked.output_events == []
    assert handler.handle_inference_response.call_count == 3
    state = dispatcher.get_circuit_breaker_state()
    assert state["state"] == "open"
    assert state["failures"] == 3


@pytest.mark.asyncio
async def test_handler_unavailable(
    dispatcher: DispatcherInferenceResponse,
    handler: MagicMock,
    response: ModelInferenceResponseData,
) -> None:
    handler.handle_inference_response.side_effect = InfraUnavailableError(
        "handler unavailable"
    )

    result = await dispatcher.handle(_make_envelope(response, response.correlation_id))

    assert result.status == EnumDispatchStatus.HANDLER_ERROR
    assert result.error_message
    assert "handler unavailable" in result.error_message
    assert result.correlation_id == response.correlation_id
    assert result.output_events == []
    handler.handle_inference_response.assert_called_once_with(response)
