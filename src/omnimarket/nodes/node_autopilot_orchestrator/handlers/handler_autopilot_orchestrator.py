# SPDX-License-Identifier: MIT
"""HandlerAutopilotOrchestrator — 4-phase autonomous close-out pipeline.

The handler is event-driven. It never blocks, polls or sleeps: each call takes
one event, advances the run it belongs to, publishes at most one downstream
start command and returns. A phase result exists only because a downstream
node's result event (or a timeout tick) said so — publishing a start command is
not a result.

Phases:
  A — Worktree health sweep. No node owns a worktree-sweep command topic yet, so
      nothing is dispatched and the phase is recorded ``skipped``.

  B — Merge sweep: node_pr_lifecycle_orchestrator.
      Dispatches pr-lifecycle-orchestrator-start; advances on
      pr-lifecycle-orchestrator-completed (PASS, or FAIL when its final state is
      FAILED) or pr-lifecycle-orchestrator-failed (FAIL). Never halts.

  C — Infra health gate: node_platform_diagnostics.
      Dispatches platform-diagnostics-start; advances on
      platform-diagnostics-completed (PASS, WARN, or HALT on FAIL or on a verdict
      it cannot read). HALT authority: a halt, or no result inside the window,
      stops the run.

  D — Quality sweep: node_aislop_sweep.
      Dispatches aislop-sweep-start; advances on aislop-sweep-completed.
      dod_verify is per ticket (its start command requires a ticket id), so the
      autopilot has nothing valid to send it and does not dispatch it. No
      individual halt authority.

A phase with no result event inside the window declared by the contract
(``phase_windows_seconds``) is closed by a runtime tick with a ``timeout`` result.

Circuit breaker: 3 consecutive phase failures -> FAILED.
Phase B and D failures are advisory (non-halting).
Phase C failure or timeout is a hard gate (halt).

The terminal event (completed, or failed when handling raised) is published once
per correlation id. In-flight runs are held per process: a runtime restart drops
them, and no timeout fires for a run the new process never saw.

Related: OMN-6872, OMN-6867, OMN-8087
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.nodes.node_autopilot_orchestrator.models.model_autopilot_downstream_event import (
    ModelAislopSweepCompletedEvent,
    ModelAutopilotDownstreamEvent,
    ModelPlatformDiagnosticsCompletedEvent,
    ModelPrLifecycleCompletedEvent,
    ModelPrLifecycleFailedEvent,
)
from omnimarket.nodes.node_autopilot_orchestrator.models.model_autopilot_phase_result import (
    EnumAutopilotCycleStatus,
    EnumAutopilotPhaseStatus,
    ModelAutopilotPhaseResult,
    ModelAutopilotResult,
)
from omnimarket.nodes.node_autopilot_orchestrator.models.model_autopilot_start_command import (
    ModelAutopilotStartCommand,
)

if TYPE_CHECKING:
    from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
        ProtocolEventBusPublisher,
    )

logger = logging.getLogger(__name__)

_CIRCUIT_BREAKER_THRESHOLD = 3

ModelAutopilotRequest = (
    ModelAutopilotStartCommand
    | ModelPrLifecycleCompletedEvent
    | ModelPrLifecycleFailedEvent
    | ModelPlatformDiagnosticsCompletedEvent
    | ModelAislopSweepCompletedEvent
    | ModelRuntimeTick
)


# ---------------------------------------------------------------------------
# FSM states
# ---------------------------------------------------------------------------


class EnumAutopilotFsmState(StrEnum):
    IDLE = "IDLE"
    PHASE_A_WORKTREE = "PHASE_A_WORKTREE"
    PHASE_B_MERGE_SWEEP = "PHASE_B_MERGE_SWEEP"
    PHASE_C_INFRA_GATE = "PHASE_C_INFRA_GATE"
    PHASE_D_QUALITY_SWEEPS = "PHASE_D_QUALITY_SWEEPS"
    COMPLETE = "COMPLETE"
    HALTED = "HALTED"
    FAILED = "FAILED"


_PHASE_FSM_STATE = {
    "A": EnumAutopilotFsmState.PHASE_A_WORKTREE,
    "B": EnumAutopilotFsmState.PHASE_B_MERGE_SWEEP,
    "C": EnumAutopilotFsmState.PHASE_C_INFRA_GATE,
    "D": EnumAutopilotFsmState.PHASE_D_QUALITY_SWEEPS,
}
_NEXT_PHASE = {"A": "B", "B": "C", "C": "D"}
_PHASE_WINDOW_KEY = {
    "B": "phase_b_merge_sweep",
    "C": "phase_c_infra_gate",
    "D": "phase_d_quality_sweeps",
}


@dataclass
class _PipelineState:
    """Mutable pipeline state tracked across phases."""

    fsm: EnumAutopilotFsmState = EnumAutopilotFsmState.IDLE
    consecutive_failures: int = 0
    phases_completed: int = 0
    phases_failed: int = 0
    halt_reason: str = ""
    phase_results: dict[str, ModelAutopilotPhaseResult] = field(default_factory=dict)


@dataclass
class _Run:
    """One in-flight autopilot run: its state and the result it is waiting on."""

    command: ModelAutopilotStartCommand
    state: _PipelineState = field(default_factory=_PipelineState)
    awaiting: str | None = None
    deadline: datetime | None = None


class AutopilotRunStore:
    """In-flight runs by correlation id, shared by every handler entry of the node."""

    def __init__(self) -> None:
        self._runs: dict[UUID, _Run] = {}

    def get(self, correlation_id: UUID) -> _Run | None:
        return self._runs.get(correlation_id)

    def put(self, run: _Run) -> None:
        self._runs[run.command.correlation_id] = run

    def drop(self, correlation_id: UUID) -> None:
        self._runs.pop(correlation_id, None)

    def in_flight(self) -> list[_Run]:
        return list(self._runs.values())


@lru_cache(maxsize=1)
def _shared_run_store() -> AutopilotRunStore:
    return AutopilotRunStore()


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Contract loader
# ---------------------------------------------------------------------------


def _load_contract(contract_path: Path | None = None) -> dict[str, Any]:
    path = contract_path or Path(__file__).parent.parent / "contract.yaml"
    with open(path) as f:
        data: dict[str, Any] = yaml.safe_load(f)
    return data


def _load_phase_windows(contract: dict[str, Any]) -> dict[str, timedelta]:
    declared = contract.get("phase_windows_seconds") or {}
    windows: dict[str, timedelta] = {}
    for phase, key in _PHASE_WINDOW_KEY.items():
        seconds = declared.get(key)
        if not isinstance(seconds, int) or seconds <= 0:
            raise ValueError(
                f"contract phase_windows_seconds.{key} must be a positive integer, "
                f"got {seconds!r}"
            )
        windows[phase] = timedelta(seconds=seconds)
    return windows


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class HandlerAutopilotOrchestrator:
    """4-phase autonomous close-out pipeline orchestrator (event-driven)."""

    def __init__(
        self,
        *,
        event_bus: ProtocolEventBusPublisher,
        contract_path: Path | None = None,
        clock: Callable[[], datetime] | None = None,
        run_store: AutopilotRunStore | None = None,
    ) -> None:
        contract = _load_contract(contract_path)
        publish_topics: list[str] = contract.get("event_bus", {}).get(
            "publish_topics", []
        )
        self._topic_phase_transition = next(
            (t for t in publish_topics if "phase-transition" in t), ""
        )
        self._topic_completed = next(
            (t for t in publish_topics if "orchestrator-completed" in t), ""
        )
        self._topic_failed = next(
            (t for t in publish_topics if "orchestrator-failed" in t), ""
        )
        self._topic_pr_lifecycle = next(
            (t for t in publish_topics if "pr-lifecycle-orchestrator-start" in t), ""
        )
        self._topic_platform_diagnostics = next(
            (t for t in publish_topics if "platform-diagnostics-start" in t), ""
        )
        self._topic_aislop = next(
            (t for t in publish_topics if "aislop-sweep-start" in t), ""
        )
        self._windows = _load_phase_windows(contract)
        self._event_bus = event_bus
        self._clock = clock or _utcnow
        self._runs = run_store if run_store is not None else _shared_run_store()

    async def handle(self, request: ModelAutopilotRequest) -> None:
        """Advance the run an event belongs to. Results leave on the bus."""
        if isinstance(request, ModelAutopilotStartCommand):
            await self._start(request)
        elif isinstance(request, ModelRuntimeTick):
            await self._expire_overdue(request.now)
        elif isinstance(request, ModelPrLifecycleCompletedEvent):
            await self._on_result("B", request, self._pr_lifecycle_result)
        elif isinstance(request, ModelPrLifecycleFailedEvent):
            await self._on_result("B", request, self._pr_lifecycle_failed_result)
        elif isinstance(request, ModelPlatformDiagnosticsCompletedEvent):
            await self._on_result("C", request, self._diagnostics_result)
        elif isinstance(request, ModelAislopSweepCompletedEvent):
            await self._on_result("D", request, self._aislop_result)
        else:
            raise TypeError(f"unsupported autopilot request: {type(request).__name__}")

    # -------------------------------------------------------------------------
    # Event entry points
    # -------------------------------------------------------------------------

    async def _start(self, command: ModelAutopilotStartCommand) -> None:
        logger.info(
            "[AUTOPILOT] === ENTRY === correlation_id=%s mode=%s dry_run=%s",
            command.correlation_id,
            command.mode,
            command.dry_run,
        )
        if self._runs.get(command.correlation_id) is not None:
            logger.warning(
                "[AUTOPILOT] start for a run already in flight ignored — correlation_id=%s",
                command.correlation_id,
            )
            return
        run = _Run(command=command)
        self._runs.put(run)
        try:
            run.state.fsm = EnumAutopilotFsmState.PHASE_A_WORKTREE
            await self._publish_phase_event(
                "IDLE", "PHASE_A_WORKTREE", command.correlation_id
            )
            await self._settle(run, self._phase_a_result())
        except Exception as exc:
            await self._fail_run(run, exc)

    async def _on_result(
        self,
        phase: str,
        event: ModelAutopilotDownstreamEvent,
        to_result: Callable[[Any], ModelAutopilotPhaseResult],
    ) -> None:
        correlation_id = event.correlation_id
        if correlation_id is None:
            logger.warning(
                "[AUTOPILOT] %s result without a correlation_id dropped", phase
            )
            return
        run = self._runs.get(correlation_id)
        if run is None or run.awaiting != phase:
            logger.info(
                "[AUTOPILOT] %s result ignored — correlation_id=%s awaiting=%s",
                phase,
                correlation_id,
                run.awaiting if run is not None else "no run",
            )
            return
        try:
            await self._settle(run, to_result(event))
        except Exception as exc:
            await self._fail_run(run, exc)

    async def _expire_overdue(self, now: datetime) -> None:
        for run in self._runs.in_flight():
            if run.awaiting is None or run.deadline is None or now < run.deadline:
                continue
            phase = run.awaiting
            logger.warning(
                "[AUTOPILOT-%s] no result inside the window — correlation_id=%s",
                phase,
                run.command.correlation_id,
            )
            try:
                await self._settle(run, self._timeout_result(phase))
            except Exception as exc:
                await self._fail_run(run, exc)

    # -------------------------------------------------------------------------
    # Phase results
    # -------------------------------------------------------------------------

    @staticmethod
    def _phase_a_result() -> ModelAutopilotPhaseResult:
        return ModelAutopilotPhaseResult(
            phase_id="A",
            status=EnumAutopilotPhaseStatus.SKIPPED,
            detail="no worktree-sweep command topic is declared; nothing dispatched",
        )

    @staticmethod
    def _pr_lifecycle_result(
        event: ModelPrLifecycleCompletedEvent,
    ) -> ModelAutopilotPhaseResult:
        if event.final_state.upper() == "FAILED":
            return ModelAutopilotPhaseResult(
                phase_id="B",
                status=EnumAutopilotPhaseStatus.FAIL,
                detail=f"merge sweep ended FAILED: {event.error_message or 'no error given'}",
            )
        return ModelAutopilotPhaseResult(
            phase_id="B",
            status=EnumAutopilotPhaseStatus.PASS,
            detail=f"merge sweep completed (final_state={event.final_state or 'unset'})",
        )

    @staticmethod
    def _pr_lifecycle_failed_result(
        event: ModelPrLifecycleFailedEvent,
    ) -> ModelAutopilotPhaseResult:
        return ModelAutopilotPhaseResult(
            phase_id="B",
            status=EnumAutopilotPhaseStatus.FAIL,
            detail=f"merge sweep failed: {event.error or 'no error given'}",
        )

    @staticmethod
    def _diagnostics_result(
        event: ModelPlatformDiagnosticsCompletedEvent,
    ) -> ModelAutopilotPhaseResult:
        verdict = event.overall_status.upper()
        if verdict == "PASS":
            return ModelAutopilotPhaseResult(
                phase_id="C",
                status=EnumAutopilotPhaseStatus.PASS,
                detail="platform diagnostics reported PASS",
            )
        if verdict == "WARN":
            return ModelAutopilotPhaseResult(
                phase_id="C",
                status=EnumAutopilotPhaseStatus.WARN,
                detail="platform diagnostics reported WARN",
            )
        reason = (
            "platform diagnostics reported FAIL"
            if verdict == "FAIL"
            else f"platform diagnostics verdict unreadable: {event.overall_status!r}"
        )
        return ModelAutopilotPhaseResult(
            phase_id="C",
            status=EnumAutopilotPhaseStatus.HALT,
            detail=reason,
            halt_reason=reason,
        )

    @staticmethod
    def _aislop_result(
        event: ModelAislopSweepCompletedEvent,
    ) -> ModelAutopilotPhaseResult:
        verdict = event.status.lower()
        skipped = (
            "dod_verify not dispatched: it verifies one ticket and needs a ticket id"
        )
        if verdict == "clean":
            return ModelAutopilotPhaseResult(
                phase_id="D",
                status=EnumAutopilotPhaseStatus.PASS,
                detail=f"aislop sweep clean; {skipped}",
            )
        if verdict in ("findings", "partial"):
            return ModelAutopilotPhaseResult(
                phase_id="D",
                status=EnumAutopilotPhaseStatus.WARN,
                detail=f"aislop sweep status={verdict}; {skipped}",
            )
        return ModelAutopilotPhaseResult(
            phase_id="D",
            status=EnumAutopilotPhaseStatus.FAIL,
            detail=f"aislop sweep status={event.status!r}; {skipped}",
        )

    def _timeout_result(self, phase: str) -> ModelAutopilotPhaseResult:
        seconds = int(self._windows[phase].total_seconds())
        what = {
            "B": "pr-lifecycle-orchestrator-completed or -failed",
            "C": "platform-diagnostics-completed",
            "D": "aislop-sweep-completed",
        }[phase]
        detail = f"no {what} for this correlation id within {seconds}s"
        return ModelAutopilotPhaseResult(
            phase_id=phase,
            status=EnumAutopilotPhaseStatus.TIMEOUT,
            detail=detail,
            halt_reason=f"infra gate timeout: {detail}" if phase == "C" else "",
        )

    # -------------------------------------------------------------------------
    # FSM
    # -------------------------------------------------------------------------

    async def _settle(self, run: _Run, result: ModelAutopilotPhaseResult) -> None:
        """Record a phase result, then halt, fail, finish or begin the next phase."""
        state = run.state
        phase = result.phase_id
        run.awaiting = None
        run.deadline = None
        state.phase_results[phase] = result
        self._record_phase_outcome(state, result)
        from_state = state.fsm.value

        if phase == "C" and result.status in (
            EnumAutopilotPhaseStatus.HALT,
            EnumAutopilotPhaseStatus.TIMEOUT,
        ):
            state.halt_reason = f"Phase C (infra-gate): {result.halt_reason}"
            logger.warning("[AUTOPILOT] HALT: %s", state.halt_reason)
            await self._end_run(run, EnumAutopilotFsmState.HALTED, from_state)
            return

        if phase == "D" and result.status == EnumAutopilotPhaseStatus.HALT:
            state.halt_reason = f"Phase D (quality): {result.halt_reason}"
            logger.warning("[AUTOPILOT] HALT: %s", state.halt_reason)
            await self._end_run(run, EnumAutopilotFsmState.HALTED, from_state)
            return

        if phase != "D" and self._circuit_breaker_tripped(state):
            await self._end_run(run, EnumAutopilotFsmState.FAILED, from_state)
            return

        if phase == "D":
            await self._end_run(run, EnumAutopilotFsmState.COMPLETE, from_state)
            return

        await self._begin_phase(run, _NEXT_PHASE[phase])

    async def _begin_phase(self, run: _Run, phase: str) -> None:
        state = run.state
        previous = state.fsm
        state.fsm = _PHASE_FSM_STATE[phase]
        await self._publish_phase_event(
            previous.value, state.fsm.value, run.command.correlation_id
        )
        closed = await self._dispatch(phase, run.command)
        if closed is not None:
            await self._settle(run, closed)
            return
        run.awaiting = phase
        run.deadline = self._clock() + self._windows[phase]

    async def _end_run(
        self, run: _Run, final: EnumAutopilotFsmState, from_state: str
    ) -> None:
        """Move to a terminal state and publish the one terminal event."""
        run.state.fsm = final
        await self._publish_phase_event(
            from_state, final.value, run.command.correlation_id
        )
        result = self._build_result(run.state, run.command.correlation_id)
        logger.info(
            "[AUTOPILOT] === EXIT === fsm=%s phases_completed=%d phases_failed=%d",
            final,
            run.state.phases_completed,
            run.state.phases_failed,
        )
        self._runs.drop(run.command.correlation_id)
        await self._publish_terminal(
            self._topic_completed,
            run.command.correlation_id,
            event_type="omnimarket.autopilot-orchestrator-completed",
            payload=result.model_dump(mode="json"),
        )

    async def _fail_run(self, run: _Run, exc: Exception) -> None:
        correlation_id = run.command.correlation_id
        logger.exception(
            "[AUTOPILOT] unhandled exception in %s — correlation_id=%s: %s",
            run.state.fsm,
            correlation_id,
            exc,
        )
        phase = run.state.fsm.value
        run.state.fsm = EnumAutopilotFsmState.FAILED
        self._runs.drop(correlation_id)
        await self._publish_terminal(
            self._topic_failed,
            correlation_id,
            event_type="omnimarket.autopilot-orchestrator-failed",
            payload={
                "correlation_id": str(correlation_id),
                "phase": phase,
                "error": str(exc),
                "failed_at": self._clock().isoformat(),
            },
        )

    # -------------------------------------------------------------------------
    # Dispatch
    # -------------------------------------------------------------------------

    async def _dispatch(
        self, phase: str, command: ModelAutopilotStartCommand
    ) -> ModelAutopilotPhaseResult | None:
        """Publish the phase's start command.

        Returns a closing result when nothing was dispatched, or ``None`` when
        the run now waits for the downstream node's result event.
        """
        cid = command.correlation_id
        base = {"correlation_id": str(cid), "dry_run": command.dry_run}
        if phase == "B":
            return await self._publish_start(
                "B",
                self._topic_pr_lifecycle,
                "omnimarket.pr-lifecycle-orchestrator-start",
                cid,
                {**base, "run_id": f"autopilot-{cid.hex[:12]}", "merge_only": True},
                unconfigured=EnumAutopilotPhaseStatus.WARN,
                on_error=EnumAutopilotPhaseStatus.FAIL,
            )
        if phase == "C":
            return await self._publish_start(
                "C",
                self._topic_platform_diagnostics,
                "omnimarket.platform-diagnostics-start",
                cid,
                base,
                unconfigured=EnumAutopilotPhaseStatus.WARN,
                on_error=EnumAutopilotPhaseStatus.HALT,
            )
        return await self._publish_start(
            "D",
            self._topic_aislop,
            "omnimarket.aislop-sweep-start",
            cid,
            base,
            unconfigured=EnumAutopilotPhaseStatus.WARN,
            on_error=EnumAutopilotPhaseStatus.WARN,
        )

    async def _publish_start(
        self,
        phase: str,
        topic: str,
        event_type: str,
        correlation_id: UUID,
        payload: dict[str, Any],
        *,
        unconfigured: EnumAutopilotPhaseStatus,
        on_error: EnumAutopilotPhaseStatus,
    ) -> ModelAutopilotPhaseResult | None:
        if not topic:
            logger.warning(
                "[AUTOPILOT-%s] start topic not configured — skipping", phase
            )
            return ModelAutopilotPhaseResult(
                phase_id=phase,
                status=unconfigured,
                detail="start topic not configured in contract",
            )
        try:
            await self._event_bus.publish(
                topic=topic,
                key=str(correlation_id).encode(),
                value=self._envelope_bytes(
                    event_type=event_type,
                    correlation_id=correlation_id,
                    payload=payload,
                ),
            )
        except Exception as exc:
            logger.exception("[AUTOPILOT-%s] dispatch failed: %s", phase, exc)
            return ModelAutopilotPhaseResult(
                phase_id=phase,
                status=on_error,
                detail=f"dispatch error: {exc}",
                halt_reason=str(exc)
                if on_error == EnumAutopilotPhaseStatus.HALT
                else "",
            )
        logger.info("[AUTOPILOT-%s] start published to %s", phase, topic)
        return None

    # -------------------------------------------------------------------------
    # FSM helpers
    # -------------------------------------------------------------------------

    def _record_phase_outcome(
        self,
        state: _PipelineState,
        result: ModelAutopilotPhaseResult,
    ) -> None:
        if result.status in (
            EnumAutopilotPhaseStatus.PASS,
            EnumAutopilotPhaseStatus.PASS_REPAIRED,
            EnumAutopilotPhaseStatus.WARN,
        ):
            state.phases_completed += 1
            state.consecutive_failures = 0
        elif result.status in (
            EnumAutopilotPhaseStatus.FAIL,
            EnumAutopilotPhaseStatus.HALT,
            EnumAutopilotPhaseStatus.TIMEOUT,
        ):
            state.phases_failed += 1
            state.consecutive_failures += 1
        # skipped / not_run do not count toward either counter

    def _circuit_breaker_tripped(self, state: _PipelineState) -> bool:
        return state.consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD

    def _build_result(
        self,
        state: _PipelineState,
        correlation_id: UUID,
    ) -> ModelAutopilotResult:
        overall: EnumAutopilotCycleStatus
        if state.fsm == EnumAutopilotFsmState.COMPLETE:
            overall = EnumAutopilotCycleStatus.COMPLETE
        elif state.fsm == EnumAutopilotFsmState.HALTED:
            overall = EnumAutopilotCycleStatus.HALTED
        elif self._circuit_breaker_tripped(state):
            overall = EnumAutopilotCycleStatus.CIRCUIT_BREAKER
        else:
            overall = EnumAutopilotCycleStatus.FAILED

        def _get(phase_id: str) -> ModelAutopilotPhaseResult:
            return state.phase_results.get(
                phase_id, ModelAutopilotPhaseResult(phase_id=phase_id)
            )

        return ModelAutopilotResult(
            correlation_id=correlation_id,
            overall_status=overall,
            phase_a=_get("A"),
            phase_b=_get("B"),
            phase_c=_get("C"),
            phase_d=_get("D"),
            halt_reason=state.halt_reason,
            phases_completed=state.phases_completed,
            phases_failed=state.phases_failed,
            consecutive_failures=state.consecutive_failures,
        )

    async def _publish_phase_event(
        self,
        from_state: str,
        to_state: str,
        correlation_id: UUID,
    ) -> None:
        if not self._topic_phase_transition:
            return
        payload = self._envelope_bytes(
            event_type="omnimarket.autopilot-phase-transition",
            correlation_id=correlation_id,
            payload={
                "from_phase": from_state.lower(),
                "to_phase": to_state.lower(),
                "correlation_id": str(correlation_id),
            },
        )
        try:
            await self._event_bus.publish(
                topic=self._topic_phase_transition,
                key=None,
                value=payload,
            )
        except Exception as exc:
            logger.warning("[AUTOPILOT] phase event publish failed: %s", exc)

    async def _publish_terminal(
        self,
        topic: str,
        correlation_id: UUID,
        *,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        if not topic:
            logger.error("[AUTOPILOT] terminal topic not configured in contract")
            return
        try:
            await self._event_bus.publish(
                topic=topic,
                key=str(correlation_id).encode(),
                value=self._envelope_bytes(
                    event_type=event_type,
                    correlation_id=correlation_id,
                    payload=payload,
                ),
            )
        except Exception as exc:
            logger.exception(
                "[AUTOPILOT] terminal event publish failed — correlation_id=%s: %s",
                correlation_id,
                exc,
            )

    @staticmethod
    def _envelope_bytes(
        *,
        event_type: str,
        correlation_id: UUID,
        payload: dict[str, Any],
    ) -> bytes:
        """Wrap payload in ModelEventEnvelope and return JSON bytes.

        Consumers on these topics validate inbound messages via
        ``ModelEventEnvelope[object]`` in the runtime auto-wiring callback
        (omnibase_infra.runtime.auto_wiring.handler_wiring._make_event_bus_callback).
        A bare-payload publish fails validation with ``payload: Field required``
        and the handler never runs (OMN-9215).
        """
        envelope = ModelEventEnvelope[dict[str, Any]](
            payload=payload,
            correlation_id=correlation_id,
            event_type=event_type,
            source_tool="node_autopilot_orchestrator",
        )
        return envelope.model_dump_json().encode()


__all__: list[str] = [
    "AutopilotRunStore",
    "EnumAutopilotFsmState",
    "HandlerAutopilotOrchestrator",
    "ModelAutopilotRequest",
]
