# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerOvernight — Overnight session FSM orchestrator.

Sequences the autonomous overnight pipeline in phase order:
  nightly_loop_controller -> build_loop_orchestrator -> merge_sweep ->
  ci_watch -> platform_readiness

nightly_loop_controller runs first to read standing orders and dispatch
mechanical tickets. The build loop then processes what it dispatched.

Each phase is represented as a named step with its own success/failure state.
In dry_run mode, all phases are simulated as successful. The handler itself
is pure — it owns no external I/O, only phase sequencing and state tracking.

Integration with the actual node handlers happens at the RuntimeLocal layer
via the event bus. This handler is responsible for the sequencing contract.

Related:
    - OMN-8025: Overseer seam integration epic — nightly loop trigger wiring
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


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
    started_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    completed_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class HandlerOvernight:
    """Overnight session orchestrator.

    Pure FSM — sequences phases, accumulates results, derives terminal status.
    No external I/O. In dry_run mode, all phases succeed synthetically.

    The caller (RuntimeLocal or test harness) is responsible for wiring
    the actual node handler calls for each phase. This handler models the
    sequencing contract only.
    """

    def handle(
        self,
        command: ModelOvernightCommand,
        phase_results: dict[EnumPhase, bool] | None = None,
    ) -> ModelOvernightResult:
        """Run the overnight pipeline.

        Args:
            command: Start command with skip flags and dry_run.
            phase_results: Optional per-phase success overrides for testing.
                           If None, all non-skipped phases succeed (dry_run path).

        Returns:
            ModelOvernightResult with per-phase outcomes and terminal status.
        """
        started_at = datetime.now(tz=UTC)
        results: list[ModelPhaseResult] = []
        overrides = phase_results or {}

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

            if command.dry_run:
                success = True
                error_msg: str | None = None
            else:
                success = overrides.get(phase, True)
                error_msg = None if success else f"Phase {phase.value} failed"

            results.append(
                ModelPhaseResult(
                    phase=phase,
                    success=success,
                    skipped=False,
                    error_message=error_msg,
                )
            )

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

        if not phases_failed:
            status = EnumOvernightStatus.COMPLETED
        elif len(phases_failed) < len(phases_run):
            status = EnumOvernightStatus.PARTIAL
        else:
            status = EnumOvernightStatus.FAILED

        return ModelOvernightResult(
            correlation_id=command.correlation_id,
            session_status=status,
            phases_run=phases_run,
            phases_failed=phases_failed,
            phases_skipped=phases_skipped,
            phase_results=results,
            dry_run=command.dry_run,
            started_at=started_at,
            completed_at=datetime.now(tz=UTC),
        )

    def _should_skip(self, phase: EnumPhase, command: ModelOvernightCommand) -> bool:
        """Return True if this phase should be skipped per command flags."""
        if phase == EnumPhase.NIGHTLY_LOOP and command.skip_nightly_loop:
            return True
        if phase == EnumPhase.BUILD_LOOP and command.skip_build_loop:
            return True
        if phase == EnumPhase.MERGE_SWEEP and command.skip_merge_sweep:
            return True
        return False


__all__: list[str] = [
    "EnumOvernightStatus",
    "EnumPhase",
    "HandlerOvernight",
    "ModelOvernightCommand",
    "ModelOvernightResult",
    "ModelPhaseResult",
]
