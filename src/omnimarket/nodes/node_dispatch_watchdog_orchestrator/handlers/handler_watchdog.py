# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDispatchWatchdogOrchestrator — Epic-level wave stall monitor.

ONEX node type: ORCHESTRATOR — impure, effectful.

Computes stall state deterministically from wave-task state — injected directly,
or loaded from ``<state_dir>/epics/<epic_id>/state.yaml`` when only ``epic_id``
is given (the only CLI-expressible shape; OMN-17017 made it reachable instead of
an unconditional ``RuntimeError``). Cancel, redispatch, escalation and the
durable check record go through ``ProtocolWatchdogRecoveryAdapter``;
``HandlerWatchdogStateStore`` is the shipped default, so a live run works
without injection and every pass leaves a durable record.

Algorithm (per dispatch_watchdog SKILL.md):
  1. For each active task in the wave, call TaskGet() to read last_activity timestamp.
  2. Compute elapsed seconds since last tool call.
  3. Apply Bash long-timeout exemption: if last_tool_name == "Bash" and
     last_tool_timeout_ms > 120000, extend threshold to (timeout_ms/1000 + 60s).
  4. If elapsed > effective_timeout: stall_detected.
     - action == "report"     → log only.
     - action == "cancel"     → SendMessage shutdown_request.
     - action == "redispatch" → kill + redispatch with narrower scope.
  5. If redispatch_count >= max_redispatches: escalate to Blocked in Linear,
     write friction event to .onex_state/friction/, log to dispatch-log.
  6. Write watchdog.json + append to dispatch-log/{date}.ndjson (recovery
     adapter's record_check; the returned paths are reported on the result).
  7. Emit ModelWatchdogResult.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_dispatch_watchdog_orchestrator.handlers.handler_watchdog_state_store import (
    HandlerWatchdogEpicStateReader,
    HandlerWatchdogStateStore,
    ModelWatchdogStatePaths,
    resolve_state_dir,
)
from omnimarket.nodes.node_dispatch_watchdog_orchestrator.models.model_watchdog import (
    EnumRecoveryAction,
    EnumTaskStatus,
    ModelRecoveryAction,
    ModelStallEvent,
    ModelWatchdogResult,
    ModelWatchdogSummary,
    ModelWaveTask,
)

# ---------------------------------------------------------------------------
# Request model (lives here so contract.yaml input_model path is canonical)
# ---------------------------------------------------------------------------

_DEFAULT_CHECK_INTERVAL = 30
_DEFAULT_STALL_TIMEOUT = 120
_DEFAULT_MAX_REDISPATCHES = 2


class ModelWatchdogRequest(BaseModel):
    """Input envelope for the dispatch watchdog orchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    epic_id: str | None = Field(
        default=None,
        description=(
            "Epic ID to monitor. When set and wave_tasks is empty, the watchdog "
            "reads wave state from <state_dir>/epics/<epic_id>/state.yaml. If "
            "None, wave_tasks must be provided directly."
        ),
    )
    state_dir: str | None = Field(
        default=None,
        description=(
            "Durable state root for epic state, watchdog.json, the friction "
            "event and the dispatch log. When unset it resolves from "
            "$ONEX_STATE_DIR, else $OMNI_HOME/.onex_state."
        ),
    )
    wave_tasks: tuple[ModelWaveTask, ...] = Field(
        default=(),
        description=(
            "Explicit list of wave tasks to monitor. Used when epic_id is not set "
            "or when the caller injects task state directly."
        ),
    )
    check_interval_seconds: int = Field(
        default=_DEFAULT_CHECK_INTERVAL,
        ge=5,
        description="Polling interval in seconds between watchdog checks.",
    )
    stall_timeout_seconds: int = Field(
        default=_DEFAULT_STALL_TIMEOUT,
        ge=10,
        description=(
            "Inactivity threshold in seconds before a task is declared stalled. "
            "Extended automatically for long-running Bash calls."
        ),
    )
    max_redispatches: int = Field(
        default=_DEFAULT_MAX_REDISPATCHES,
        ge=1,
        description="Max redispatch attempts per task before escalation to Blocked.",
    )
    action: EnumRecoveryAction = Field(
        default=EnumRecoveryAction.REDISPATCH,
        description="Recovery action to take on stall detection.",
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "When True, log stall events but do not kill/redispatch agents or "
            "mutate Linear state."
        ),
    )


class ProtocolWatchdogEpicStateReader(Protocol):
    """Adapter boundary for loading a wave's task state by epic id."""

    def read_wave_tasks(self, epic_id: str) -> tuple[ModelWaveTask, ...]: ...


class ProtocolWatchdogRecoveryAdapter(Protocol):
    """Adapter boundary for live watchdog recovery side effects."""

    def cancel_task(self, task: ModelWaveTask) -> None: ...

    def redispatch_task(self, task: ModelWaveTask) -> str: ...

    def escalate_to_blocked(self, task: ModelWaveTask) -> str | None: ...

    def record_check(self, result: ModelWatchdogResult) -> ModelWatchdogStatePaths: ...


class HandlerDispatchWatchdogOrchestrator:
    """ORCHESTRATOR — epic-level wave stall monitor and recovery dispatcher.

    Dry-run and report mode only compute output records. Mutating recovery
    actions require ``ProtocolWatchdogRecoveryAdapter``.
    """

    def __init__(
        self,
        recovery_adapter: ProtocolWatchdogRecoveryAdapter | None = None,
        *,
        now_utc: datetime | None = None,
        epic_state_reader: ProtocolWatchdogEpicStateReader | None = None,
    ) -> None:
        self._recovery_adapter = recovery_adapter
        self._now_utc = now_utc
        self._epic_state_reader = epic_state_reader

    def handle(self, request: ModelWatchdogRequest) -> ModelWatchdogResult:
        """Run one watchdog check pass over the wave tasks.

        Raises:
            FileNotFoundError: when ``epic_id`` names a wave with no state file.
        """
        wave_tasks = self._wave_tasks(request)
        adapter = self._recovery_adapter or HandlerWatchdogStateStore(
            state_dir=resolve_state_dir(request.state_dir),
            now_utc=self._now_utc,
        )

        now = self._now_utc or datetime.now(UTC)
        stall_events: list[ModelStallEvent] = []
        recovery_actions: list[ModelRecoveryAction] = []
        healthy_task_ids: list[str] = []

        for task in wave_tasks:
            stall_event = _stall_event(task, request, now)
            if stall_event is None:
                if task.status is EnumTaskStatus.IN_PROGRESS:
                    healthy_task_ids.append(task.task_id)
                continue

            recovery_action, recorded_stall_event = self._recover(
                adapter, task, request, stall_event
            )
            stall_events.append(recorded_stall_event)
            recovery_actions.append(recovery_action)

        blocked = sum(1 for action in recovery_actions if action.escalated_to_blocked)
        redispatched = sum(
            1
            for action in recovery_actions
            if action.action is EnumRecoveryAction.REDISPATCH
            and not action.escalated_to_blocked
        )
        cancelled = sum(
            1
            for action in recovery_actions
            if action.action is EnumRecoveryAction.CANCEL
        )
        result = ModelWatchdogResult(
            epic_id=request.epic_id,
            check_timestamp_utc=now.isoformat().replace("+00:00", "Z"),
            healthy_task_ids=tuple(healthy_task_ids),
            stall_events=tuple(stall_events),
            recovery_actions=tuple(recovery_actions),
            summary=ModelWatchdogSummary(
                total_tasks=len(wave_tasks),
                healthy=len(healthy_task_ids),
                stalled=len(stall_events),
                blocked=blocked,
                redispatched=redispatched,
                cancelled=cancelled,
            ),
            watchdog_log_path=None,
            dispatch_log_path=None,
        )
        # Every pass leaves a durable record — including dry_run, where the
        # record IS the deliverable. OMN-17017: the contract promised
        # watchdog.json + a dispatch-log line and the handler hardcoded None.
        paths = adapter.record_check(result)
        return result.model_copy(
            update={
                "watchdog_log_path": paths.watchdog_log_path,
                "dispatch_log_path": paths.dispatch_log_path,
            }
        )

    def _wave_tasks(self, request: ModelWatchdogRequest) -> tuple[ModelWaveTask, ...]:
        if request.wave_tasks:
            return request.wave_tasks
        if not request.epic_id:
            return ()
        reader = self._epic_state_reader or HandlerWatchdogEpicStateReader(
            state_dir=resolve_state_dir(request.state_dir)
        )
        return reader.read_wave_tasks(request.epic_id)

    def _recover(
        self,
        adapter: ProtocolWatchdogRecoveryAdapter,
        task: ModelWaveTask,
        request: ModelWatchdogRequest,
        stall_event: ModelStallEvent,
    ) -> tuple[ModelRecoveryAction, ModelStallEvent]:
        if (
            request.action is EnumRecoveryAction.REDISPATCH
            and task.redispatch_count >= request.max_redispatches
        ):
            friction_path = None
            if not request.dry_run:
                friction_path = adapter.escalate_to_blocked(task)
            return (
                ModelRecoveryAction(
                    ticket_id=task.ticket_id,
                    task_id=task.task_id,
                    action=request.action,
                    escalated_to_blocked=True,
                    friction_event_path=friction_path,
                ),
                stall_event,
            )

        if not request.dry_run and request.action is not EnumRecoveryAction.REPORT:
            if request.action is EnumRecoveryAction.CANCEL:
                adapter.cancel_task(task)
            elif request.action is EnumRecoveryAction.REDISPATCH:
                recovery_task_id = adapter.redispatch_task(task)
                stall_event = stall_event.model_copy(
                    update={"recovery_task_id": recovery_task_id}
                )

        return (
            ModelRecoveryAction(
                ticket_id=task.ticket_id,
                task_id=task.task_id,
                action=request.action,
                escalated_to_blocked=False,
                friction_event_path=None,
            ),
            stall_event,
        )


def _stall_event(
    task: ModelWaveTask, request: ModelWatchdogRequest, now: datetime
) -> ModelStallEvent | None:
    if task.status is not EnumTaskStatus.IN_PROGRESS:
        return None
    last_activity = _parse_timestamp(task.last_activity_ts)
    idle_seconds = (
        request.stall_timeout_seconds + 1.0
        if last_activity is None
        else max(0.0, (now - last_activity).total_seconds())
    )
    effective_timeout, bash_exemption = _effective_timeout(task, request)
    if idle_seconds <= effective_timeout:
        return None
    return ModelStallEvent(
        task_id=task.task_id,
        ticket_id=task.ticket_id,
        last_activity_ts=task.last_activity_ts,
        idle_seconds=idle_seconds,
        bash_timeout_exemption=bash_exemption,
        effective_timeout_seconds=effective_timeout,
        action_taken=request.action,
        redispatch_attempt=(
            task.redispatch_count + 1
            if request.action is EnumRecoveryAction.REDISPATCH
            else task.redispatch_count
        ),
        recovery_task_id=None,
    )


def _effective_timeout(
    task: ModelWaveTask, request: ModelWatchdogRequest
) -> tuple[float, bool]:
    if (
        task.last_tool_name == "Bash"
        and task.last_tool_timeout_ms is not None
        and task.last_tool_timeout_ms > 120_000
    ):
        return (task.last_tool_timeout_ms / 1000.0) + 60.0, True
    return float(request.stall_timeout_seconds), False


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


__all__ = [
    "HandlerDispatchWatchdogOrchestrator",
    "ModelWatchdogRequest",
    "ProtocolWatchdogEpicStateReader",
    "ProtocolWatchdogRecoveryAdapter",
]
