"""Golden chain test for build loop phase transition events (OMN-8175 Task 2).

Drives HandlerBuildLoopOrchestrator with mock sub-handlers and EventBusInmemory,
collects phase transition events from the bus, and asserts field-level values
for every phase in a full cycle.

Chain: ModelLoopStartCommand -> HandlerBuildLoopOrchestrator.handle()
    -> _publish_phase_event -> EventBusInmemory -> get_event_history
    -> ModelPhaseTransitionEvent.model_validate

Asserts:
    - >= 6 events per full cycle
    - to_phase includes CLOSING_OUT, VERIFYING, FILLING, CLASSIFYING, BUILDING, COMPLETE
    - Each event has non-null correlation_id (cycle_id), to_phase, timestamp
    - NOT just count assertions — field-level validation on every event
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_build_loop.models.model_loop_start_command import (
    ModelLoopStartCommand,
)
from omnimarket.nodes.node_build_loop.models.model_loop_state import (
    EnumBuildLoopPhase,
)
from omnimarket.nodes.node_build_loop.models.model_phase_transition_event import (
    ModelPhaseTransitionEvent,
)
from omnimarket.nodes.node_build_loop_orchestrator.handlers.handler_build_loop_orchestrator import (
    HandlerBuildLoopOrchestrator,
)
from omnimarket.nodes.node_build_loop_orchestrator.protocols.protocol_sub_handlers import (
    BuildTarget,
    ClassifyResult,
    CloseoutResult,
    DispatchResult,
    RsdFillResult,
    ScoredTicket,
    VerifyResult,
)

TOPIC_PHASE_TRANSITION = (
    "onex.evt.omnimarket.build-loop-orchestrator-phase-transition.v1"
)

REQUIRED_PHASES = [
    EnumBuildLoopPhase.CLOSING_OUT,
    EnumBuildLoopPhase.VERIFYING,
    EnumBuildLoopPhase.FILLING,
    EnumBuildLoopPhase.CLASSIFYING,
    EnumBuildLoopPhase.BUILDING,
    EnumBuildLoopPhase.COMPLETE,
]


class MockCloseout:
    async def handle(
        self, *, correlation_id: UUID, dry_run: bool = False
    ) -> CloseoutResult:
        return CloseoutResult(success=True)


class MockVerify:
    async def handle(
        self, *, correlation_id: UUID, dry_run: bool = False
    ) -> VerifyResult:
        return VerifyResult(all_critical_passed=True)


class MockRsdFill:
    def __init__(self, tickets: tuple[ScoredTicket, ...] = ()) -> None:
        self._tickets = tickets

    async def handle(
        self,
        *,
        correlation_id: UUID,
        scored_tickets: tuple[ScoredTicket, ...],
        max_tickets: int = 5,
    ) -> RsdFillResult:
        return RsdFillResult(
            selected_tickets=self._tickets,
            total_selected=len(self._tickets),
        )


class MockClassify:
    def __init__(self, targets: tuple[BuildTarget, ...] = ()) -> None:
        self._targets = targets

    async def handle(
        self,
        *,
        correlation_id: UUID,
        tickets: tuple[ScoredTicket, ...],
    ) -> ClassifyResult:
        return ClassifyResult(classifications=self._targets)


class MockDispatch:
    def __init__(self, dispatched: int = 0) -> None:
        self._dispatched = dispatched

    async def handle(
        self,
        *,
        correlation_id: UUID,
        targets: tuple[BuildTarget, ...],
        dry_run: bool = False,
    ) -> DispatchResult:
        return DispatchResult(
            total_dispatched=self._dispatched,
            delegation_payloads=(),
        )


def _make_command() -> ModelLoopStartCommand:
    return ModelLoopStartCommand(
        correlation_id=uuid4(),
        max_cycles=1,
        dry_run=False,
        requested_at=datetime.now(tz=UTC),
    )


def _make_orchestrator(*, event_bus: EventBusInmemory) -> HandlerBuildLoopOrchestrator:
    tickets = (
        ScoredTicket(ticket_id="OMN-1", title="Test", rsd_score=3.0, priority=2),
    )
    targets = (
        BuildTarget(ticket_id="OMN-1", title="Test", buildability="auto_buildable"),
    )
    return HandlerBuildLoopOrchestrator(
        closeout=MockCloseout(),
        verify=MockVerify(),
        rsd_fill=MockRsdFill(tickets=tickets),
        classify=MockClassify(targets=targets),
        dispatch=MockDispatch(dispatched=1),
        event_bus=event_bus,
    )


@pytest.mark.unit
async def test_build_loop_phase_transition_events() -> None:
    """Full-cycle golden chain: orchestrator emits >= 6 phase transition events
    with correct field-level values through EventBusInmemory.

    Asserts per-event: correlation_id, to_phase, timestamp all non-null.
    Asserts set-wise: all 6 required phases appear in to_phase values.
    """
    bus = EventBusInmemory(environment="test", group="golden-chain-build-loop")
    await bus.start()

    orch = _make_orchestrator(event_bus=bus)
    command = _make_command()
    cycle_id = command.correlation_id

    result = await orch.handle(command)

    assert result.cycles_completed == 1
    assert result.cycles_failed == 0

    history = await bus.get_event_history(topic=TOPIC_PHASE_TRANSITION)

    assert len(history) >= 6, (
        f"Expected >= 6 phase transition events for a full cycle, got {len(history)}"
    )

    received_to_phases: list[str] = []

    for idx, msg in enumerate(history):
        raw = json.loads(msg.value.decode("utf-8"))

        event = ModelPhaseTransitionEvent.model_validate(raw)

        assert event.correlation_id is not None, (
            f"Event {idx}: correlation_id (cycle_id) must be non-null"
        )
        assert event.to_phase is not None, (
            f"Event {idx}: to_phase (phase) must be non-null"
        )
        assert event.timestamp is not None, f"Event {idx}: timestamp must be non-null"

        assert event.correlation_id == cycle_id, (
            f"Event {idx}: correlation_id {event.correlation_id} != cycle_id {cycle_id}"
        )

        assert event.success is True, f"Event {idx}: success must be True"

        received_to_phases.append(event.to_phase.value)

    received_phase_set = set(received_to_phases)
    for required in REQUIRED_PHASES:
        assert required.value in received_phase_set, (
            f"Required phase '{required.value}' missing from emitted events. "
            f"Got: {sorted(received_phase_set)}"
        )

    assert received_to_phases[-1] == EnumBuildLoopPhase.COMPLETE.value, (
        "Last event must transition to COMPLETE"
    )

    await bus.close()


@pytest.mark.unit
async def test_phase_transition_events_ordered() -> None:
    """Phase transition events are emitted in FSM order."""
    bus = EventBusInmemory(environment="test", group="golden-chain-build-loop")
    await bus.start()

    orch = _make_orchestrator(event_bus=bus)
    command = _make_command()

    await orch.handle(command)

    history = await bus.get_event_history(topic=TOPIC_PHASE_TRANSITION)

    to_phases = []
    for msg in history:
        raw = json.loads(msg.value.decode("utf-8"))
        event = ModelPhaseTransitionEvent.model_validate(raw)
        to_phases.append(event.to_phase)

    expected_order = [
        EnumBuildLoopPhase.CLOSING_OUT,
        EnumBuildLoopPhase.VERIFYING,
        EnumBuildLoopPhase.FILLING,
        EnumBuildLoopPhase.CLASSIFYING,
        EnumBuildLoopPhase.BUILDING,
        EnumBuildLoopPhase.COMPLETE,
    ]

    assert to_phases == expected_order, (
        f"Phase order mismatch.\n  Expected: {[p.value for p in expected_order]}\n"
        f"  Got:      {[p.value for p in to_phases]}"
    )

    await bus.close()


@pytest.mark.unit
async def test_phase_transition_from_phase_chain() -> None:
    """Each event's from_phase matches the previous event's to_phase."""
    bus = EventBusInmemory(environment="test", group="golden-chain-build-loop")
    await bus.start()

    orch = _make_orchestrator(event_bus=bus)
    command = _make_command()

    await orch.handle(command)

    history = await bus.get_event_history(topic=TOPIC_PHASE_TRANSITION)

    events = []
    for msg in history:
        raw = json.loads(msg.value.decode("utf-8"))
        events.append(ModelPhaseTransitionEvent.model_validate(raw))

    for i in range(1, len(events)):
        assert events[i].from_phase == events[i - 1].to_phase, (
            f"Event {i}: from_phase={events[i].from_phase.value} "
            f"!= previous to_phase={events[i - 1].to_phase.value}"
        )

    assert events[0].from_phase == EnumBuildLoopPhase.IDLE

    await bus.close()


@pytest.mark.unit
async def test_phase_transition_timestamps_monotonic() -> None:
    """Timestamps across phase transition events are non-decreasing."""
    bus = EventBusInmemory(environment="test", group="golden-chain-build-loop")
    await bus.start()

    orch = _make_orchestrator(event_bus=bus)
    command = _make_command()

    await orch.handle(command)

    history = await bus.get_event_history(topic=TOPIC_PHASE_TRANSITION)

    timestamps = []
    for msg in history:
        raw = json.loads(msg.value.decode("utf-8"))
        event = ModelPhaseTransitionEvent.model_validate(raw)
        timestamps.append(event.timestamp)

    for i in range(1, len(timestamps)):
        assert timestamps[i] >= timestamps[i - 1], (
            f"Timestamp at event {i} ({timestamps[i]}) "
            f"< timestamp at event {i - 1} ({timestamps[i - 1]})"
        )

    await bus.close()
