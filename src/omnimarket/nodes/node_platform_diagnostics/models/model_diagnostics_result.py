# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for node_platform_diagnostics."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
    EnumReadinessStatus,
)


class EnumDiagnosticDimension(StrEnum):
    """The 7 diagnostic dimensions."""

    CONTRACT_HEALTH = "CONTRACT_HEALTH"
    GOLDEN_CHAIN = "GOLDEN_CHAIN"
    RUNTIME_NODES = "RUNTIME_NODES"
    HOOK_HEALTH = "HOOK_HEALTH"
    DATABASE_PROJECTIONS = "DATABASE_PROJECTIONS"
    CI_STATUS = "CI_STATUS"
    COVERAGE = "COVERAGE"


class ModelDiagnosticDimensionResult(BaseModel):
    """Result for a single diagnostic dimension."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dimension: EnumDiagnosticDimension
    status: EnumReadinessStatus
    check_count: int = Field(ge=0)
    valid_zero: bool = False
    actionable_items: list[str] = Field(default_factory=list)
    evidence_source: str
    freshness_seconds: int | None = Field(default=None, ge=0)
    raw_detail: str = ""


class ModelDiagnosticsResult(BaseModel):
    """Output of the HandlerPlatformDiagnostics orchestrator."""

    model_config = ConfigDict(extra="forbid")

    overall_status: EnumReadinessStatus = EnumReadinessStatus.PASS
    dimensions: list[ModelDiagnosticDimensionResult] = Field(default_factory=list)
    run_duration_seconds: float = 0.0
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
