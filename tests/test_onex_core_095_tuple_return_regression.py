# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression: build_loop / design_to_plan handlers must not tuple-crash the bus.

OMN-13841: ``HandlerBuildLoop.handle()`` and ``HandlerDesignToPlan.handle()``
previously returned a raw 3-tuple ``(state, events, completed_event)``. The
``LocalRuntimeBusAdapter`` publish path (``omnibase_core.runtime`` /
``runtime_local_adapter``) only accepts a ``BaseModel``/``dict``/``None`` handler
return; a ``tuple`` raised
``ONEX_CORE_095_HANDLER_EXECUTION_ERROR`` at publish time, so the terminal
event was never published and the mapped skills (``build_loop`` /
``design_to_plan``) failed exit 1.

These tests drive the **real** adapter ``on_message`` end-to-end and assert:

1. ``on_error`` is never invoked (no ONEX_CORE_095), and
2. a payload is published to the declared output topic, and
3. that payload round-trips back into the handler's typed result model.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from omnibase_core.runtime.runtime_local_adapter import LocalRuntimeBusAdapter

from omnimarket.nodes.node_build_loop.handlers.handler_build_loop import (
    HandlerBuildLoop,
)
from omnimarket.nodes.node_build_loop.models.model_build_loop_result import (
    ModelBuildLoopResult,
)
from omnimarket.nodes.node_build_loop.models.model_loop_start_command import (
    ModelLoopStartCommand,
)
from omnimarket.nodes.node_design_to_plan.handlers.handler_design_to_plan import (
    HandlerDesignToPlan,
)
from omnimarket.nodes.node_design_to_plan.models.model_design_to_plan_command import (
    ModelDesignToPlanCommand,
)
from omnimarket.nodes.node_design_to_plan.models.model_design_to_plan_result import (
    ModelDesignToPlanResult,
)


class _CapturingBus:
    """Minimal ProtocolLocalRuntimeBus stand-in that records publishes."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, topic: str, key: object, value: bytes) -> None:
        self.published.append((topic, value))


class _Msg:
    """Minimal ProtocolLocalRuntimeMessage stand-in carrying encoded bytes."""

    def __init__(self, value: bytes) -> None:
        self.value = value


@pytest.mark.unit
async def test_build_loop_handle_publishes_typed_result_no_095() -> None:
    """HandlerBuildLoop.handle() output survives the adapter publish path."""
    errors: list[str] = []
    bus = _CapturingBus()
    adapter = LocalRuntimeBusAdapter(
        handler=HandlerBuildLoop(),
        handler_name="HandlerBuildLoop",
        input_model_cls=ModelLoopStartCommand,
        output_topic="build-loop-completed.v1",
        bus=bus,
        on_error=lambda: errors.append("FAILED"),
    )
    command = ModelLoopStartCommand(
        correlation_id=uuid.uuid4(),
        requested_at=datetime.now(tz=UTC),
        dry_run=True,
    )
    msg = _Msg(json.dumps(command.model_dump(mode="json")).encode("utf-8"))

    await adapter.on_message(msg)

    # No ONEX_CORE_095: on_error never fired.
    assert errors == []
    # Terminal event published to the declared topic.
    assert len(bus.published) == 1
    topic, payload = bus.published[0]
    assert topic == "build-loop-completed.v1"
    # Payload round-trips into the typed result model.
    result = ModelBuildLoopResult.model_validate_json(payload)
    assert result.completed_event.correlation_id == command.correlation_id


@pytest.mark.unit
async def test_design_to_plan_handle_publishes_typed_result_no_095() -> None:
    """HandlerDesignToPlan.handle() output survives the adapter publish path."""
    errors: list[str] = []
    bus = _CapturingBus()
    adapter = LocalRuntimeBusAdapter(
        handler=HandlerDesignToPlan(),
        handler_name="HandlerDesignToPlan",
        input_model_cls=ModelDesignToPlanCommand,
        output_topic="design-to-plan-completed.v1",
        bus=bus,
        on_error=lambda: errors.append("FAILED"),
    )
    command = ModelDesignToPlanCommand(
        correlation_id=uuid.uuid4(),
        topic="regression probe",
        requested_at=datetime.now(tz=UTC),
        dry_run=True,
        plan_only=True,
    )
    msg = _Msg(json.dumps(command.model_dump(mode="json")).encode("utf-8"))

    await adapter.on_message(msg)

    assert errors == []
    assert len(bus.published) == 1
    topic, payload = bus.published[0]
    assert topic == "design-to-plan-completed.v1"
    result = ModelDesignToPlanResult.model_validate_json(payload)
    assert result.completed_event.correlation_id == command.correlation_id
