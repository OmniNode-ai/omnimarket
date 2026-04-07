"""Handler that executes the close-out phase: merge-sweep, quality gates, release readiness.

This is an EFFECT handler -- performs external I/O via protocol-based DI.

Related:
    - OMN-7580: Migrate node_closeout_effect to omnimarket
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

import logging
from typing import Literal
from uuid import UUID

from omnimarket.nodes.node_closeout_effect.models.model_closeout_result import (
    ModelCloseoutResult,
)
from omnimarket.nodes.node_closeout_effect.protocols import (
    ProtocolMergeSweeper,
    ProtocolQualityGateChecker,
)

logger = logging.getLogger(__name__)


class HandlerCloseout:
    """Executes close-out phase: merge-sweep, quality gates, release readiness.

    Dependencies are injected via constructor (protocol-based DI).
    In dry-run mode, returns a synthetic success result without side effects.
    """

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    def __init__(
        self,
        merge_sweeper: ProtocolMergeSweeper | None = None,
        quality_gate_checker: ProtocolQualityGateChecker | None = None,
    ) -> None:
        self._merge_sweeper = merge_sweeper
        self._quality_gate_checker = quality_gate_checker

    async def handle(
        self,
        correlation_id: UUID,
        dry_run: bool = False,
    ) -> ModelCloseoutResult:
        """Execute close-out phase.

        Steps:
            1. Run merge-sweep (enable auto-merge on ready PRs)
            2. Check quality gates
            3. Verify release readiness

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

        warnings: list[str] = []

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

        # Phase 1: Merge sweep
        merge_sweep_ok = True
        prs_merged = 0
        try:
            if self._merge_sweeper is not None:
                prs_merged = await self._merge_sweeper.sweep(dry_run=dry_run)
            else:
                logger.info("Merge sweep: no sweeper injected, skipping")
            merge_sweep_ok = True
        except Exception as exc:
            warnings.append(f"Merge sweep warning: {exc}")
            merge_sweep_ok = False

        # Phase 2: Quality gates
        quality_gates_ok = True
        try:
            if self._quality_gate_checker is not None:
                quality_gates_ok = await self._quality_gate_checker.check(
                    dry_run=dry_run
                )
            else:
                logger.info("Quality gates: no checker injected, skipping")
        except Exception as exc:
            warnings.append(f"Quality gates warning: {exc}")
            quality_gates_ok = False

        # Phase 3: Release readiness
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


__all__: list[str] = ["HandlerCloseout"]
