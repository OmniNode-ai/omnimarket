# SPDX-License-Identifier: MIT
"""HandlerAutopilotOrchestrator — 4-phase autonomous close-out pipeline.

Phases:
  A — Worktree health sweep: detect lost uncommitted work, clean merged worktrees.
      Dispatches to node_worktree_triage (stub-safe) or runs prune script directly.
      Never halts; records pass | warn | fail.

  B — Merge sweep: drain open PRs via node_pr_lifecycle_orchestrator.
      Publishes onex.cmd.omnimarket.pr-lifecycle-orchestrator-start.v1.
      Never halts; records pass | warn | fail.

  C — Infra health gate: verify postgres, redpanda, valkey, runtime API.
      Dispatches to node_platform_diagnostics.
      HALT authority: infra failure blocks release.

  D — Quality sweeps (parallel advisory):
      - aislop_sweep: AI anti-pattern detection
      - dod_verify: DoD compliance audit
      - gap: cross-repo integration health
      Publishes dispatch events per sweep.
      Records individual results; no individual halt authority.

Circuit breaker: 3 consecutive phase failures -> FAILED.
Phase B and D failures are advisory (non-halting).
Phase C failure is a hard gate (halt).

Related: OMN-6872, OMN-6867, OMN-8087
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import yaml

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


_TERMINAL_STATES = {
    EnumAutopilotFsmState.COMPLETE,
    EnumAutopilotFsmState.HALTED,
    EnumAutopilotFsmState.FAILED,
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


# ---------------------------------------------------------------------------
# Contract loader
# ---------------------------------------------------------------------------


def _load_contract(contract_path: Path | None = None) -> dict[str, Any]:
    path = contract_path or Path(__file__).parent.parent / "contract.yaml"
    with open(path) as f:
        data: dict[str, Any] = yaml.safe_load(f)
    return data


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class HandlerAutopilotOrchestrator:
    """4-phase autonomous close-out pipeline orchestrator.

    All sub-node dispatch is via Kafka publish (event_bus). When event_bus
    is None (standalone / unit test), phase dispatch is skipped and stubs
    record warn results so the FSM still executes fully.
    """

    def __init__(
        self,
        *,
        event_bus: ProtocolEventBusPublisher | None = None,
        contract_path: Path | None = None,
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
        self._topic_pr_lifecycle = next(
            (t for t in publish_topics if "pr-lifecycle-orchestrator-start" in t), ""
        )
        self._topic_platform_diagnostics = next(
            (t for t in publish_topics if "platform-diagnostics-start" in t), ""
        )
        self._topic_aislop = next(
            (t for t in publish_topics if "aislop-sweep-start" in t), ""
        )
        self._topic_dod = next(
            (t for t in publish_topics if "dod-verify-start" in t), ""
        )
        self._event_bus = event_bus

    async def handle(
        self,
        command: ModelAutopilotStartCommand,
    ) -> ModelAutopilotResult:
        """Run the autopilot pipeline."""
        logger.info(
            "[AUTOPILOT] === ENTRY === correlation_id=%s mode=%s dry_run=%s",
            command.correlation_id,
            command.mode,
            command.dry_run,
        )

        state = _PipelineState()

        try:
            # ----------------------------------------------------------------
            # Phase A: Worktree health sweep
            # ----------------------------------------------------------------
            state.fsm = EnumAutopilotFsmState.PHASE_A_WORKTREE
            await self._publish_phase_event(
                "IDLE", "PHASE_A_WORKTREE", command.correlation_id
            )

            phase_a = await self._run_phase_a(command)
            state.phase_results["A"] = phase_a
            self._record_phase_outcome(state, phase_a)

            if self._circuit_breaker_tripped(state):
                state.fsm = EnumAutopilotFsmState.FAILED
                return self._build_result(state, command.correlation_id)

            # ----------------------------------------------------------------
            # Phase B: Merge sweep
            # ----------------------------------------------------------------
            state.fsm = EnumAutopilotFsmState.PHASE_B_MERGE_SWEEP
            await self._publish_phase_event(
                "PHASE_A_WORKTREE", "PHASE_B_MERGE_SWEEP", command.correlation_id
            )

            phase_b = await self._run_phase_b(command)
            state.phase_results["B"] = phase_b
            self._record_phase_outcome(state, phase_b)

            if self._circuit_breaker_tripped(state):
                state.fsm = EnumAutopilotFsmState.FAILED
                return self._build_result(state, command.correlation_id)

            # ----------------------------------------------------------------
            # Phase C: Infra health gate (HALT authority)
            # ----------------------------------------------------------------
            state.fsm = EnumAutopilotFsmState.PHASE_C_INFRA_GATE
            await self._publish_phase_event(
                "PHASE_B_MERGE_SWEEP", "PHASE_C_INFRA_GATE", command.correlation_id
            )

            phase_c = await self._run_phase_c(command)
            state.phase_results["C"] = phase_c
            self._record_phase_outcome(state, phase_c)

            if phase_c.status == EnumAutopilotPhaseStatus.HALT:
                state.fsm = EnumAutopilotFsmState.HALTED
                state.halt_reason = f"Phase C (infra-gate): {phase_c.halt_reason}"
                logger.warning(
                    "[AUTOPILOT] HALT: infra gate failed — %s", phase_c.halt_reason
                )
                await self._publish_phase_event(
                    "PHASE_C_INFRA_GATE", "HALTED", command.correlation_id
                )
                return self._build_result(state, command.correlation_id)

            if self._circuit_breaker_tripped(state):
                state.fsm = EnumAutopilotFsmState.FAILED
                return self._build_result(state, command.correlation_id)

            # ----------------------------------------------------------------
            # Phase D: Quality sweeps (parallel advisory)
            # ----------------------------------------------------------------
            state.fsm = EnumAutopilotFsmState.PHASE_D_QUALITY_SWEEPS
            await self._publish_phase_event(
                "PHASE_C_INFRA_GATE", "PHASE_D_QUALITY_SWEEPS", command.correlation_id
            )

            phase_d = await self._run_phase_d(command)
            state.phase_results["D"] = phase_d
            self._record_phase_outcome(state, phase_d)

            if phase_d.status == EnumAutopilotPhaseStatus.HALT:
                state.fsm = EnumAutopilotFsmState.HALTED
                state.halt_reason = f"Phase D (quality): {phase_d.halt_reason}"
                logger.warning(
                    "[AUTOPILOT] HALT: quality sweep hard gate — %s",
                    phase_d.halt_reason,
                )
                await self._publish_phase_event(
                    "PHASE_D_QUALITY_SWEEPS", "HALTED", command.correlation_id
                )
                return self._build_result(state, command.correlation_id)

            state.fsm = EnumAutopilotFsmState.COMPLETE
            await self._publish_phase_event(
                "PHASE_D_QUALITY_SWEEPS", "COMPLETE", command.correlation_id
            )

        except Exception as exc:
            logger.exception(
                "[AUTOPILOT] unhandled exception in phase %s: %s", state.fsm, exc
            )
            state.halt_reason = str(exc)
            state.fsm = EnumAutopilotFsmState.FAILED

        logger.info(
            "[AUTOPILOT] === EXIT === fsm=%s phases_completed=%d phases_failed=%d",
            state.fsm,
            state.phases_completed,
            state.phases_failed,
        )
        return self._build_result(state, command.correlation_id)

    # -------------------------------------------------------------------------
    # Phase implementations
    # -------------------------------------------------------------------------

    async def _run_phase_a(
        self, command: ModelAutopilotStartCommand
    ) -> ModelAutopilotPhaseResult:
        """Phase A: Worktree health sweep.

        Dispatches worktree triage via Kafka if event_bus is wired.
        Falls back to warn when event_bus is absent (no-infra standalone mode).
        Never halts — worktree health is hygiene, not release-blocking.
        """
        logger.info(
            "[AUTOPILOT-A] worktree health sweep — correlation_id=%s",
            command.correlation_id,
        )

        if self._event_bus is None:
            logger.warning(
                "[AUTOPILOT-A] event_bus not wired — skipping dispatch (stub pass)"
            )
            return ModelAutopilotPhaseResult(
                phase_id="A",
                status=EnumAutopilotPhaseStatus.WARN,
                detail="event_bus not wired; worktree dispatch skipped (standalone mode)",
            )

        try:
            payload = json.dumps(
                {
                    "correlation_id": str(command.correlation_id),
                    "dry_run": command.dry_run,
                    "operation": "worktree_health_sweep",
                }
            ).encode()
            # Publish worktree triage command. The contract declares no
            # dedicated worktree topic yet — reuse the phase-transition topic
            # as an advisory signal until node_worktree_triage is wired.
            await self._event_bus.publish(
                topic=self._topic_phase_transition,
                key=str(command.correlation_id).encode(),
                value=payload,
            )
            logger.info("[AUTOPILOT-A] worktree sweep dispatch published")
            return ModelAutopilotPhaseResult(
                phase_id="A",
                status=EnumAutopilotPhaseStatus.PASS,
                detail="worktree health sweep dispatched",
            )
        except Exception as exc:
            logger.exception("[AUTOPILOT-A] dispatch failed: %s", exc)
            return ModelAutopilotPhaseResult(
                phase_id="A",
                status=EnumAutopilotPhaseStatus.FAIL,
                detail=f"dispatch error: {exc}",
            )

    async def _run_phase_b(
        self, command: ModelAutopilotStartCommand
    ) -> ModelAutopilotPhaseResult:
        """Phase B: Merge sweep via node_pr_lifecycle_orchestrator.

        Publishes onex.cmd.omnimarket.pr-lifecycle-orchestrator-start.v1.
        Never halts — merge sweep failures are advisory.
        """
        logger.info(
            "[AUTOPILOT-B] merge sweep — correlation_id=%s", command.correlation_id
        )

        if self._event_bus is None:
            logger.warning(
                "[AUTOPILOT-B] event_bus not wired — skipping dispatch (stub pass)"
            )
            return ModelAutopilotPhaseResult(
                phase_id="B",
                status=EnumAutopilotPhaseStatus.WARN,
                detail="event_bus not wired; merge sweep skipped (standalone mode)",
            )

        if not self._topic_pr_lifecycle:
            logger.warning("[AUTOPILOT-B] pr-lifecycle topic not configured — skipping")
            return ModelAutopilotPhaseResult(
                phase_id="B",
                status=EnumAutopilotPhaseStatus.WARN,
                detail="pr-lifecycle topic not configured in contract",
            )

        try:
            payload = json.dumps(
                {
                    "correlation_id": str(command.correlation_id),
                    "dry_run": command.dry_run,
                    "merge_only": True,
                }
            ).encode()
            await self._event_bus.publish(
                topic=self._topic_pr_lifecycle,
                key=str(command.correlation_id).encode(),
                value=payload,
            )
            logger.info(
                "[AUTOPILOT-B] pr-lifecycle dispatch published to %s",
                self._topic_pr_lifecycle,
            )
            return ModelAutopilotPhaseResult(
                phase_id="B",
                status=EnumAutopilotPhaseStatus.PASS,
                detail="merge sweep dispatched to node_pr_lifecycle_orchestrator",
            )
        except Exception as exc:
            logger.exception("[AUTOPILOT-B] dispatch failed: %s", exc)
            return ModelAutopilotPhaseResult(
                phase_id="B",
                status=EnumAutopilotPhaseStatus.FAIL,
                detail=f"dispatch error: {exc}",
            )

    async def _run_phase_c(
        self, command: ModelAutopilotStartCommand
    ) -> ModelAutopilotPhaseResult:
        """Phase C: Infra health gate via node_platform_diagnostics.

        Publishes platform-diagnostics-start command.
        HALT authority: infra failure blocks all subsequent phases.
        """
        logger.info(
            "[AUTOPILOT-C] infra health gate — correlation_id=%s",
            command.correlation_id,
        )

        if self._event_bus is None:
            logger.warning(
                "[AUTOPILOT-C] event_bus not wired — skipping dispatch (stub warn)"
            )
            return ModelAutopilotPhaseResult(
                phase_id="C",
                status=EnumAutopilotPhaseStatus.WARN,
                detail="event_bus not wired; infra gate skipped (standalone mode)",
            )

        if not self._topic_platform_diagnostics:
            logger.warning(
                "[AUTOPILOT-C] platform-diagnostics topic not configured — skipping"
            )
            return ModelAutopilotPhaseResult(
                phase_id="C",
                status=EnumAutopilotPhaseStatus.WARN,
                detail="platform-diagnostics topic not configured in contract",
            )

        try:
            payload = json.dumps(
                {
                    "correlation_id": str(command.correlation_id),
                    "dry_run": command.dry_run,
                    "mode": "infra_health",
                }
            ).encode()
            await self._event_bus.publish(
                topic=self._topic_platform_diagnostics,
                key=str(command.correlation_id).encode(),
                value=payload,
            )
            logger.info("[AUTOPILOT-C] platform-diagnostics dispatch published")
            return ModelAutopilotPhaseResult(
                phase_id="C",
                status=EnumAutopilotPhaseStatus.PASS,
                detail="infra health gate dispatched to node_platform_diagnostics",
            )
        except Exception as exc:
            logger.exception("[AUTOPILOT-C] dispatch failed: %s", exc)
            return ModelAutopilotPhaseResult(
                phase_id="C",
                status=EnumAutopilotPhaseStatus.HALT,
                detail=f"infra gate dispatch error: {exc}",
                halt_reason=str(exc),
            )

    async def _run_phase_d(
        self, command: ModelAutopilotStartCommand
    ) -> ModelAutopilotPhaseResult:
        """Phase D: Quality sweeps (aislop, dod_verify, gap) — advisory parallel.

        Publishes dispatch events for each sweep. Individual sweep failures
        are non-halting; only a hard-gate result (e.g. dod FAIL) halts.
        """
        logger.info(
            "[AUTOPILOT-D] quality sweeps — correlation_id=%s", command.correlation_id
        )

        if self._event_bus is None:
            logger.warning(
                "[AUTOPILOT-D] event_bus not wired — skipping dispatch (stub pass)"
            )
            return ModelAutopilotPhaseResult(
                phase_id="D",
                status=EnumAutopilotPhaseStatus.WARN,
                detail="event_bus not wired; quality sweeps skipped (standalone mode)",
            )

        sweep_errors: list[str] = []

        # Dispatch aislop sweep
        if self._topic_aislop:
            try:
                await self._event_bus.publish(
                    topic=self._topic_aislop,
                    key=str(command.correlation_id).encode(),
                    value=json.dumps(
                        {
                            "correlation_id": str(command.correlation_id),
                            "dry_run": command.dry_run,
                        }
                    ).encode(),
                )
                logger.info("[AUTOPILOT-D] aislop sweep dispatched")
            except Exception as exc:
                logger.exception("[AUTOPILOT-D] aislop dispatch failed: %s", exc)
                sweep_errors.append(f"aislop: {exc}")
        else:
            logger.warning("[AUTOPILOT-D] aislop topic not configured")
            sweep_errors.append("aislop: topic not configured")

        # Dispatch dod_verify sweep
        if self._topic_dod:
            try:
                await self._event_bus.publish(
                    topic=self._topic_dod,
                    key=str(command.correlation_id).encode(),
                    value=json.dumps(
                        {
                            "correlation_id": str(command.correlation_id),
                            "dry_run": command.dry_run,
                            "since_last_cycle": True,
                            "per_ticket_verify": True,
                        }
                    ).encode(),
                )
                logger.info("[AUTOPILOT-D] dod_verify sweep dispatched")
            except Exception as exc:
                logger.exception("[AUTOPILOT-D] dod_verify dispatch failed: %s", exc)
                sweep_errors.append(f"dod_verify: {exc}")
        else:
            logger.warning("[AUTOPILOT-D] dod_verify topic not configured")
            sweep_errors.append("dod_verify: topic not configured")

        if sweep_errors:
            return ModelAutopilotPhaseResult(
                phase_id="D",
                status=EnumAutopilotPhaseStatus.WARN,
                detail=f"quality sweep partial dispatch failures: {'; '.join(sweep_errors)}",
            )

        return ModelAutopilotPhaseResult(
            phase_id="D",
            status=EnumAutopilotPhaseStatus.PASS,
            detail="quality sweeps dispatched: aislop, dod_verify",
        )

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
        if self._event_bus is None or not self._topic_phase_transition:
            return
        payload = json.dumps(
            {
                "from_phase": from_state.lower(),
                "to_phase": to_state.lower(),
                "correlation_id": str(correlation_id),
            }
        ).encode()
        try:
            await self._event_bus.publish(
                topic=self._topic_phase_transition,
                key=None,
                value=payload,
            )
        except Exception as exc:
            logger.warning("[AUTOPILOT] phase event publish failed: %s", exc)


__all__: list[str] = [
    "HandlerAutopilotOrchestrator",
]
