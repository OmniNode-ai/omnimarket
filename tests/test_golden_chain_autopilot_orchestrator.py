# SPDX-License-Identifier: MIT
"""Golden chain tests for node_autopilot_orchestrator.

Verifies the 4-phase FSM: start command -> A -> B -> C -> D -> COMPLETE,
circuit breaker, Phase C halt authority, and EventBusInmemory wiring.
"""

from __future__ import annotations

import contextlib
import json
from uuid import uuid4

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_autopilot_orchestrator.handlers.handler_autopilot_orchestrator import (
    HandlerAutopilotOrchestrator,
)
from omnimarket.nodes.node_autopilot_orchestrator.models.model_autopilot_phase_result import (
    EnumAutopilotCycleStatus,
    EnumAutopilotPhaseStatus,
)
from omnimarket.nodes.node_autopilot_orchestrator.models.model_autopilot_start_command import (
    ModelAutopilotStartCommand,
)

CMD_TOPIC = "onex.cmd.omnimarket.autopilot-orchestrator-start.v1"
PHASE_TOPIC = "onex.evt.omnimarket.autopilot-orchestrator-phase-transition.v1"
COMPLETED_TOPIC = "onex.evt.omnimarket.autopilot-orchestrator-completed.v1"


def _make_command(
    dry_run: bool = False,
    mode: str = "close-out",
) -> ModelAutopilotStartCommand:
    return ModelAutopilotStartCommand(
        correlation_id=uuid4(),
        mode=mode,
        dry_run=dry_run,
    )


@pytest.mark.unit
class TestAutopilotOrchestratorGoldenChain:
    """Golden chain: start command -> 4 phases -> COMPLETE."""

    async def test_no_event_bus_all_phases_warn(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Without event_bus, all 4 phases return warn (standalone stub mode)."""
        handler = HandlerAutopilotOrchestrator(event_bus=None)
        command = _make_command()

        result = await handler.handle(command)

        assert result.overall_status == EnumAutopilotCycleStatus.COMPLETE
        assert result.phases_completed == 4
        assert result.phases_failed == 0
        assert result.phase_a.status == EnumAutopilotPhaseStatus.WARN
        assert result.phase_b.status == EnumAutopilotPhaseStatus.WARN
        assert result.phase_c.status == EnumAutopilotPhaseStatus.WARN
        assert result.phase_d.status == EnumAutopilotPhaseStatus.WARN
        assert result.halt_reason == ""

    async def test_with_event_bus_all_phases_pass(
        self, event_bus: EventBusInmemory
    ) -> None:
        """With event_bus wired, all 4 phases publish and return pass."""
        await event_bus.start()
        handler = HandlerAutopilotOrchestrator(event_bus=event_bus)
        command = _make_command()

        result = await handler.handle(command)

        assert result.overall_status == EnumAutopilotCycleStatus.COMPLETE
        assert result.phases_completed == 4
        assert result.phases_failed == 0
        assert result.halt_reason == ""

        # Phase transition events published (IDLE->A, A->B, B->C, C->D, D->COMPLETE = 5)
        history = await event_bus.get_event_history(topic=PHASE_TOPIC)
        assert len(history) >= 4

        await event_bus.close()

    async def test_dry_run_propagates_to_payloads(
        self, event_bus: EventBusInmemory
    ) -> None:
        """dry_run flag propagates to all dispatch payloads (inside the envelope)."""
        await event_bus.start()
        captured_envelopes: list[dict[str, object]] = []

        original_publish = event_bus.publish

        async def recording_publish(topic: str, key: object, value: bytes) -> None:
            with contextlib.suppress(Exception):
                captured_envelopes.append(json.loads(value))
            await original_publish(topic=topic, key=key, value=value)

        event_bus.publish = recording_publish  # type: ignore[method-assign]

        handler = HandlerAutopilotOrchestrator(event_bus=event_bus)
        command = _make_command(dry_run=True)

        await handler.handle(command)

        # OMN-9215: payloads are now wrapped — inspect envelope.payload.
        inner_payloads = [
            env["payload"]
            for env in captured_envelopes
            if isinstance(env.get("payload"), dict) and "dry_run" in env["payload"]  # type: ignore[operator]
        ]
        assert inner_payloads, "expected at least one envelope carrying dry_run"
        assert all(p["dry_run"] is True for p in inner_payloads)

        await event_bus.close()

    async def test_correlation_id_in_all_payloads(
        self, event_bus: EventBusInmemory
    ) -> None:
        """correlation_id propagates into every dispatch envelope and payload."""
        await event_bus.start()
        captured_envelopes: list[dict[str, object]] = []

        original_publish = event_bus.publish

        async def recording_publish(topic: str, key: object, value: bytes) -> None:
            with contextlib.suppress(Exception):
                captured_envelopes.append(json.loads(value))
            await original_publish(topic=topic, key=key, value=value)

        event_bus.publish = recording_publish  # type: ignore[method-assign]

        handler = HandlerAutopilotOrchestrator(event_bus=event_bus)
        command = _make_command()

        await handler.handle(command)

        cid = str(command.correlation_id)
        # OMN-9215: correlation_id lives both on the envelope and in the
        # inner payload dict.
        for env in captured_envelopes:
            if env.get("correlation_id") is not None:
                assert env["correlation_id"] == cid
            inner = env.get("payload")
            if isinstance(inner, dict) and "correlation_id" in inner:
                assert inner["correlation_id"] == cid

        await event_bus.close()

    async def test_publishes_are_model_event_envelopes(
        self, event_bus: EventBusInmemory
    ) -> None:
        """OMN-9215: every publish must produce a valid ModelEventEnvelope.

        The consumer-side auto-wiring callback validates every inbound message
        via ``ModelEventEnvelope[object].model_validate(...)`` before dispatch
        (omnibase_infra.runtime.auto_wiring.handler_wiring._make_event_bus_callback).
        Bare-payload publishes fail validation with ``payload: Field required``
        and the target handler never runs — the regression that caused every
        merge_sweep tick to time out at 15m.
        """
        from omnibase_core.models.events.model_event_envelope import (
            ModelEventEnvelope,
        )

        await event_bus.start()
        captured: list[bytes] = []

        original_publish = event_bus.publish

        async def recording_publish(topic: str, key: object, value: bytes) -> None:
            captured.append(value)
            await original_publish(topic=topic, key=key, value=value)

        event_bus.publish = recording_publish  # type: ignore[method-assign]

        handler = HandlerAutopilotOrchestrator(event_bus=event_bus)
        command = _make_command()

        await handler.handle(command)

        assert captured, "handler should have published at least one message"
        for raw in captured:
            data = json.loads(raw)
            # Round-trip through ModelEventEnvelope — exactly what the
            # consumer-side auto-wiring callback does.
            envelope = ModelEventEnvelope[object].model_validate(data)
            assert envelope.payload is not None, (
                "envelope.payload must be present — bare-payload publish "
                "would fail ModelEventEnvelope validation (OMN-9215)"
            )
            assert envelope.correlation_id == command.correlation_id
            assert envelope.event_type is not None

        await event_bus.close()

    async def test_phase_results_not_run_when_halted_at_c(
        self, event_bus: EventBusInmemory
    ) -> None:
        """When Phase C halts, Phase D is not_run."""
        # Build a handler that overrides _run_phase_c to return HALT
        handler = HandlerAutopilotOrchestrator(event_bus=None)
        command = _make_command()

        from omnimarket.nodes.node_autopilot_orchestrator.models.model_autopilot_phase_result import (
            ModelAutopilotPhaseResult,
        )

        async def _halt_phase_c(
            cmd: ModelAutopilotStartCommand,
        ) -> ModelAutopilotPhaseResult:
            return ModelAutopilotPhaseResult(
                phase_id="C",
                status=EnumAutopilotPhaseStatus.HALT,
                detail="simulated infra gate failure",
                halt_reason="postgres unreachable",
            )

        handler._run_phase_c = _halt_phase_c  # type: ignore[method-assign]

        result = await handler.handle(command)

        assert result.overall_status == EnumAutopilotCycleStatus.HALTED
        assert result.halt_reason != ""
        assert result.phase_c.status == EnumAutopilotPhaseStatus.HALT
        # Phase D was never reached
        assert result.phase_d.status == EnumAutopilotPhaseStatus.NOT_RUN

    async def test_circuit_breaker_after_3_consecutive_failures(
        self, event_bus: EventBusInmemory
    ) -> None:
        """3 consecutive phase failures trip the circuit breaker -> FAILED."""
        handler = HandlerAutopilotOrchestrator(event_bus=None)
        command = _make_command()

        from omnimarket.nodes.node_autopilot_orchestrator.models.model_autopilot_phase_result import (
            ModelAutopilotPhaseResult,
        )

        async def _fail_phase(
            cmd: ModelAutopilotStartCommand,
        ) -> ModelAutopilotPhaseResult:
            return ModelAutopilotPhaseResult(
                phase_id="X",
                status=EnumAutopilotPhaseStatus.FAIL,
                detail="simulated failure",
            )

        handler._run_phase_a = _fail_phase  # type: ignore[method-assign]
        handler._run_phase_b = _fail_phase  # type: ignore[method-assign]
        handler._run_phase_c = _fail_phase  # type: ignore[method-assign]

        result = await handler.handle(command)

        assert result.overall_status == EnumAutopilotCycleStatus.CIRCUIT_BREAKER
        assert result.consecutive_failures >= 3

    async def test_phase_a_warn_does_not_halt(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Phase A warn (non-halting) allows pipeline to continue to COMPLETE."""
        handler = HandlerAutopilotOrchestrator(event_bus=None)
        command = _make_command()

        result = await handler.handle(command)

        # All phases are warn in standalone mode but pipeline still completes
        assert result.overall_status == EnumAutopilotCycleStatus.COMPLETE
        assert result.phase_a.status == EnumAutopilotPhaseStatus.WARN

    async def test_phase_d_halt_stops_pipeline(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Phase D halt (dod hard gate) stops after D -> HALTED."""
        handler = HandlerAutopilotOrchestrator(event_bus=None)
        command = _make_command()

        from omnimarket.nodes.node_autopilot_orchestrator.models.model_autopilot_phase_result import (
            ModelAutopilotPhaseResult,
        )

        async def _halt_phase_d(
            cmd: ModelAutopilotStartCommand,
        ) -> ModelAutopilotPhaseResult:
            return ModelAutopilotPhaseResult(
                phase_id="D",
                status=EnumAutopilotPhaseStatus.HALT,
                detail="dod sweep returned FAIL",
                halt_reason="dod FAIL: 3 tickets with incomplete evidence",
            )

        handler._run_phase_d = _halt_phase_d  # type: ignore[method-assign]

        result = await handler.handle(command)

        assert result.overall_status == EnumAutopilotCycleStatus.HALTED
        assert "Phase D" in result.halt_reason
        assert result.phase_d.status == EnumAutopilotPhaseStatus.HALT
