# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPlatformDiagnostics — composable 7-dimension platform health check orchestrator.

Composes async dimension checks via asyncio.gather and derives an overall PASS/WARN/FAIL.

ONEX node type: ORCHESTRATOR
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_platform_diagnostics.handlers.dimension_checks import (
    DiagnosticsCheckContext,
    run_dimension_checks,
)
from omnimarket.nodes.node_platform_diagnostics.models.model_diagnostics_result import (
    EnumDiagnosticDimension,
    ModelDiagnosticDimensionResult,
    ModelDiagnosticsResult,
)
from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
    EnumReadinessStatus,
)

_ALL_DIMENSIONS = list(EnumDiagnosticDimension)


class ModelDiagnosticsRequest(BaseModel):
    """Input for HandlerPlatformDiagnostics.

    When dimensions is empty, all 7 dimensions are checked.
    """

    model_config = ConfigDict(extra="forbid")

    dimensions: list[EnumDiagnosticDimension] = Field(default_factory=list)
    dry_run: bool = False
    freshness_threshold_hours: int = Field(default=4, ge=1, le=168)


class HandlerPlatformDiagnostics:
    """Orchestrate 7 async dimension checks and aggregate into a diagnostics report."""

    async def handle(self, request: ModelDiagnosticsRequest) -> ModelDiagnosticsResult:
        """Run dimension checks and produce a tri-state diagnostics result.

        1. Build CheckContext from request parameters
        2. Select dimensions (all if request.dimensions is empty)
        3. Run all selected checks in parallel via asyncio.gather
        4. Derive overall_status: any FAIL → FAIL; any WARN → WARN; else PASS
        5. Return ModelDiagnosticsResult
        """
        start_time = time.monotonic()

        ctx = DiagnosticsCheckContext(
            freshness_threshold_hours=request.freshness_threshold_hours,
            dry_run=request.dry_run,
        )

        selected_dimensions = (
            request.dimensions if request.dimensions else _ALL_DIMENSIONS
        )
        dimension_results = await run_dimension_checks(ctx, selected_dimensions)

        overall_status = self._derive_overall_status(dimension_results)
        run_duration = time.monotonic() - start_time

        return ModelDiagnosticsResult(
            overall_status=overall_status,
            dimensions=dimension_results,
            run_duration_seconds=round(run_duration, 3),
            generated_at=datetime.now(UTC),
        )

    def _derive_overall_status(
        self, results: list[ModelDiagnosticDimensionResult]
    ) -> EnumReadinessStatus:
        """FAIL > WARN > PASS — any FAIL produces FAIL, any WARN (no FAIL) produces WARN."""
        statuses = {r.status for r in results}
        if EnumReadinessStatus.FAIL in statuses:
            return EnumReadinessStatus.FAIL
        if EnumReadinessStatus.WARN in statuses:
            return EnumReadinessStatus.WARN
        return EnumReadinessStatus.PASS
