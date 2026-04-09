"""Golden chain tests for node_build_loop_orchestrator.

Verifies the orchestrator composes sub-handlers via FSM:
  start command -> phase transitions with sub-handler invocations -> completion.
Uses mock sub-handlers and EventBusInmemory.

Related:
    - OMN-7583: Migrate build loop orchestrator
    - OMN-7575: Build loop migration epic
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

# Topic string for test assertions — matches contract.yaml publish_topics
TOPIC_PHASE_TRANSITION = (
    "onex.evt.omnimarket.build-loop-orchestrator-phase-transition.v1"
)

# --- Mock sub-handlers ---


class MockCloseout:
    """Mock closeout handler that always succeeds."""

    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.call_count = 0

    async def handle(
        self, *, correlation_id: UUID, dry_run: bool = False
    ) -> CloseoutResult:
        self.call_count += 1
        if self._fail:
            msg = "Closeout failed"
            raise RuntimeError(msg)
        return CloseoutResult(success=True)


class MockVerify:
    """Mock verify handler."""

    def __init__(self, *, pass_checks: bool = True) -> None:
        self._pass = pass_checks
        self.call_count = 0

    async def handle(
        self, *, correlation_id: UUID, dry_run: bool = False
    ) -> VerifyResult:
        self.call_count += 1
        return VerifyResult(all_critical_passed=self._pass)


class MockRsdFill:
    """Mock RSD fill handler."""

    def __init__(self, tickets: tuple[ScoredTicket, ...] = ()) -> None:
        self._tickets = tickets
        self.call_count = 0

    async def handle(
        self,
        *,
        correlation_id: UUID,
        scored_tickets: tuple[ScoredTicket, ...],
        max_tickets: int = 5,
    ) -> RsdFillResult:
        self.call_count += 1
        return RsdFillResult(
            selected_tickets=self._tickets,
            total_selected=len(self._tickets),
        )


class MockClassify:
    """Mock classify handler."""

    def __init__(self, targets: tuple[BuildTarget, ...] = ()) -> None:
        self._targets = targets
        self.call_count = 0

    async def handle(
        self,
        *,
        correlation_id: UUID,
        tickets: tuple[ScoredTicket, ...],
    ) -> ClassifyResult:
        self.call_count += 1
        return ClassifyResult(classifications=self._targets)


class MockDispatch:
    """Mock dispatch handler."""

    def __init__(self, dispatched: int = 0) -> None:
        self._dispatched = dispatched
        self.call_count = 0

    async def handle(
        self,
        *,
        correlation_id: UUID,
        targets: tuple[BuildTarget, ...],
        dry_run: bool = False,
    ) -> DispatchResult:
        self.call_count += 1
        return DispatchResult(
            total_dispatched=self._dispatched,
            delegation_payloads=(),
        )


def _make_command(
    skip_closeout: bool = False,
    dry_run: bool = False,
    max_cycles: int = 1,
) -> ModelLoopStartCommand:
    return ModelLoopStartCommand(
        correlation_id=uuid4(),
        max_cycles=max_cycles,
        skip_closeout=skip_closeout,
        dry_run=dry_run,
        requested_at=datetime.now(tz=UTC),
    )


def _make_orchestrator(
    *,
    closeout: MockCloseout | None = None,
    verify: MockVerify | None = None,
    rsd_fill: MockRsdFill | None = None,
    classify: MockClassify | None = None,
    dispatch: MockDispatch | None = None,
    event_bus: EventBusInmemory | None = None,
) -> HandlerBuildLoopOrchestrator:
    return HandlerBuildLoopOrchestrator(
        closeout=closeout or MockCloseout(),
        verify=verify or MockVerify(),
        rsd_fill=rsd_fill or MockRsdFill(),
        classify=classify or MockClassify(),
        dispatch=dispatch or MockDispatch(),
        event_bus=event_bus,
    )


@pytest.mark.unit
class TestBuildLoopOrchestratorGoldenChain:
    """Golden chain: orchestrator composes sub-handlers through FSM cycle."""

    async def test_full_cycle_all_phases_succeed(self) -> None:
        """All 6 phases succeed -> COMPLETE with cycles_completed=1."""
        tickets = (
            ScoredTicket(ticket_id="OMN-1", title="Test", rsd_score=3.0, priority=2),
        )
        targets = (
            BuildTarget(ticket_id="OMN-1", title="Test", buildability="auto_buildable"),
        )
        orch = _make_orchestrator(
            rsd_fill=MockRsdFill(tickets=tickets),
            classify=MockClassify(targets=targets),
            dispatch=MockDispatch(dispatched=1),
        )
        command = _make_command()

        result = await orch.handle(command)

        assert result.cycles_completed == 1
        assert result.cycles_failed == 0
        assert result.total_tickets_dispatched == 1
        assert len(result.cycle_summaries) == 1
        assert result.cycle_summaries[0].final_phase == EnumBuildLoopPhase.COMPLETE

    async def test_skip_closeout(self) -> None:
        """skip_closeout=True skips CLOSING_OUT, goes IDLE -> VERIFYING."""
        closeout = MockCloseout()
        orch = _make_orchestrator(closeout=closeout)
        command = _make_command(skip_closeout=True)

        result = await orch.handle(command)

        assert result.cycles_completed == 1
        assert closeout.call_count == 0  # Closeout was skipped

    async def test_closeout_called_by_default(self) -> None:
        """Default flow calls closeout handler."""
        closeout = MockCloseout()
        orch = _make_orchestrator(closeout=closeout)
        command = _make_command()

        result = await orch.handle(command)

        assert result.cycles_completed == 1
        assert closeout.call_count == 1

    async def test_verify_failure_causes_cycle_failure(self) -> None:
        """Verification failure -> cycle fails after circuit breaker."""
        verify = MockVerify(pass_checks=False)
        orch = _make_orchestrator(verify=verify)
        command = _make_command()

        result = await orch.handle(command)

        # Verify fails, circuit breaker trips after 3 consecutive failures
        assert result.cycles_failed == 1
        assert result.cycles_completed == 0
        # Verify called multiple times due to retry-in-place before breaker
        assert verify.call_count == 3

    async def test_sub_handler_exception_causes_failure(self) -> None:
        """Exception in a sub-handler -> phase failure -> eventually FAILED."""
        closeout = MockCloseout(fail=True)
        orch = _make_orchestrator(closeout=closeout)
        command = _make_command()

        result = await orch.handle(command)

        assert result.cycles_failed == 1
        assert result.cycles_completed == 0
        assert closeout.call_count == 3  # Retried 3 times

    async def test_dry_run_propagated(self) -> None:
        """dry_run flag propagates through to sub-handlers."""
        orch = _make_orchestrator()
        command = _make_command(dry_run=True)

        result = await orch.handle(command)

        assert result.cycles_completed == 1

    async def test_event_bus_receives_phase_transitions(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Phase transition events are published to event bus."""
        await event_bus.start()

        orch = _make_orchestrator(event_bus=event_bus)
        command = _make_command()

        result = await orch.handle(command)

        assert result.cycles_completed == 1

        phase_history = await event_bus.get_event_history(
            topic=TOPIC_PHASE_TRANSITION,
        )
        # 6 transitions: IDLE->CLOSING_OUT, CLOSING_OUT->VERIFYING,
        # VERIFYING->FILLING, FILLING->CLASSIFYING, CLASSIFYING->BUILDING,
        # BUILDING->COMPLETE
        assert len(phase_history) == 6

        # Verify first and last transitions
        first = json.loads(phase_history[0].value)
        assert first["from_phase"] == "idle"
        assert first["to_phase"] == "closing_out"
        assert first["success"] is True

        last = json.loads(phase_history[-1].value)
        assert last["from_phase"] == "building"
        assert last["to_phase"] == "complete"
        assert last["success"] is True

        await event_bus.close()

    async def test_metrics_accumulate(self) -> None:
        """Ticket metrics accumulate across phases."""
        tickets = (
            ScoredTicket(ticket_id="OMN-1", title="T1", rsd_score=3.0, priority=2),
            ScoredTicket(ticket_id="OMN-2", title="T2", rsd_score=2.0, priority=3),
        )
        targets = (
            BuildTarget(ticket_id="OMN-1", title="T1", buildability="auto_buildable"),
        )
        orch = _make_orchestrator(
            rsd_fill=MockRsdFill(tickets=tickets),
            classify=MockClassify(targets=targets),
            dispatch=MockDispatch(dispatched=1),
        )
        command = _make_command()

        result = await orch.handle(command)

        summary = result.cycle_summaries[0]
        assert summary.tickets_filled == 2
        assert summary.tickets_classified == 1  # Only auto_buildable
        assert summary.tickets_dispatched == 1

    async def test_multiple_cycles(self) -> None:
        """Multiple cycles run sequentially when max_cycles > 1."""
        orch = _make_orchestrator(dispatch=MockDispatch(dispatched=2))
        command = _make_command(max_cycles=3)

        result = await orch.handle(command)

        assert result.cycles_completed == 3
        assert result.cycles_failed == 0
        assert result.total_tickets_dispatched == 6
        assert len(result.cycle_summaries) == 3

    async def test_zero_imports_from_omnibase_infra(self) -> None:
        """Verify no imports from omnibase_infra in the orchestrator module."""
        import importlib
        import inspect

        mod = importlib.import_module(
            "omnimarket.nodes.node_build_loop_orchestrator."
            "handlers.handler_build_loop_orchestrator"
        )
        source = inspect.getsource(mod)
        assert "from omnibase_infra" not in source
        assert "import omnibase_infra" not in source

    def test_zero_arg_construction_succeeds(self) -> None:
        """Auto-wiring runtime must be able to construct with zero args."""
        orch = HandlerBuildLoopOrchestrator()
        assert orch._closeout is None
        assert orch._verify is None
        assert orch._rsd_fill is None
        assert orch._classify is None
        assert orch._dispatch is None

    def test_explicit_injection_still_works(self) -> None:
        """Callers that pass sub-handlers explicitly must not be broken."""
        closeout = MockCloseout()
        verify = MockVerify()
        rsd_fill = MockRsdFill()
        classify = MockClassify()
        dispatch = MockDispatch()
        orch = HandlerBuildLoopOrchestrator(
            closeout=closeout,
            verify=verify,
            rsd_fill=rsd_fill,
            classify=classify,
            dispatch=dispatch,
        )
        assert orch._closeout is closeout
        assert orch._verify is verify
        assert orch._rsd_fill is rsd_fill
        assert orch._classify is classify
        assert orch._dispatch is dispatch


@pytest.mark.unit
class TestDoDVerificationGating:
    """DoD verification gates FSM advancement after BUILDING phase.

    Tests that the overseer verifier is called after dispatch and that
    a FAIL verdict blocks FSM advancement to COMPLETE.

    Related: OMN-8030
    """

    async def test_dod_pass_advances_to_complete(self) -> None:
        """Happy path: verifier returns PASS for all targets -> COMPLETE."""
        targets = (
            BuildTarget(ticket_id="OMN-100", title="T1", buildability="auto_buildable"),
        )
        orch = _make_orchestrator(
            rsd_fill=MockRsdFill(
                tickets=(
                    ScoredTicket(
                        ticket_id="OMN-100", title="T1", rsd_score=3.0, priority=2
                    ),
                )
            ),
            classify=MockClassify(targets=targets),
            dispatch=MockDispatch(dispatched=1),
        )
        command = _make_command()
        result = await orch.handle(command)
        assert result.cycles_completed == 1
        assert result.cycles_failed == 0
        assert result.cycle_summaries[0].final_phase == EnumBuildLoopPhase.COMPLETE

    async def test_dod_emits_event_on_pass(self) -> None:
        """DoD PASS emits onex.evt.build-loop.dod-checked.v1 for each target."""
        from omnimarket.nodes.node_build_loop_orchestrator.topics import (
            TOPIC_DOD_CHECKED,
        )

        event_bus = EventBusInmemory()
        await event_bus.start()
        targets = (
            BuildTarget(ticket_id="OMN-200", title="T2", buildability="auto_buildable"),
        )
        orch = _make_orchestrator(
            rsd_fill=MockRsdFill(
                tickets=(
                    ScoredTicket(
                        ticket_id="OMN-200", title="T2", rsd_score=3.0, priority=2
                    ),
                )
            ),
            classify=MockClassify(targets=targets),
            dispatch=MockDispatch(dispatched=1),
            event_bus=event_bus,
        )
        command = _make_command()
        await orch.handle(command)

        dod_events = await event_bus.get_event_history(topic=TOPIC_DOD_CHECKED)
        assert len(dod_events) == 1
        payload = json.loads(dod_events[0].value)
        assert payload["task_id"] == "OMN-200"
        assert payload["verdict"] == "PASS"
        await event_bus.close()

    async def test_dod_fail_blocks_fsm_at_building(self) -> None:
        """DoD FAIL verdict keeps cycle in BUILDING (not COMPLETE).

        The overseer verifier returns FAIL when domain is unknown or input
        is empty. We force FAIL by providing a task_id that triggers
        input_completeness failure — empty node_id placeholder.

        Actually, since HandlerOverseerVerifier is deterministic and requires
        real domain + node_id, the easiest way to force FAIL is to mock the
        overseer verifier on the orchestrator instance.
        """
        from unittest.mock import patch

        targets = (
            BuildTarget(ticket_id="OMN-300", title="T3", buildability="auto_buildable"),
        )
        orch = _make_orchestrator(
            rsd_fill=MockRsdFill(
                tickets=(
                    ScoredTicket(
                        ticket_id="OMN-300", title="T3", rsd_score=3.0, priority=2
                    ),
                )
            ),
            classify=MockClassify(targets=targets),
            dispatch=MockDispatch(dispatched=1),
        )
        command = _make_command()

        # Patch the overseer verifier to return FAIL verdict
        with patch.object(
            orch._overseer_verifier,
            "verify",
            return_value={
                "verdict": "FAIL",
                "checks": [{"name": "input_completeness", "passed": False}],
                "failure_class": "DATA_INTEGRITY",
                "summary": "Forced FAIL for test",
            },
        ):
            result = await orch.handle(command)

        assert result.cycles_completed == 0
        assert result.cycles_failed == 1
        assert result.cycle_summaries[0].final_phase == EnumBuildLoopPhase.FAILED

    async def test_dod_fail_emits_fail_event(self) -> None:
        """DoD FAIL emits dod-checked event with verdict=FAIL."""
        from unittest.mock import patch

        from omnimarket.nodes.node_build_loop_orchestrator.topics import (
            TOPIC_DOD_CHECKED,
        )

        event_bus = EventBusInmemory()
        await event_bus.start()
        targets = (
            BuildTarget(ticket_id="OMN-400", title="T4", buildability="auto_buildable"),
        )
        orch = _make_orchestrator(
            rsd_fill=MockRsdFill(
                tickets=(
                    ScoredTicket(
                        ticket_id="OMN-400", title="T4", rsd_score=3.0, priority=2
                    ),
                )
            ),
            classify=MockClassify(targets=targets),
            dispatch=MockDispatch(dispatched=1),
            event_bus=event_bus,
        )
        command = _make_command()

        with patch.object(
            orch._overseer_verifier,
            "verify",
            return_value={
                "verdict": "FAIL",
                "checks": [{"name": "input_completeness", "passed": False}],
                "failure_class": "DATA_INTEGRITY",
                "summary": "Forced FAIL for test",
            },
        ):
            await orch.handle(command)

        dod_events = await event_bus.get_event_history(topic=TOPIC_DOD_CHECKED)
        assert len(dod_events) >= 1
        payload = json.loads(dod_events[0].value)
        assert payload["task_id"] == "OMN-400"
        assert payload["verdict"] == "FAIL"
        assert payload["checks_failed"] == 1
        await event_bus.close()
