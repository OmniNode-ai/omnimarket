# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for HandlerObservabilitySinkEffect.

Tests cover:
  - Side-effect-free runtime smoke with both sinks disabled
  - Explicit failure when a requested sink adapter is not injected
  - Typed adapter persistence
  - Input model construction and validation
  - Output model construction
  - Contract YAML is loadable and has expected fields
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml

from omnimarket.nodes.node_observability_sink_effect.handlers.handler_observability_sink_effect import (
    HandlerObservabilitySinkEffect,
)
from omnimarket.nodes.node_observability_sink_effect.models.model_observability_sink_input import (
    ModelActionEvent,
    ModelObservabilitySinkInput,
)
from omnimarket.nodes.node_observability_sink_effect.models.model_observability_sink_output import (
    ModelObservabilitySinkOutput,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
_CORR_ID = uuid4()
_SESSION_ID = uuid4()
_EVENT_ID = uuid4()

CONTRACT_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_observability_sink_effect"
    / "contract.yaml"
)


def _make_action_event(
    event_id: UUID | None = None,
    agent_name: str = "agent-test",
    action_type: str = "tool_call",
    action_name: str = "Read",
) -> ModelActionEvent:
    return ModelActionEvent(
        event_id=event_id or uuid4(),
        agent_name=agent_name,
        action_type=action_type,
        action_name=action_name,
        action_details={"file_path": "/tmp/test.py"},
        duration_ms=12,
        emitted_at=_NOW,
    )


def _make_input(
    events: tuple[ModelActionEvent, ...] | None = None,
) -> ModelObservabilitySinkInput:
    return ModelObservabilitySinkInput(
        correlation_id=_CORR_ID,
        session_id=_SESSION_ID,
        events=events if events is not None else (_make_action_event(),),
        sink_kafka=True,
        sink_postgres=True,
        submitted_at=_NOW,
    )


# ---------------------------------------------------------------------------
# Handler behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handler_no_sink_runtime_smoke_has_no_side_effects() -> None:
    """Both sink flags false is the deterministic runtime dispatch smoke path."""
    handler = HandlerObservabilitySinkEffect(clock=lambda: _NOW)
    request = _make_input().model_copy(
        update={"sink_kafka": False, "sink_postgres": False}
    )

    result = await handler.handle(request)

    assert result.correlation_id == request.correlation_id
    assert result.session_id == request.session_id
    assert result.persisted_event_count == 0
    assert result.kafka_trace_ids == ()
    assert result.postgres_row_ids == ()
    assert result.persisted_at == _NOW


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handler_refuses_kafka_without_injected_adapter() -> None:
    handler = HandlerObservabilitySinkEffect()
    request = _make_input().model_copy(update={"sink_postgres": False})

    with pytest.raises(RuntimeError, match="Kafka persistence"):
        await handler.handle(request)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handler_refuses_postgres_without_injected_adapter() -> None:
    handler = HandlerObservabilitySinkEffect()
    request = _make_input().model_copy(update={"sink_kafka": False})

    with pytest.raises(RuntimeError, match="PostgreSQL persistence"):
        await handler.handle(request)


class _KafkaSink:
    def __init__(self) -> None:
        self.events: list[ModelActionEvent] = []

    async def publish_action_event(
        self,
        *,
        correlation_id: UUID,
        session_id: UUID,
        event: ModelActionEvent,
    ) -> str:
        assert correlation_id == _CORR_ID
        assert session_id == _SESSION_ID
        self.events.append(event)
        return f"kafka:{event.event_id}"


class _PostgresSink:
    def __init__(self, row_id: UUID) -> None:
        self.row_id = row_id
        self.events: list[ModelActionEvent] = []

    def insert_action_event(
        self,
        *,
        correlation_id: UUID,
        session_id: UUID,
        event: ModelActionEvent,
    ) -> UUID:
        assert correlation_id == _CORR_ID
        assert session_id == _SESSION_ID
        self.events.append(event)
        return self.row_id


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handler_uses_typed_injected_sink_adapters() -> None:
    row_id = uuid4()
    kafka = _KafkaSink()
    postgres = _PostgresSink(row_id=row_id)
    handler = HandlerObservabilitySinkEffect(
        kafka_sink=kafka,
        postgres_sink=postgres,
        clock=lambda: _NOW,
    )
    event = _make_action_event(event_id=_EVENT_ID)
    request = _make_input(events=(event,))

    result = await handler.handle(request)

    assert kafka.events == [event]
    assert postgres.events == [event]
    assert result.persisted_event_count == 1
    assert result.kafka_trace_ids == (f"kafka:{_EVENT_ID}",)
    assert result.postgres_row_ids == (row_id,)


# ---------------------------------------------------------------------------
# Input model validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_input_model_construction() -> None:
    """ModelObservabilitySinkInput constructs correctly with valid fields."""
    event = _make_action_event()
    inp = _make_input(events=(event,))

    assert inp.correlation_id == _CORR_ID
    assert inp.session_id == _SESSION_ID
    assert len(inp.events) == 1
    assert inp.events[0].event_id == event.event_id
    assert inp.sink_kafka is True
    assert inp.sink_postgres is True


@pytest.mark.unit
def test_input_model_is_frozen() -> None:
    """Input model must be immutable (frozen=True)."""
    inp = _make_input()
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        inp.sink_kafka = False  # type: ignore[misc]


@pytest.mark.unit
def test_input_model_rejects_extra_fields() -> None:
    """Extra fields are rejected (extra='forbid')."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ModelObservabilitySinkInput(
            correlation_id=_CORR_ID,
            session_id=_SESSION_ID,
            events=(_make_action_event(),),
            submitted_at=_NOW,
            unknown_field="bad",  # type: ignore[call-arg]
        )


@pytest.mark.unit
def test_action_event_duration_non_negative() -> None:
    """duration_ms must be >= 0."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ModelActionEvent(
            event_id=uuid4(),
            agent_name="agent-test",
            action_type="tool_call",
            action_name="Read",
            duration_ms=-1,
            emitted_at=_NOW,
        )


# ---------------------------------------------------------------------------
# Output model validation
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_output_model_construction() -> None:
    """ModelObservabilitySinkOutput constructs correctly with valid fields."""
    out = ModelObservabilitySinkOutput(
        correlation_id=_CORR_ID,
        session_id=_SESSION_ID,
        persisted_event_count=3,
        kafka_trace_ids=("offset-1", "offset-2", "offset-3"),
        postgres_row_ids=(uuid4(), uuid4(), uuid4()),
        persisted_at=_NOW,
        error="",
    )
    assert out.persisted_event_count == 3
    assert len(out.kafka_trace_ids) == 3
    assert len(out.postgres_row_ids) == 3
    assert out.error == ""


@pytest.mark.unit
def test_output_model_defaults() -> None:
    """Output model defaults: empty tuples for trace IDs and empty error string."""
    out = ModelObservabilitySinkOutput(
        correlation_id=_CORR_ID,
        session_id=_SESSION_ID,
        persisted_event_count=0,
        persisted_at=_NOW,
    )
    assert out.kafka_trace_ids == ()
    assert out.postgres_row_ids == ()
    assert out.error == ""


@pytest.mark.unit
def test_output_model_is_frozen() -> None:
    """Output model must be immutable (frozen=True)."""
    out = ModelObservabilitySinkOutput(
        correlation_id=_CORR_ID,
        session_id=_SESSION_ID,
        persisted_event_count=0,
        persisted_at=_NOW,
    )
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        out.persisted_event_count = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Handler metadata
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_handler_type_metadata() -> None:
    """Handler declares the correct handler_type and handler_category class attributes."""
    handler = HandlerObservabilitySinkEffect()
    assert handler.handler_type == "node_handler"
    assert handler.handler_category == "effect"


# ---------------------------------------------------------------------------
# Contract YAML
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_contract_yaml_loadable() -> None:
    """contract.yaml must be loadable and contain required top-level keys."""
    assert CONTRACT_PATH.exists(), f"contract.yaml not found at {CONTRACT_PATH}"
    with CONTRACT_PATH.open() as fh:
        contract = yaml.safe_load(fh)

    assert contract["name"] == "node_observability_sink_effect"
    assert contract["node_type"] == "EFFECT_GENERIC"
    assert contract["node_not_implemented"] is False
    assert contract["handler"]["class"] == "HandlerObservabilitySinkEffect"
    assert contract["handler_routing"]["routing_strategy"] == "operation_match"


@pytest.mark.unit
def test_contract_yaml_subscribe_topic() -> None:
    """contract.yaml must declare the sink subscribe topic."""
    with CONTRACT_PATH.open() as fh:
        contract = yaml.safe_load(fh)

    topics = contract["event_bus"]["subscribe_topics"]
    assert "onex.cmd.omnimarket.observability-sink.v1" in topics


@pytest.mark.unit
def test_contract_yaml_publish_topic() -> None:
    """contract.yaml must declare the persisted publish topic."""
    with CONTRACT_PATH.open() as fh:
        contract = yaml.safe_load(fh)

    topics = contract["event_bus"]["publish_topics"]
    assert "onex.evt.omnimarket.observability-persisted.v1" in topics


@pytest.mark.unit
def test_contract_yaml_terminal_event() -> None:
    """terminal_event must match the publish topic."""
    with CONTRACT_PATH.open() as fh:
        contract = yaml.safe_load(fh)

    assert (
        contract["terminal_event"] == "onex.evt.omnimarket.observability-persisted.v1"
    )
