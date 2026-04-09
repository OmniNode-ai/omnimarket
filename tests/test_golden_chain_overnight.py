"""Golden chain test for node_overnight.

Verifies the overnight FSM orchestrator correctly sequences phases,
handles skip flags, and derives terminal status. All tests use dry_run=True
or inject phase_results — no subprocess calls.
"""

from __future__ import annotations

import json

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_overnight.handlers.handler_overnight import (
    EnumOvernightStatus,
    EnumPhase,
    HandlerOvernight,
    ModelOvernightCommand,
)

CMD_TOPIC = "onex.cmd.omnimarket.overnight-start.v1"
EVT_TOPIC = "onex.evt.omnimarket.overnight.session-completed.v1"


@pytest.mark.unit
class TestOvernightGoldenChain:
    """Golden chain: command -> FSM sequencing -> completion event."""

    async def test_dry_run_all_phases_succeed(
        self, event_bus: EventBusInmemory
    ) -> None:
        """dry_run mode should run all 4 phases and return COMPLETED."""
        handler = HandlerOvernight()
        command = ModelOvernightCommand(
            correlation_id="dry-001",
            dry_run=True,
        )
        result = handler.handle(command)

        assert result.session_status == EnumOvernightStatus.COMPLETED
        assert len(result.phases_run) == 4
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
        """Skipping both optional phases still completes with ci_watch and platform_readiness."""
        handler = HandlerOvernight()
        command = ModelOvernightCommand(
            correlation_id="skip-both",
            skip_build_loop=True,
            skip_merge_sweep=True,
            dry_run=True,
        )
        result = handler.handle(command)

        assert len(result.phases_run) == 2
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
            EnumPhase.BUILD_LOOP: False,
        }
        result = handler.handle(command, phase_results=phase_results)

        assert EnumPhase.BUILD_LOOP.value in result.phases_failed
        # merge_sweep and later should NOT have run
        assert EnumPhase.MERGE_SWEEP.value not in result.phases_run
        assert result.session_status == EnumOvernightStatus.FAILED

    async def test_all_phases_fail_yields_failed_status(
        self, event_bus: EventBusInmemory
    ) -> None:
        """build_loop failing alone = FAILED (halts immediately, nothing else ran)."""
        handler = HandlerOvernight()
        command = ModelOvernightCommand(
            correlation_id="all-fail",
            dry_run=False,
        )
        phase_results = {
            EnumPhase.BUILD_LOOP: False,
            EnumPhase.MERGE_SWEEP: False,
            EnumPhase.CI_WATCH: False,
            EnumPhase.PLATFORM_READINESS: False,
        }
        result = handler.handle(command, phase_results=phase_results)

        assert result.session_status == EnumOvernightStatus.FAILED

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
        assert len(parsed["phases_run"]) == 4
