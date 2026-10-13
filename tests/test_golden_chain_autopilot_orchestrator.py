# SPDX-License-Identifier: MIT
"""Golden chain tests for node_autopilot_orchestrator.

Verifies the 4-phase FSM on the in-memory bus: start command -> A -> B -> C -> D
-> COMPLETE with every phase advancing on its downstream result event, Phase C
halt authority, the circuit breaker, and the FSM surface against the contract.
Per-event behaviour (timeouts, ignored events, payload shapes) lives in
test_autopilot_orchestrator_phase_advance.py.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.nodes.node_autopilot_orchestrator.handlers.handler_autopilot_orchestrator import (
    AutopilotRunStore,
    EnumAutopilotFsmState,
    HandlerAutopilotOrchestrator,
    _PipelineState,
)
from omnimarket.nodes.node_autopilot_orchestrator.models.model_autopilot_downstream_event import (
    ModelAislopSweepCompletedEvent,
    ModelPlatformDiagnosticsCompletedEvent,
    ModelPrLifecycleCompletedEvent,
)
from omnimarket.nodes.node_autopilot_orchestrator.models.model_autopilot_phase_result import (
    EnumAutopilotCycleStatus,
    EnumAutopilotPhaseStatus,
)
from omnimarket.nodes.node_autopilot_orchestrator.models.model_autopilot_start_command import (
    ModelAutopilotStartCommand,
)

PHASE_TOPIC = "onex.evt.omnimarket.autopilot-orchestrator-phase-transition.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.autopilot-orchestrator-completed.v1"
START_TOPICS = (
    "onex.cmd.omnimarket.pr-lifecycle-orchestrator-start.v1",
    "onex.cmd.omnimarket.platform-diagnostics-start.v1",
    "onex.cmd.omnimarket.aislop-sweep-start.v1",
)


def _make_command(dry_run: bool = False) -> ModelAutopilotStartCommand:
    return ModelAutopilotStartCommand(correlation_id=uuid4(), dry_run=dry_run)


async def _run_chain(
    bus: EventBusInmemory, command: ModelAutopilotStartCommand
) -> HandlerAutopilotOrchestrator:
    """Start a run and answer each phase the way its downstream node would."""
    await bus.start()
    handler = HandlerAutopilotOrchestrator(event_bus=bus, run_store=AutopilotRunStore())
    cid = command.correlation_id
    await handler.handle(command)
    await handler.handle(
        ModelPrLifecycleCompletedEvent(correlation_id=cid, final_state="COMPLETE")
    )
    await handler.handle(
        ModelPlatformDiagnosticsCompletedEvent(
            correlation_id=cid, overall_status="PASS"
        )
    )
    await handler.handle(
        ModelAislopSweepCompletedEvent(correlation_id=cid, status="clean")
    )
    return handler


async def _envelopes(bus: EventBusInmemory, topic: str) -> list[dict[str, Any]]:
    return [json.loads(m.value) for m in await bus.get_event_history(topic=topic)]


@pytest.mark.unit
class TestAutopilotOrchestratorGoldenChain:
    """Golden chain: start command -> 4 phases -> COMPLETE."""

    async def test_chain_completes_with_every_phase_answered(
        self, event_bus: EventBusInmemory
    ) -> None:
        command = _make_command()
        await _run_chain(event_bus, command)

        (terminal,) = await _envelopes(event_bus, COMPLETED_TOPIC)
        result = terminal["payload"]
        assert result["overall_status"] == EnumAutopilotCycleStatus.COMPLETE
        # A is skipped (no downstream node); B, C, D passed on their result events.
        assert result["phase_a"]["status"] == EnumAutopilotPhaseStatus.SKIPPED
        assert result["phases_completed"] == 3
        assert result["phases_failed"] == 0
        assert result["halt_reason"] == ""

        # IDLE->A, A->B, B->C, C->D, D->COMPLETE
        assert len(await _envelopes(event_bus, PHASE_TOPIC)) == 5

    async def test_dry_run_propagates_to_every_start_command(
        self, event_bus: EventBusInmemory
    ) -> None:
        await _run_chain(event_bus, _make_command(dry_run=True))

        for topic in START_TOPICS:
            (envelope,) = await _envelopes(event_bus, topic)
            assert envelope["payload"]["dry_run"] is True

    async def test_correlation_id_in_every_publish(
        self, event_bus: EventBusInmemory
    ) -> None:
        command = _make_command()
        await _run_chain(event_bus, command)

        cid = str(command.correlation_id)
        for topic in (*START_TOPICS, PHASE_TOPIC, COMPLETED_TOPIC):
            for env in await _envelopes(event_bus, topic):
                assert env["correlation_id"] == cid
                assert env["payload"]["correlation_id"] == cid

    async def test_publishes_are_model_event_envelopes(
        self, event_bus: EventBusInmemory
    ) -> None:
        """OMN-9215: every publish must produce a valid ModelEventEnvelope.

        The consumer-side auto-wiring callback validates every inbound message
        via ``ModelEventEnvelope[object].model_validate(...)`` before dispatch.
        A bare-payload publish fails validation with ``payload: Field required``
        and the target handler never runs.
        """
        command = _make_command()
        await _run_chain(event_bus, command)

        seen = 0
        for topic in (*START_TOPICS, PHASE_TOPIC, COMPLETED_TOPIC):
            for data in await _envelopes(event_bus, topic):
                envelope = ModelEventEnvelope[object].model_validate(data)
                assert envelope.payload is not None
                assert envelope.correlation_id == command.correlation_id
                assert envelope.event_type is not None
                seen += 1
        assert seen >= 8

    async def test_phase_a_skipped_does_not_halt(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Phase A has no downstream node; it neither blocks nor counts as a failure."""
        command = _make_command()
        await _run_chain(event_bus, command)

        (terminal,) = await _envelopes(event_bus, COMPLETED_TOPIC)
        assert (
            terminal["payload"]["phase_a"]["status"] == EnumAutopilotPhaseStatus.SKIPPED
        )
        assert (
            terminal["payload"]["overall_status"] == EnumAutopilotCycleStatus.COMPLETE
        )

    async def test_phase_c_fail_halts_and_phase_d_is_not_run(
        self, event_bus: EventBusInmemory
    ) -> None:
        await event_bus.start()
        handler = HandlerAutopilotOrchestrator(
            event_bus=event_bus, run_store=AutopilotRunStore()
        )
        command = _make_command()
        cid = command.correlation_id
        await handler.handle(command)
        await handler.handle(
            ModelPrLifecycleCompletedEvent(correlation_id=cid, final_state="COMPLETE")
        )
        await handler.handle(
            ModelPlatformDiagnosticsCompletedEvent(
                correlation_id=cid, overall_status="FAIL"
            )
        )

        (terminal,) = await _envelopes(event_bus, COMPLETED_TOPIC)
        result = terminal["payload"]
        assert result["overall_status"] == EnumAutopilotCycleStatus.HALTED
        assert result["halt_reason"] != ""
        assert result["phase_c"]["status"] == EnumAutopilotPhaseStatus.HALT
        assert result["phase_d"]["status"] == EnumAutopilotPhaseStatus.NOT_RUN

    async def test_start_publish_failure_at_infra_gate_halts(
        self, event_bus: EventBusInmemory
    ) -> None:
        """A dispatch the gate could not even publish is a halt, not a pass."""
        await event_bus.start()
        original = event_bus.publish

        async def failing_publish(topic: str, key: object, value: bytes) -> None:
            if topic == "onex.cmd.omnimarket.platform-diagnostics-start.v1":
                raise RuntimeError("broker down")
            await original(topic=topic, key=key, value=value)

        event_bus.publish = failing_publish  # type: ignore[method-assign]
        handler = HandlerAutopilotOrchestrator(
            event_bus=event_bus, run_store=AutopilotRunStore()
        )
        command = _make_command()
        await handler.handle(command)
        await handler.handle(
            ModelPrLifecycleCompletedEvent(
                correlation_id=command.correlation_id, final_state="COMPLETE"
            )
        )

        (terminal,) = await _envelopes(event_bus, COMPLETED_TOPIC)
        assert terminal["payload"]["overall_status"] == EnumAutopilotCycleStatus.HALTED
        assert "broker down" in terminal["payload"]["halt_reason"]

    async def test_circuit_breaker_trips_at_three_consecutive_failures(
        self, event_bus: EventBusInmemory
    ) -> None:
        handler = HandlerAutopilotOrchestrator(
            event_bus=event_bus, run_store=AutopilotRunStore()
        )
        state = _PipelineState(consecutive_failures=2)
        assert handler._circuit_breaker_tripped(state) is False
        state.consecutive_failures = 3
        assert handler._circuit_breaker_tripped(state) is True
        state.fsm = EnumAutopilotFsmState.FAILED
        result = handler._build_result(state, uuid4())
        assert result.overall_status == EnumAutopilotCycleStatus.CIRCUIT_BREAKER


@pytest.mark.unit
def test_fsm_state_enum_matches_contract_declared_states() -> None:
    """EnumAutopilotFsmState must expose exactly the eight contract.yaml
    state_machine states (IDLE, PHASE_A_WORKTREE, PHASE_B_MERGE_SWEEP,
    PHASE_C_INFRA_GATE, PHASE_D_QUALITY_SWEEPS, COMPLETE, HALTED, FAILED) —
    pins the runtime FSM surface against contract drift. The golden chain above
    reaches IDLE -> A -> B -> C -> D -> COMPLETE, halts at C, and the breaker
    test reaches FAILED."""
    from pathlib import Path

    contract = yaml.safe_load(
        (
            Path(__file__).resolve().parent.parent
            / "src/omnimarket/nodes/node_autopilot_orchestrator/contract.yaml"
        ).read_text()
    )
    declared = {s["state_name"] for s in contract["state_machine"]["states"]}
    observed = {member.value for member in EnumAutopilotFsmState}
    assert observed == declared
    assert observed == {
        EnumAutopilotFsmState.IDLE,
        EnumAutopilotFsmState.PHASE_A_WORKTREE,
        EnumAutopilotFsmState.PHASE_B_MERGE_SWEEP,
        EnumAutopilotFsmState.PHASE_C_INFRA_GATE,
        EnumAutopilotFsmState.PHASE_D_QUALITY_SWEEPS,
        EnumAutopilotFsmState.COMPLETE,
        EnumAutopilotFsmState.HALTED,
        EnumAutopilotFsmState.FAILED,
    }
