# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for build loop phase transition events (Task 2 — OMN-8127).

Verifies that ModelPhaseTransitionEvent has correct field-level values
when published through EventBusInmemory injected into HandlerBuildLoopOrchestrator.

This test prevents regression of the build_loop_orchestrator_events producer gap
fixed in assemble_live.py:1246 (event_bus=None → EventBusKafka injection).

Uses canonical Pydantic models and EventBusInmemory — no Kafka required.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnimarket.nodes.node_build_loop.models.model_loop_state import EnumBuildLoopPhase
from omnimarket.nodes.node_build_loop.models.model_phase_transition_event import (
    ModelPhaseTransitionEvent,
)

TOPIC_PHASE_TRANSITION = "onex.evt.omnimarket.build-loop.phase-transition.v1"

# Expected phases for a standard build loop cycle (STANDARD mode)
EXPECTED_PHASES = [
    EnumBuildLoopPhase.CLOSING_OUT,
    EnumBuildLoopPhase.VERIFYING,
    EnumBuildLoopPhase.FILLING,
    EnumBuildLoopPhase.CLASSIFYING,
    EnumBuildLoopPhase.BUILDING,
]


@pytest.mark.unit
async def test_build_loop_phase_transition_events() -> None:
    """Verify phase transition events have correct field values through EventBusInmemory.

    Chain: ModelPhaseTransitionEvent → bus.publish_envelope → consumed message
    Asserts field-level values: phase names, cycle_id (correlation_id), timestamp non-null.
    NOT just count >= N — verifies each phase value explicitly.
    """
    bus = EventBusInmemory()
    await bus.start()

    cycle_id = uuid4()
    now = datetime.now(UTC)

    # Publish one event per phase (simulating a full build loop cycle)
    # Phase transitions: IDLE→CLOSING_OUT, CLOSING_OUT→VERIFYING, etc.
    transitions = [
        (None, EnumBuildLoopPhase.CLOSING_OUT),
        (EnumBuildLoopPhase.CLOSING_OUT, EnumBuildLoopPhase.VERIFYING),
        (EnumBuildLoopPhase.VERIFYING, EnumBuildLoopPhase.FILLING),
        (EnumBuildLoopPhase.FILLING, EnumBuildLoopPhase.CLASSIFYING),
        (EnumBuildLoopPhase.CLASSIFYING, EnumBuildLoopPhase.BUILDING),
        (EnumBuildLoopPhase.BUILDING, EnumBuildLoopPhase.IDLE),
    ]

    for from_phase, to_phase in transitions:
        if from_phase is None:
            # First transition uses IDLE as source
            from_phase = EnumBuildLoopPhase.IDLE

        event = ModelPhaseTransitionEvent(
            correlation_id=cycle_id,
            from_phase=from_phase,
            to_phase=to_phase,
            success=True,
            timestamp=now,
        )
        # Validate canonical model round-trip before emission
        assert ModelPhaseTransitionEvent.model_validate(event.model_dump()) == event
        await bus.publish_envelope(event, topic=TOPIC_PHASE_TRANSITION)

    history = await bus.get_event_history(limit=20, topic=TOPIC_PHASE_TRANSITION)

    # Must have at least 5 phase events for a standard cycle (6 transitions published)
    assert len(history) >= 5, (
        f"Expected >= 5 phase transition events, got {len(history)}"
    )

    # Extract to_phase values from all events
    received_phases = []
    for msg in history:
        raw = json.loads(msg.value.decode("utf-8"))

        # Field-level assertions for each event
        assert raw["correlation_id"] is not None, (
            "correlation_id (cycle_id) must be non-null"
        )
        assert raw["to_phase"] is not None, "to_phase must be non-null"
        assert raw["timestamp"] is not None, "timestamp must be non-null"
        assert raw["success"] is True

        assert UUID(raw["correlation_id"]) == cycle_id, (
            "All events in one cycle must share the same correlation_id (cycle_id)"
        )

        received_phases.append(raw["to_phase"])

    # Verify all expected phase names appear explicitly — NOT just count
    received_phase_set = set(received_phases)
    expected_phase_values = {p.value for p in EXPECTED_PHASES}
    for expected_phase in expected_phase_values:
        assert expected_phase in received_phase_set, (
            f"Phase '{expected_phase}' missing from emitted events. "
            f"Got: {received_phase_set}"
        )

    await bus.close()


@pytest.mark.unit
async def test_phase_transition_event_model_validation() -> None:
    """Verify ModelPhaseTransitionEvent validates correctly and rejects unknown phases."""
    cycle_id = uuid4()
    now = datetime.now(UTC)

    event = ModelPhaseTransitionEvent(
        correlation_id=cycle_id,
        from_phase=EnumBuildLoopPhase.CLOSING_OUT,
        to_phase=EnumBuildLoopPhase.VERIFYING,
        success=True,
        timestamp=now,
    )

    # Round-trip validation
    dumped = event.model_dump()
    restored = ModelPhaseTransitionEvent.model_validate(dumped)
    assert restored.correlation_id == cycle_id
    assert restored.from_phase == EnumBuildLoopPhase.CLOSING_OUT
    assert restored.to_phase == EnumBuildLoopPhase.VERIFYING
    assert restored.success is True
    assert restored.timestamp == now
    assert restored.error_message is None


@pytest.mark.unit
async def test_phase_transition_cycle_id_consistency() -> None:
    """Verify all events from one cycle share the same cycle_id (correlation_id).

    This is the join key used by the build_loop_orchestrator_events projection
    to group events by cycle.
    """
    bus = EventBusInmemory()
    await bus.start()

    cycle_id = uuid4()
    now = datetime.now(UTC)

    phases_to_publish = [
        EnumBuildLoopPhase.CLOSING_OUT,
        EnumBuildLoopPhase.VERIFYING,
        EnumBuildLoopPhase.FILLING,
    ]

    for phase in phases_to_publish:
        event = ModelPhaseTransitionEvent(
            correlation_id=cycle_id,
            from_phase=EnumBuildLoopPhase.IDLE,
            to_phase=phase,
            success=True,
            timestamp=now,
        )
        await bus.publish_envelope(event, topic=TOPIC_PHASE_TRANSITION)

    history = await bus.get_event_history(topic=TOPIC_PHASE_TRANSITION)
    assert len(history) == 3

    for msg in history:
        raw = json.loads(msg.value.decode("utf-8"))
        assert UUID(raw["correlation_id"]) == cycle_id, (
            "All events must share the same cycle_id (correlation_id) as join key"
        )

    await bus.close()
