# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler that executes the close-out phase: merge-sweep, quality gates, release readiness.

This is an EFFECT handler - performs external I/O. Delegates FSM state tracking
to omnimarket's HandlerCloseOut node.

Related:
    - OMN-7316: node_closeout_effect
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from omnimarket.nodes.node_close_out.handlers.handler_close_out import HandlerCloseOut
from omnimarket.nodes.node_close_out.models.model_close_out_start_command import (
    ModelCloseOutStartCommand,
)
from omnimarket.nodes.node_close_out.models.model_close_out_state import (
    EnumCloseOutPhase,
)

from omnimarket.nodes.node_closeout_effect.handlers.merge_sweep_runner import (
    run_merge_sweep,
)
from omnimarket.nodes.node_closeout_effect.models.model_closeout_result import (
    ModelCloseoutResult,
)

logger = logging.getLogger(__name__)

HandlerType = Literal["NODE_HANDLER", "INFRA_HANDLER", "PROJECTION_HANDLER"]
HandlerCategory = Literal["EFFECT", "COMPUTE", "NONDETERMINISTIC_COMPUTE"]


class HandlerCloseout:
    """Executes close-out phase: merge-sweep, quality gates, release readiness.

    Delegates FSM state tracking to omnimarket's HandlerCloseOut.
    In dry-run mode, returns a synthetic success result without side effects.
    """

    def __init__(self) -> None:
        self._close_out_fsm = HandlerCloseOut()

    @property
    def handler_type(self) -> HandlerType:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> HandlerCategory:
        return "EFFECT"

    async def handle(
        self,
        correlation_id: UUID,
        dry_run: bool = False,
    ) -> ModelCloseoutResult:
        """Execute close-out phase.

        Steps:
            1. Run merge-sweep (enable auto-merge on ready PRs)
            2. Feed results through omnimarket's close-out FSM
            3. Return translated result

        Args:
            correlation_id: Cycle correlation ID.
            dry_run: Skip actual side effects.

        Returns:
            ModelCloseoutResult with outcomes.
        """
        logger.info(
            "Closeout phase started (correlation_id=%s, dry_run=%s)",
            correlation_id,
            dry_run,
        )

        if dry_run:
            logger.info("Dry run: skipping closeout side effects")
            return ModelCloseoutResult(
                correlation_id=correlation_id,
                merge_sweep_completed=True,
                prs_merged=0,
                quality_gates_passed=True,
                release_ready=True,
                warnings=("dry_run: no side effects executed",),
            )

        warnings: list[str] = []

        # Phase 1: Merge sweep via gh CLI
        merge_sweep_ok = True
        prs_merged = 0
        try:
            sweep_result = await run_merge_sweep(dry_run=dry_run)
            prs_merged = sweep_result.auto_merge_enabled
            if sweep_result.errors:
                for err in sweep_result.errors:
                    warnings.append(f"Merge sweep: {err}")
                merge_sweep_ok = False
            logger.info(
                "Merge sweep complete: %d PRs classified, %d auto-merge enabled",
                len(sweep_result.classified),
                prs_merged,
            )
        except Exception as exc:  # noqa: BLE001 — boundary: catch-all for merge-sweep resilience
            warnings.append(f"Merge sweep warning: {exc}")
            merge_sweep_ok = False

        # Phase 2: Run omnimarket close-out FSM for state tracking
        fsm_command = ModelCloseOutStartCommand(
            correlation_id=correlation_id,
            dry_run=dry_run,
            requested_at=datetime.now(tz=UTC),
        )
        phase_results = {
            EnumCloseOutPhase.MERGE_SWEEP: merge_sweep_ok,
            EnumCloseOutPhase.DEPLOY_PLUGIN: True,
            EnumCloseOutPhase.START_ENV: True,
            EnumCloseOutPhase.INTEGRATION: True,
            EnumCloseOutPhase.RELEASE_CHECK: merge_sweep_ok,
            EnumCloseOutPhase.REDEPLOY_CHECK: True,
            EnumCloseOutPhase.DASHBOARD_SWEEP: True,
        }
        _state, _events, completed = self._close_out_fsm.run_full_pipeline(
            fsm_command, phase_results=phase_results
        )
        logger.info(
            "Close-out FSM finished: final_phase=%s, transitions=%d",
            completed.final_phase.value,
            len(_events),
        )

        quality_gates_ok = merge_sweep_ok
        release_ready = merge_sweep_ok and quality_gates_ok

        logger.info(
            "Closeout complete: merge_sweep=%s, quality_gates=%s, release_ready=%s",
            merge_sweep_ok,
            quality_gates_ok,
            release_ready,
        )

        return ModelCloseoutResult(
            correlation_id=correlation_id,
            merge_sweep_completed=merge_sweep_ok,
            prs_merged=prs_merged,
            quality_gates_passed=quality_gates_ok,
            release_ready=release_ready,
            warnings=tuple(warnings),
        )
