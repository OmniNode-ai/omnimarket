"""Golden chain test for node_overnight.

Verifies the overnight FSM orchestrator correctly sequences phases,
handles skip flags, and derives terminal status. All tests use dry_run=True
or inject phase_results — no subprocess calls.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from onex_change_control.overseer.model_overnight_contract import (
    ModelOvernightContract,
    ModelOvernightHaltCondition,
    ModelOvernightPhaseSpec,
)

from omnimarket.nodes.node_overnight.handlers.handler_overnight import (
    EnumOvernightStatus,
    EnumPhase,
    HandlerOvernight,
    ModelOvernightCommand,
)

CMD_TOPIC = "onex.cmd.omnimarket.overnight-start.v1"
EVT_TOPIC = "onex.evt.omnimarket.overnight-session-completed.v1"


def _make_contract(
    *,
    max_cost_usd: float = 10.0,
    phases: list[ModelOvernightPhaseSpec] | None = None,
    halt_conditions: list[ModelOvernightHaltCondition] | None = None,
) -> ModelOvernightContract:
    """Build a minimal ModelOvernightContract for testing."""
    default_phases = [
        ModelOvernightPhaseSpec(phase_name=phase.value)
        for phase in [
            EnumPhase.BUILD_LOOP,
            EnumPhase.MERGE_SWEEP,
            EnumPhase.CI_WATCH,
            EnumPhase.PLATFORM_READINESS,
        ]
    ]
    default_halt: list[ModelOvernightHaltCondition] = [
        ModelOvernightHaltCondition(
            condition_id="cost_ceiling",
            description="Stop if cost exceeds ceiling",
            check_type="cost_ceiling",
            threshold=max_cost_usd,
        )
    ]
    return ModelOvernightContract(
        session_id="test-session-contract",
        created_at=datetime.now(tz=UTC),
        max_cost_usd=max_cost_usd,
        phases=tuple(phases if phases is not None else default_phases),
        halt_conditions=tuple(
            halt_conditions if halt_conditions is not None else default_halt
        ),
    )


@pytest.mark.unit
class TestOvernightGoldenChain:
    """Golden chain: command -> FSM sequencing -> completion event."""

    async def test_dry_run_all_phases_succeed(
        self, event_bus: EventBusInmemory
    ) -> None:
        """dry_run mode should run all 5 phases and return COMPLETED."""
        handler = HandlerOvernight()
        command = ModelOvernightCommand(
            correlation_id="dry-001",
            dry_run=True,
        )
        result = handler.handle(command)

        assert result.session_status == EnumOvernightStatus.COMPLETED
        assert len(result.phases_run) == 5
        assert len(result.phases_failed) == 0
        assert result.dry_run is True

    async def test_dry_run_phase_sequence_order(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Phases should execute in canonical order."""
        handler = HandlerOvernight()
        command = ModelOvernightCommand(
            correlation_id="order-test",
            dry_run=True,
        )
        result = handler.handle(command)

        expected_order = [
            EnumPhase.NIGHTLY_LOOP.value,
            EnumPhase.BUILD_LOOP.value,
            EnumPhase.MERGE_SWEEP.value,
            EnumPhase.CI_WATCH.value,
            EnumPhase.PLATFORM_READINESS.value,
        ]
        assert result.phases_run == expected_order

    async def test_skip_build_loop_removes_from_run(
        self, event_bus: EventBusInmemory
    ) -> None:
        """skip_build_loop should exclude build_loop_orchestrator from phases_run."""
        handler = HandlerOvernight()
        command = ModelOvernightCommand(
            correlation_id="skip-build-test",
            skip_build_loop=True,
            dry_run=True,
        )
        result = handler.handle(command)

        assert EnumPhase.BUILD_LOOP.value not in result.phases_run
        assert EnumPhase.BUILD_LOOP.value in result.phases_skipped
        assert result.session_status == EnumOvernightStatus.COMPLETED

    async def test_skip_merge_sweep_removes_from_run(
        self, event_bus: EventBusInmemory
    ) -> None:
        """skip_merge_sweep should exclude merge_sweep from phases_run."""
        handler = HandlerOvernight()
        command = ModelOvernightCommand(
            correlation_id="skip-merge-test",
            skip_merge_sweep=True,
            dry_run=True,
        )
        result = handler.handle(command)

        assert EnumPhase.MERGE_SWEEP.value not in result.phases_run
        assert EnumPhase.MERGE_SWEEP.value in result.phases_skipped
        assert result.session_status == EnumOvernightStatus.COMPLETED

    async def test_skip_both_optional_phases(self, event_bus: EventBusInmemory) -> None:
        """Skipping both optional phases still completes with nightly_loop, ci_watch, platform_readiness."""
        handler = HandlerOvernight()
        command = ModelOvernightCommand(
            correlation_id="skip-both",
            skip_build_loop=True,
            skip_merge_sweep=True,
            dry_run=True,
        )
        result = handler.handle(command)

        assert len(result.phases_run) == 3
        assert EnumPhase.NIGHTLY_LOOP.value in result.phases_run
        assert EnumPhase.CI_WATCH.value in result.phases_run
        assert EnumPhase.PLATFORM_READINESS.value in result.phases_run
        assert result.session_status == EnumOvernightStatus.COMPLETED

    async def test_phase_failure_builds_partial_status(
        self, event_bus: EventBusInmemory
    ) -> None:
        """A non-build-loop phase failing yields PARTIAL status."""
        handler = HandlerOvernight()
        command = ModelOvernightCommand(
            correlation_id="partial-test",
            dry_run=False,
        )
        # ci_watch fails, everything else succeeds
        phase_results = {
            EnumPhase.NIGHTLY_LOOP: True,
            EnumPhase.BUILD_LOOP: True,
            EnumPhase.MERGE_SWEEP: True,
            EnumPhase.CI_WATCH: False,
            EnumPhase.PLATFORM_READINESS: True,
        }
        result = handler.handle(command, phase_results=phase_results)

        assert result.session_status == EnumOvernightStatus.PARTIAL
        assert EnumPhase.CI_WATCH.value in result.phases_failed
        assert EnumPhase.BUILD_LOOP.value in result.phases_run

    async def test_build_loop_failure_halts_pipeline(
        self, event_bus: EventBusInmemory
    ) -> None:
        """build_loop failure should halt the pipeline immediately."""
        handler = HandlerOvernight()
        command = ModelOvernightCommand(
            correlation_id="halt-test",
            dry_run=False,
        )
        phase_results = {
            EnumPhase.NIGHTLY_LOOP: True,
            EnumPhase.BUILD_LOOP: False,
        }
        result = handler.handle(command, phase_results=phase_results)

        assert EnumPhase.BUILD_LOOP.value in result.phases_failed
        # merge_sweep and later should NOT have run
        assert EnumPhase.MERGE_SWEEP.value not in result.phases_run
        # nightly_loop succeeded, build_loop failed → PARTIAL (1 failed, 2 ran)
        assert result.session_status == EnumOvernightStatus.PARTIAL

    async def test_all_phases_fail_yields_failed_status(
        self, event_bus: EventBusInmemory
    ) -> None:
        """nightly_loop failing alone = FAILED (halts immediately, nothing else ran)."""
        handler = HandlerOvernight()
        command = ModelOvernightCommand(
            correlation_id="all-fail",
            dry_run=False,
        )
        # nightly_loop fails first — pipeline halts before any other phase
        phase_results = {
            EnumPhase.NIGHTLY_LOOP: False,
        }
        result = handler.handle(command, phase_results=phase_results)

        assert result.session_status == EnumOvernightStatus.FAILED
        assert EnumPhase.NIGHTLY_LOOP.value in result.phases_failed
        assert EnumPhase.BUILD_LOOP.value not in result.phases_run

    async def test_correlation_id_preserved(self, event_bus: EventBusInmemory) -> None:
        """correlation_id should round-trip through the result."""
        handler = HandlerOvernight()
        command = ModelOvernightCommand(
            correlation_id="preserve-id-xyz",
            dry_run=True,
        )
        result = handler.handle(command)

        assert result.correlation_id == "preserve-id-xyz"

    async def test_timestamps_set(self, event_bus: EventBusInmemory) -> None:
        """started_at and completed_at should both be set."""
        handler = HandlerOvernight()
        command = ModelOvernightCommand(
            correlation_id="ts-test",
            dry_run=True,
        )
        result = handler.handle(command)

        assert result.started_at is not None
        assert result.completed_at is not None
        assert result.completed_at >= result.started_at

    async def test_event_bus_wiring(self, event_bus: EventBusInmemory) -> None:
        """Handler can be wired to event bus and process command events."""
        handler = HandlerOvernight()
        results_captured: list[dict] = []  # type: ignore[type-arg]

        async def on_command(message: object) -> None:
            payload = json.loads(message.value)  # type: ignore[union-attr]
            command = ModelOvernightCommand(**payload)
            result = handler.handle(command)
            result_payload = result.model_dump(mode="json")
            results_captured.append(result_payload)
            await event_bus.publish(
                EVT_TOPIC,
                key=None,
                value=json.dumps(result_payload).encode(),
            )

        await event_bus.start()
        await event_bus.subscribe(
            CMD_TOPIC, on_message=on_command, group_id="test-overnight"
        )

        cmd_payload = json.dumps(
            {
                "correlation_id": "bus-test-overnight",
                "dry_run": True,
            }
        ).encode()
        await event_bus.publish(CMD_TOPIC, key=None, value=cmd_payload)

        assert len(results_captured) == 1
        assert results_captured[0]["session_status"] == "completed"

        history = await event_bus.get_event_history(topic=EVT_TOPIC)
        assert len(history) == 1

        await event_bus.close()

    async def test_result_serializes_to_json(self, event_bus: EventBusInmemory) -> None:
        """Result should serialize cleanly to JSON."""
        handler = HandlerOvernight()
        command = ModelOvernightCommand(
            correlation_id="json-test-overnight",
            dry_run=True,
        )
        result = handler.handle(command)
        serialized = result.model_dump_json()
        parsed = json.loads(serialized)

        assert parsed["session_status"] == "completed"
        assert parsed["dry_run"] is True
        assert len(parsed["phases_run"]) == 5


@pytest.mark.unit
class TestOvernightContractEnforcement:
    """Contract enforcement: cost ceiling and halt-on-failure checks."""

    async def test_no_contract_behaves_as_before(
        self, event_bus: EventBusInmemory
    ) -> None:
        """Without a contract, handler behaves exactly as before this change."""
        handler = HandlerOvernight()
        command = ModelOvernightCommand(
            correlation_id="no-contract",
            dry_run=True,
        )
        result = handler.handle(command)

        assert result.session_status == EnumOvernightStatus.COMPLETED
        assert result.halt_reason is None
        assert len(result.phases_run) == 5

    async def test_cost_ceiling_not_exceeded_completes(
        self, event_bus: EventBusInmemory
    ) -> None:
        """When accumulated cost stays below ceiling, pipeline completes normally."""
        contract = _make_contract(max_cost_usd=10.0)
        handler = HandlerOvernight()
        command = ModelOvernightCommand(
            correlation_id="cost-ok",
            dry_run=False,
            overnight_contract=contract,
        )
        phase_costs = {
            EnumPhase.BUILD_LOOP: 1.0,
            EnumPhase.MERGE_SWEEP: 0.5,
            EnumPhase.CI_WATCH: 0.5,
            EnumPhase.PLATFORM_READINESS: 0.5,
        }
        result = handler.handle(command, phase_costs=phase_costs)

        assert result.session_status == EnumOvernightStatus.COMPLETED
        assert result.halt_reason is None

    async def test_cost_ceiling_exceeded_halts(
        self, event_bus: EventBusInmemory
    ) -> None:
        """When cost hits or exceeds ceiling after a phase, pipeline halts."""
        contract = _make_contract(max_cost_usd=1.5)
        handler = HandlerOvernight()
        command = ModelOvernightCommand(
            correlation_id="cost-halt",
            dry_run=False,
            overnight_contract=contract,
        )
        # BUILD_LOOP costs 1.0, MERGE_SWEEP pushes to 2.0 — ceiling is 1.5
        phase_costs = {
            EnumPhase.BUILD_LOOP: 1.0,
            EnumPhase.MERGE_SWEEP: 1.0,
        }
        result = handler.handle(command, phase_costs=phase_costs)

        assert result.halt_reason is not None
        assert "cost_ceiling" in result.halt_reason
        assert result.session_status == EnumOvernightStatus.FAILED
        # Pipeline stopped — platform_readiness should not have run
        assert EnumPhase.PLATFORM_READINESS.value not in result.phases_run

    async def test_halt_on_failure_triggers_halt(
        self, event_bus: EventBusInmemory
    ) -> None:
        """A phase with halt_on_failure=True that fails halts the pipeline."""
        phases = [
            ModelOvernightPhaseSpec(
                phase_name=EnumPhase.BUILD_LOOP.value, halt_on_failure=True
            ),
            ModelOvernightPhaseSpec(phase_name=EnumPhase.MERGE_SWEEP.value),
            ModelOvernightPhaseSpec(phase_name=EnumPhase.CI_WATCH.value),
            ModelOvernightPhaseSpec(phase_name=EnumPhase.PLATFORM_READINESS.value),
        ]
        contract = _make_contract(phases=phases)
        handler = HandlerOvernight()
        command = ModelOvernightCommand(
            correlation_id="halt-on-fail",
            dry_run=False,
            overnight_contract=contract,
        )
        result = handler.handle(
            command,
            phase_results={EnumPhase.BUILD_LOOP: False},
        )

        assert result.halt_reason is not None
        assert "halt_on_failure" in result.halt_reason
        assert EnumPhase.MERGE_SWEEP.value not in result.phases_run

    async def test_halt_on_failure_false_continues(
        self, event_bus: EventBusInmemory
    ) -> None:
        """A phase with halt_on_failure=False that fails does not trigger contract halt."""
        phases = [
            ModelOvernightPhaseSpec(phase_name=EnumPhase.BUILD_LOOP.value),
            ModelOvernightPhaseSpec(
                phase_name=EnumPhase.MERGE_SWEEP.value, halt_on_failure=False
            ),
            ModelOvernightPhaseSpec(phase_name=EnumPhase.CI_WATCH.value),
            ModelOvernightPhaseSpec(phase_name=EnumPhase.PLATFORM_READINESS.value),
        ]
        contract = _make_contract(phases=phases)
        handler = HandlerOvernight()
        command = ModelOvernightCommand(
            correlation_id="no-halt-on-fail",
            dry_run=False,
            overnight_contract=contract,
        )
        result = handler.handle(
            command,
            phase_results={
                EnumPhase.BUILD_LOOP: True,
                EnumPhase.MERGE_SWEEP: False,
                EnumPhase.CI_WATCH: True,
                EnumPhase.PLATFORM_READINESS: True,
            },
        )

        # No contract halt — pipeline continues past failed merge_sweep
        assert result.halt_reason is None
        assert EnumPhase.CI_WATCH.value in result.phases_run
        assert EnumPhase.PLATFORM_READINESS.value in result.phases_run
        assert result.session_status == EnumOvernightStatus.PARTIAL

    async def test_contract_halt_result_serializes(
        self, event_bus: EventBusInmemory
    ) -> None:
        """ModelOvernightResult with halt_reason serializes cleanly to JSON."""
        contract = _make_contract(max_cost_usd=0.01)
        handler = HandlerOvernight()
        command = ModelOvernightCommand(
            correlation_id="serial-test",
            dry_run=False,
            overnight_contract=contract,
        )
        phase_costs = {EnumPhase.BUILD_LOOP: 1.0}
        result = handler.handle(command, phase_costs=phase_costs)

        parsed = json.loads(result.model_dump_json())
        assert "halt_reason" in parsed
        assert parsed["halt_reason"] is not None
