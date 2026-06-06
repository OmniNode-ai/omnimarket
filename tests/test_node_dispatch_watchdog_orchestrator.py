"""Unit tests for node_dispatch_watchdog_orchestrator."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_dispatch_watchdog_orchestrator import (
    EnumRecoveryAction,
    EnumTaskStatus,
    HandlerDispatchWatchdogOrchestrator,
    ModelRecoveryAction,
    ModelStallEvent,
    ModelWatchdogRequest,
    ModelWatchdogResult,
    ModelWatchdogSummary,
    ModelWaveTask,
)

# ---------------------------------------------------------------------------
# Import / public surface
# ---------------------------------------------------------------------------


class TestPublicSurface:
    @pytest.mark.unit
    def test_all_symbols_importable(self) -> None:
        assert EnumRecoveryAction is not None
        assert EnumTaskStatus is not None
        assert HandlerDispatchWatchdogOrchestrator is not None
        assert ModelRecoveryAction is not None
        assert ModelStallEvent is not None
        assert ModelWatchdogRequest is not None
        assert ModelWatchdogResult is not None
        assert ModelWatchdogSummary is not None
        assert ModelWaveTask is not None


# ---------------------------------------------------------------------------
# EnumRecoveryAction
# ---------------------------------------------------------------------------


class TestEnumRecoveryAction:
    @pytest.mark.unit
    def test_all_members(self) -> None:
        assert EnumRecoveryAction.REPORT.value == "report"
        assert EnumRecoveryAction.CANCEL.value == "cancel"
        assert EnumRecoveryAction.REDISPATCH.value == "redispatch"


# ---------------------------------------------------------------------------
# EnumTaskStatus
# ---------------------------------------------------------------------------


class TestEnumTaskStatus:
    @pytest.mark.unit
    def test_all_members(self) -> None:
        assert EnumTaskStatus.IN_PROGRESS.value == "in_progress"
        assert EnumTaskStatus.COMPLETED.value == "completed"
        assert EnumTaskStatus.FAILED.value == "failed"
        assert EnumTaskStatus.BLOCKED.value == "blocked"
        assert EnumTaskStatus.UNKNOWN.value == "unknown"


# ---------------------------------------------------------------------------
# ModelWaveTask
# ---------------------------------------------------------------------------


class TestModelWaveTask:
    @pytest.mark.unit
    def test_minimal_valid(self) -> None:
        task = ModelWaveTask(
            task_id="task-abc123",
            ticket_id="OMN-1234",
            team_name="wave-1",
        )
        assert task.task_id == "task-abc123"
        assert task.ticket_id == "OMN-1234"
        assert task.status == EnumTaskStatus.IN_PROGRESS
        assert task.redispatch_count == 0
        assert task.last_tool_name is None

    @pytest.mark.unit
    def test_with_bash_timeout(self) -> None:
        task = ModelWaveTask(
            task_id="task-xyz",
            ticket_id="OMN-5678",
            team_name="wave-2",
            last_tool_name="Bash",
            last_tool_timeout_ms=300000,
        )
        assert task.last_tool_timeout_ms == 300000

    @pytest.mark.unit
    def test_redispatch_count_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            ModelWaveTask(
                task_id="t",
                ticket_id="OMN-1",
                team_name="w",
                redispatch_count=-1,
            )

    @pytest.mark.unit
    def test_frozen(self) -> None:
        task = ModelWaveTask(task_id="t", ticket_id="OMN-1", team_name="w")
        with pytest.raises(ValidationError):
            task.redispatch_count = 5  # type: ignore[misc]

    @pytest.mark.unit
    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ModelWaveTask(
                task_id="t",
                ticket_id="OMN-1",
                team_name="w",
                surprise="bad",  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# ModelStallEvent
# ---------------------------------------------------------------------------


class TestModelStallEvent:
    @pytest.mark.unit
    def test_valid_stall_event(self) -> None:
        event = ModelStallEvent(
            task_id="task-abc",
            ticket_id="OMN-1234",
            idle_seconds=145.0,
            effective_timeout_seconds=120.0,
            action_taken=EnumRecoveryAction.REDISPATCH,
            redispatch_attempt=1,
        )
        assert event.idle_seconds == 145.0
        assert event.bash_timeout_exemption is False
        assert event.recovery_task_id is None

    @pytest.mark.unit
    def test_with_bash_exemption(self) -> None:
        event = ModelStallEvent(
            task_id="task-bash",
            ticket_id="OMN-9999",
            idle_seconds=400.0,
            effective_timeout_seconds=360.0,
            bash_timeout_exemption=True,
            action_taken=EnumRecoveryAction.CANCEL,
            redispatch_attempt=0,
        )
        assert event.bash_timeout_exemption is True

    @pytest.mark.unit
    def test_idle_seconds_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            ModelStallEvent(
                task_id="t",
                ticket_id="OMN-1",
                idle_seconds=-1.0,
                effective_timeout_seconds=120.0,
                action_taken=EnumRecoveryAction.REPORT,
                redispatch_attempt=0,
            )

    @pytest.mark.unit
    def test_frozen(self) -> None:
        event = ModelStallEvent(
            task_id="t",
            ticket_id="OMN-1",
            idle_seconds=10.0,
            effective_timeout_seconds=120.0,
            action_taken=EnumRecoveryAction.REPORT,
            redispatch_attempt=0,
        )
        with pytest.raises(ValidationError):
            event.idle_seconds = 999.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ModelRecoveryAction
# ---------------------------------------------------------------------------


class TestModelRecoveryAction:
    @pytest.mark.unit
    def test_valid_recovery_action(self) -> None:
        action = ModelRecoveryAction(
            ticket_id="OMN-5678",
            task_id="task-xyz",
            action=EnumRecoveryAction.REDISPATCH,
        )
        assert action.escalated_to_blocked is False
        assert action.friction_event_path is None

    @pytest.mark.unit
    def test_escalated(self) -> None:
        action = ModelRecoveryAction(
            ticket_id="OMN-0001",
            task_id="task-escalated",
            action=EnumRecoveryAction.CANCEL,
            escalated_to_blocked=True,
            friction_event_path=".onex_state/friction/2026-05-25-stall-escalation-omn-0001.md",
        )
        assert action.escalated_to_blocked is True
        assert action.friction_event_path is not None

    @pytest.mark.unit
    def test_frozen(self) -> None:
        action = ModelRecoveryAction(
            ticket_id="OMN-1",
            task_id="t",
            action=EnumRecoveryAction.REPORT,
        )
        with pytest.raises(ValidationError):
            action.action = EnumRecoveryAction.CANCEL  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ModelWatchdogSummary
# ---------------------------------------------------------------------------


class TestModelWatchdogSummary:
    @pytest.mark.unit
    def test_all_zero(self) -> None:
        s = ModelWatchdogSummary(
            total_tasks=0,
            healthy=0,
            stalled=0,
            blocked=0,
            redispatched=0,
            cancelled=0,
        )
        assert s.total_tasks == 0

    @pytest.mark.unit
    def test_typical_wave(self) -> None:
        s = ModelWatchdogSummary(
            total_tasks=5,
            healthy=3,
            stalled=1,
            blocked=0,
            redispatched=1,
            cancelled=0,
        )
        assert s.healthy == 3
        assert s.stalled == 1

    @pytest.mark.unit
    def test_non_negative_fields(self) -> None:
        with pytest.raises(ValidationError):
            ModelWatchdogSummary(
                total_tasks=-1,
                healthy=0,
                stalled=0,
                blocked=0,
                redispatched=0,
                cancelled=0,
            )

    @pytest.mark.unit
    def test_frozen(self) -> None:
        s = ModelWatchdogSummary(
            total_tasks=1,
            healthy=1,
            stalled=0,
            blocked=0,
            redispatched=0,
            cancelled=0,
        )
        with pytest.raises(ValidationError):
            s.total_tasks = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ModelWatchdogRequest
# ---------------------------------------------------------------------------


class TestModelWatchdogRequest:
    @pytest.mark.unit
    def test_defaults(self) -> None:
        req = ModelWatchdogRequest()
        assert req.epic_id is None
        assert req.wave_tasks == ()
        assert req.check_interval_seconds == 30
        assert req.stall_timeout_seconds == 120
        assert req.max_redispatches == 2
        assert req.action == EnumRecoveryAction.REDISPATCH
        assert req.dry_run is False

    @pytest.mark.unit
    def test_custom_values(self) -> None:
        task = ModelWaveTask(task_id="t1", ticket_id="OMN-1", team_name="wave-1")
        req = ModelWatchdogRequest(
            epic_id="OMN-2000",
            wave_tasks=(task,),
            check_interval_seconds=60,
            stall_timeout_seconds=300,
            max_redispatches=3,
            action=EnumRecoveryAction.REPORT,
            dry_run=True,
        )
        assert req.epic_id == "OMN-2000"
        assert len(req.wave_tasks) == 1
        assert req.dry_run is True

    @pytest.mark.unit
    def test_check_interval_lower_bound(self) -> None:
        with pytest.raises(ValidationError):
            ModelWatchdogRequest(check_interval_seconds=4)

    @pytest.mark.unit
    def test_stall_timeout_lower_bound(self) -> None:
        with pytest.raises(ValidationError):
            ModelWatchdogRequest(stall_timeout_seconds=9)

    @pytest.mark.unit
    def test_max_redispatches_lower_bound(self) -> None:
        with pytest.raises(ValidationError):
            ModelWatchdogRequest(max_redispatches=0)

    @pytest.mark.unit
    def test_frozen(self) -> None:
        req = ModelWatchdogRequest()
        with pytest.raises(ValidationError):
            req.epic_id = "OMN-9999"  # type: ignore[misc]

    @pytest.mark.unit
    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            ModelWatchdogRequest(unknown_field="bad")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class TestHandlerDispatchWatchdogOrchestrator:
    @pytest.mark.unit
    def test_dry_run_detects_stalled_task(self) -> None:
        handler = HandlerDispatchWatchdogOrchestrator(
            now_utc=datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
        )
        req = ModelWatchdogRequest(
            wave_tasks=(
                ModelWaveTask(
                    task_id="task-1",
                    ticket_id="OMN-1",
                    team_name="wave-1",
                    last_activity_ts="2026-05-28T11:57:00Z",
                ),
            ),
            stall_timeout_seconds=120,
            dry_run=True,
        )
        result = handler.handle(req)

        assert result.summary.stalled == 1
        assert result.summary.redispatched == 1
        assert result.stall_events[0].idle_seconds == 180

    @pytest.mark.unit
    def test_handler_instantiates_without_args(self) -> None:
        handler = HandlerDispatchWatchdogOrchestrator()
        assert handler is not None

    @pytest.mark.unit
    def test_live_redispatch_without_adapter_requires_adapter(self) -> None:
        handler = HandlerDispatchWatchdogOrchestrator(
            now_utc=datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
        )
        req = ModelWatchdogRequest(
            wave_tasks=(
                ModelWaveTask(
                    task_id="task-1",
                    ticket_id="OMN-1",
                    team_name="wave-1",
                    last_activity_ts="2026-05-28T11:57:00Z",
                ),
            ),
            stall_timeout_seconds=120,
        )

        with pytest.raises(RuntimeError, match="recovery_adapter required"):
            handler.handle(req)

    @pytest.mark.unit
    def test_report_mode_does_not_require_adapter(self) -> None:
        handler = HandlerDispatchWatchdogOrchestrator(
            now_utc=datetime(2026, 5, 28, 12, 0, tzinfo=UTC)
        )
        req = ModelWatchdogRequest(
            wave_tasks=(
                ModelWaveTask(
                    task_id="task-1",
                    ticket_id="OMN-1",
                    team_name="wave-1",
                    last_activity_ts="2026-05-28T11:57:00Z",
                ),
            ),
            action=EnumRecoveryAction.REPORT,
        )

        result = handler.handle(req)

        assert result.summary.stalled == 1
        assert result.recovery_actions[0].action is EnumRecoveryAction.REPORT
