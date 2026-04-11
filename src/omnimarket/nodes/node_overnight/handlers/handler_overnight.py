# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerOvernight — Overnight session FSM orchestrator.

Sequences the autonomous overnight pipeline in phase order:
  nightly_loop_controller -> build_loop_orchestrator -> merge_sweep ->
  ci_watch -> platform_readiness

nightly_loop_controller runs first to read standing orders and dispatch
mechanical tickets. The build loop then processes what it dispatched.

Each phase is represented as a named step with its own success/failure state.
In dry_run mode, all phases are simulated as successful. When dispatch_phases
is True, the handler invokes the real compute-node handler for each phase
(MVP wiring — OMN-8371). Without dispatch_phases, the handler stays a pure
FSM and the caller supplies phase_results directly.

Related:
    - OMN-8025: Overseer seam integration epic — nightly loop trigger wiring
    - OMN-8371: Minimum viable executor wiring — dispatch phases to compute
      node handlers instead of running as a pure FSM
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from time import monotonic

from omnibase_compat.overseer.model_overnight_contract import ModelOvernightContract
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_overnight.topics import (
    TOPIC_OVERNIGHT_COMPLETE,
    TOPIC_OVERNIGHT_PHASE_END,
    TOPIC_OVERNIGHT_PHASE_START,
)

logger = logging.getLogger(__name__)

# Type alias for a phase dispatcher: takes the command + contract (if present),
# returns (success, error_message). Dispatchers own their own handler imports
# to avoid hard coupling at module import time.
PhaseDispatcher = Callable[
    ["ModelOvernightCommand", ModelOvernightContract | None],
    tuple[bool, str | None],
]

# OMN-8405: Sync event-publisher seam. HandlerOvernight is synchronous; the
# asyncio-based ProtocolEventBusPublisher cannot be awaited from here without
# changing every caller signature. Instead, callers inject a thin callable
# that adapts their bus to this shape (same pattern as TickEmitter in
# overseer_tick.py from OMN-8375). Topic strings come from topics.py.
EventPublisher = Callable[[str, bytes], None]


class EnumPhase(StrEnum):
    """Overnight pipeline phases in execution order."""

    NIGHTLY_LOOP = "nightly_loop_controller"
    BUILD_LOOP = "build_loop_orchestrator"
    MERGE_SWEEP = "merge_sweep"
    CI_WATCH = "ci_watch"
    PLATFORM_READINESS = "platform_readiness"


# Canonical phase sequence — nightly_loop_controller runs first so it can
# dispatch tickets before the build loop processes them.
_PHASE_SEQUENCE: list[EnumPhase] = [
    EnumPhase.NIGHTLY_LOOP,
    EnumPhase.BUILD_LOOP,
    EnumPhase.MERGE_SWEEP,
    EnumPhase.CI_WATCH,
    EnumPhase.PLATFORM_READINESS,
]


class EnumOvernightStatus(StrEnum):
    """Terminal session status."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class ModelOvernightCommand(BaseModel):
    """Input command for overnight handler."""

    model_config = ConfigDict(extra="forbid")

    correlation_id: str
    max_cycles: int = 0
    skip_nightly_loop: bool = False
    skip_build_loop: bool = False
    skip_merge_sweep: bool = False
    dry_run: bool = False
    overnight_contract: ModelOvernightContract | None = None


class ModelPhaseResult(BaseModel):
    """Result for a single overnight phase."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    phase: EnumPhase
    success: bool
    skipped: bool = False
    error_message: str | None = None
    duration_seconds: float = 0.0


class ModelOvernightResult(BaseModel):
    """Result emitted by HandlerOvernight."""

    model_config = ConfigDict(extra="forbid")

    correlation_id: str
    session_status: EnumOvernightStatus
    phases_run: list[str] = Field(default_factory=list)
    phases_failed: list[str] = Field(default_factory=list)
    phases_skipped: list[str] = Field(default_factory=list)
    phase_results: list[ModelPhaseResult] = Field(default_factory=list)
    dry_run: bool = False
    halt_reason: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    completed_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class HandlerOvernight:
    """Overnight session orchestrator.

    Sequences phases, accumulates results, derives terminal status. Can run
    in two modes:

    - Pure FSM (default): no external I/O. Caller supplies phase_results to
      drive per-phase success. Used by existing unit tests.
    - Executor (OMN-8371): set dispatch_phases=True on handle() to invoke the
      real compute-node handler for each phase. Dispatchers are resolved from
      self._dispatchers, which defaults to ``_DEFAULT_PHASE_DISPATCHERS``.
      Each dispatcher returns (success, error_message).

    When an overnight_contract is provided via the command, the handler
    enforces cost ceiling and halt-on-failure checks after each phase.
    Accumulated cost is supplied by the caller via phase_costs; if not
    provided, cost checks are skipped (backwards-compatible).
    """

    def __init__(
        self,
        dispatchers: dict[EnumPhase, PhaseDispatcher] | None = None,
        event_bus: EventPublisher | None = None,
    ) -> None:
        """Create an overnight handler.

        Args:
            dispatchers: Optional phase -> dispatcher map. Defaults to
                ``_DEFAULT_PHASE_DISPATCHERS`` when None. Tests inject
                mocks here to assert per-phase invocation.
            event_bus: Optional sync callable ``(topic, payload_bytes) -> None``
                used to publish phase-start / phase-end / complete envelopes
                (OMN-8405). When None, publishing is a no-op so legacy callers
                and unit tests remain unaffected. Topics come from
                ``node_overnight/topics.py`` (mirrored in contract.yaml).
        """
        self._dispatchers: dict[EnumPhase, PhaseDispatcher] = (
            dispatchers if dispatchers is not None else dict(_DEFAULT_PHASE_DISPATCHERS)
        )
        self._event_bus: EventPublisher | None = event_bus

    def _publish(self, topic: str, payload: dict[str, object]) -> None:
        """Publish an envelope via the injected event bus, swallowing errors.

        The overnight session must never be taken down by a bus outage. A
        publish failure is logged and execution continues.
        """
        if self._event_bus is None:
            return
        try:
            self._event_bus(topic, json.dumps(payload).encode())
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("[OVERNIGHT] event_bus publish to %s failed: %s", topic, exc)

    def handle(
        self,
        command: ModelOvernightCommand,
        phase_results: dict[EnumPhase, bool] | None = None,
        phase_costs: dict[EnumPhase, float] | None = None,
        dispatch_phases: bool = False,
    ) -> ModelOvernightResult:
        """Run the overnight pipeline.

        Args:
            command: Start command with skip flags, dry_run, and optional contract.
            phase_results: Optional per-phase success overrides for testing.
                           If None, all non-skipped phases succeed (dry_run path).
            phase_costs: Optional per-phase cost in USD. Used for cost ceiling
                         enforcement when overnight_contract is set.

        Returns:
            ModelOvernightResult with per-phase outcomes, terminal status, and
            optional halt_reason if a contract halt condition was triggered.
        """
        started_at = datetime.now(tz=UTC)
        results: list[ModelPhaseResult] = []
        overrides = phase_results or {}
        costs = phase_costs or {}
        contract = command.overnight_contract
        accumulated_cost: float = 0.0
        halt_reason: str | None = None

        for phase in _PHASE_SEQUENCE:
            skipped = self._should_skip(phase, command)

            if skipped:
                results.append(
                    ModelPhaseResult(
                        phase=phase,
                        success=True,
                        skipped=True,
                    )
                )
                continue

            # OMN-8405: phase-start envelope before dispatch so downstream
            # consumers (overseer tick loop, delegation pipeline) can observe
            # an overnight run advancing.
            self._publish(
                TOPIC_OVERNIGHT_PHASE_START,
                {
                    "correlation_id": command.correlation_id,
                    "phase": phase.value,
                    "dry_run": command.dry_run,
                    "timestamp": datetime.now(tz=UTC).isoformat(),
                },
            )
            phase_started_at = monotonic()

            if dispatch_phases and phase not in overrides:
                success, error_msg = self._dispatch_phase(phase, command, contract)
            elif command.dry_run:
                success = True
                error_msg = None
            else:
                success = overrides.get(phase, True)
                error_msg = None if success else f"Phase {phase.value} failed"

            accumulated_cost += costs.get(phase, 0.0)
            duration_ms = int((monotonic() - phase_started_at) * 1000)

            results.append(
                ModelPhaseResult(
                    phase=phase,
                    success=success,
                    skipped=False,
                    error_message=error_msg,
                )
            )

            # OMN-8405: phase-end envelope after the phase settles (before
            # halt-condition evaluation so we always emit a terminal signal
            # even when a halt breaks the loop on the next line).
            self._publish(
                TOPIC_OVERNIGHT_PHASE_END,
                {
                    "correlation_id": command.correlation_id,
                    "phase": phase.value,
                    "phase_status": "success" if success else "failed",
                    "error_message": error_msg,
                    "duration_ms": duration_ms,
                    "accumulated_cost_usd": accumulated_cost,
                    "timestamp": datetime.now(tz=UTC).isoformat(),
                },
            )

            if contract is not None:
                halt = self._check_halt_conditions(
                    contract=contract,
                    phase=phase,
                    phase_success=success,
                    accumulated_cost=accumulated_cost,
                )
                if halt is not None:
                    halt_reason = halt
                    logger.error("Overnight halt triggered: %s", halt_reason)
                    break

            if not success:
                # On failure, stop the pipeline unless it's a non-critical phase.
                # nightly_loop or build_loop failure stops everything; other phases continue.
                if phase in (EnumPhase.NIGHTLY_LOOP, EnumPhase.BUILD_LOOP):
                    logger.warning(
                        "%s failed — halting overnight pipeline", phase.value
                    )
                    break
                logger.warning(
                    "Phase %s failed — continuing to next phase", phase.value
                )

        phases_run = [r.phase.value for r in results if not r.skipped]
        phases_failed = [
            r.phase.value for r in results if not r.success and not r.skipped
        ]
        phases_skipped = [r.phase.value for r in results if r.skipped]

        if halt_reason is not None:
            status = EnumOvernightStatus.FAILED
        elif not phases_failed:
            status = EnumOvernightStatus.COMPLETED
        elif len(phases_failed) < len(phases_run):
            status = EnumOvernightStatus.PARTIAL
        else:
            status = EnumOvernightStatus.FAILED

        completed_at = datetime.now(tz=UTC)

        # OMN-8405: session-complete envelope published exactly once when the
        # pipeline exits (success, halt, or phase failure — all paths reach
        # here). Downstream consumers key off this for end-of-run analytics.
        self._publish(
            TOPIC_OVERNIGHT_COMPLETE,
            {
                "correlation_id": command.correlation_id,
                "session_status": status.value,
                "phases_run": phases_run,
                "phases_failed": phases_failed,
                "phases_skipped": phases_skipped,
                "dry_run": command.dry_run,
                "halt_reason": halt_reason,
                "accumulated_cost_usd": accumulated_cost,
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
            },
        )

        return ModelOvernightResult(
            correlation_id=command.correlation_id,
            session_status=status,
            phases_run=phases_run,
            phases_failed=phases_failed,
            phases_skipped=phases_skipped,
            phase_results=results,
            dry_run=command.dry_run,
            halt_reason=halt_reason,
            started_at=started_at,
            completed_at=completed_at,
        )

    def _check_halt_conditions(
        self,
        *,
        contract: ModelOvernightContract,
        phase: EnumPhase,
        phase_success: bool,
        accumulated_cost: float,
    ) -> str | None:
        """Check all contract halt conditions after a phase completes.

        Returns a halt_reason string if any condition is triggered, else None.
        """
        for halt_cond in contract.halt_conditions:
            if (
                halt_cond.check_type == "cost_ceiling"
                and accumulated_cost >= halt_cond.threshold
            ):
                return (
                    f"cost_ceiling: {accumulated_cost:.2f} >= "
                    f"{halt_cond.threshold:.2f} USD"
                )

        # Check halt_on_failure for the completed phase against contract phase specs
        if not phase_success:
            for phase_spec in contract.phases:
                if phase_spec.phase_name == phase.value and phase_spec.halt_on_failure:
                    return f"halt_on_failure: phase {phase.value} failed"

        return None

    def _dispatch_phase(
        self,
        phase: EnumPhase,
        command: ModelOvernightCommand,
        contract: ModelOvernightContract | None,
    ) -> tuple[bool, str | None]:
        """Invoke the compute-node handler for ``phase``.

        Returns (success, error_message). Unknown phases and dispatcher
        exceptions are logged and reported as failures so halt_on_failure
        semantics still apply.
        """
        dispatcher = self._dispatchers.get(phase)
        if dispatcher is None:
            msg = f"No dispatcher registered for phase {phase.value}"
            logger.warning("[OVERNIGHT] %s", msg)
            return False, msg

        logger.info("[OVERNIGHT] Dispatching phase %s", phase.value)
        try:
            return dispatcher(command, contract)
        except Exception as exc:
            msg = f"Phase {phase.value} dispatcher raised: {exc}"
            logger.exception("[OVERNIGHT] %s", msg)
            return False, msg

    def _should_skip(self, phase: EnumPhase, command: ModelOvernightCommand) -> bool:
        """Return True if this phase should be skipped per command flags."""
        if phase == EnumPhase.NIGHTLY_LOOP and command.skip_nightly_loop:
            return True
        if phase == EnumPhase.BUILD_LOOP and command.skip_build_loop:
            return True
        if phase == EnumPhase.MERGE_SWEEP and command.skip_merge_sweep:
            return True
        return False


def _dispatch_nightly_loop(
    command: ModelOvernightCommand,
    contract: ModelOvernightContract | None,
) -> tuple[bool, str | None]:
    """Dispatch nightly_loop_controller.

    The full ``HandlerNightlyLoopController.run()`` path requires a
    ``DatabaseAdapter`` not available at the overseer layer yet. The
    handler's ``handle()`` entry point returns a metadata record and is
    safe to call from the dispatch seam without DI plumbing.
    """
    from omnimarket.nodes.node_nightly_loop_controller.handlers.handler_nightly_loop_controller import (
        HandlerNightlyLoopController,
    )

    handler = HandlerNightlyLoopController()
    result = handler.handle()
    logger.info("[OVERNIGHT] nightly_loop metadata: %s", result)
    return True, None


def _dispatch_build_loop(
    command: ModelOvernightCommand,
    contract: ModelOvernightContract | None,
) -> tuple[bool, str | None]:
    """Dispatch the async build-loop orchestrator synchronously."""
    import asyncio
    from datetime import datetime as _dt
    from uuid import uuid4

    from omnimarket.nodes.node_build_loop.models.model_loop_start_command import (
        ModelLoopStartCommand,
    )
    from omnimarket.nodes.node_build_loop_orchestrator.handlers.handler_build_loop_orchestrator import (
        HandlerBuildLoopOrchestrator,
    )

    handler = HandlerBuildLoopOrchestrator()
    build_cmd = ModelLoopStartCommand(
        correlation_id=uuid4(),
        max_cycles=max(command.max_cycles, 1),
        mode="build",
        dry_run=command.dry_run,
        requested_at=_dt.now(tz=UTC),
    )
    result = asyncio.run(handler.handle(build_cmd))
    success = result.cycles_failed == 0
    error = None if success else f"build_loop: {result.cycles_failed} cycles failed"
    return success, error


def _dispatch_merge_sweep(
    command: ModelOvernightCommand,
    contract: ModelOvernightContract | None,
) -> tuple[bool, str | None]:
    """Dispatch merge sweep.

    Requires a PR inventory (``list[ModelPRInfo]``) that must be collected
    from GitHub before the sweep can classify anything. MVP passes an empty
    list to prove the wiring; a follow-up ticket must add a PR-inventory
    adapter before this produces real output.
    """
    from omnimarket.nodes.node_merge_sweep.handlers.handler_merge_sweep import (
        ModelMergeSweepRequest,
        NodeMergeSweep,
    )

    handler = NodeMergeSweep()
    request = ModelMergeSweepRequest(prs=[])
    result = handler.handle(request)
    logger.info("[OVERNIGHT] merge_sweep status: %s", result.status)
    return True, None


def _dispatch_ci_watch(
    command: ModelOvernightCommand,
    contract: ModelOvernightContract | None,
) -> tuple[bool, str | None]:
    """Dispatch CI watch.

    ``HandlerCiWatch`` requires a concrete PR + repo to poll; those are not
    available at the overnight session level. MVP returns success in dry_run
    and logs a warning otherwise. A follow-up must wire this from the PRs
    touched by the build loop phase above.
    """
    if not command.dry_run:
        logger.warning("[OVERNIGHT] ci_watch dispatched without PR context — skipping")
    return True, None


def _dispatch_platform_readiness(
    command: ModelOvernightCommand,
    contract: ModelOvernightContract | None,
) -> tuple[bool, str | None]:
    """Dispatch platform readiness.

    ``NodePlatformReadiness.handle`` auto-collects all 7 system dimensions
    when ``dimensions=[]``. This is the cleanest MVP target — it runs real
    work with no additional wiring required.
    """
    from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
        EnumReadinessStatus,
        ModelPlatformReadinessRequest,
        NodePlatformReadiness,
    )

    if command.dry_run:
        # Auto-collection runs subprocess calls (ssh, claude plugin list);
        # skip them in dry_run so smoke tests stay hermetic.
        return True, None

    handler = NodePlatformReadiness()
    result = handler.handle(ModelPlatformReadinessRequest())
    success = result.overall != EnumReadinessStatus.FAIL
    error = None if success else f"platform_readiness blockers: {result.blockers}"
    return success, error


_DEFAULT_PHASE_DISPATCHERS: dict[EnumPhase, PhaseDispatcher] = {
    EnumPhase.NIGHTLY_LOOP: _dispatch_nightly_loop,
    EnumPhase.BUILD_LOOP: _dispatch_build_loop,
    EnumPhase.MERGE_SWEEP: _dispatch_merge_sweep,
    EnumPhase.CI_WATCH: _dispatch_ci_watch,
    EnumPhase.PLATFORM_READINESS: _dispatch_platform_readiness,
}


__all__: list[str] = [
    "EnumOvernightStatus",
    "EnumPhase",
    "EventPublisher",
    "HandlerOvernight",
    "ModelOvernightCommand",
    "ModelOvernightContract",
    "ModelOvernightResult",
    "ModelPhaseResult",
    "PhaseDispatcher",
]
