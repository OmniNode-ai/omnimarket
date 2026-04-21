# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for PR health monitor output."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_merge_sweep_compute.handlers.handler_merge_sweep import (
    ModelPRInfo,
)


class EnumPrHealthStatus(StrEnum):
    """Health classification for a single PR."""

    HEALTHY = "healthy"
    STALE_RED = "stale_red"  # CI failing > threshold hours
    STALE_INACTIVE = "stale_inactive"  # No activity > threshold days
    CONFLICTED = "conflicted"  # Has merge conflicts
    REVIEW_BLOCKED = "review_blocked"  # Unresolved review threads / changes requested


class ModelPrHealthEntry(BaseModel):
    """A single PR with its health classification and reasons."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr: ModelPRInfo
    status: EnumPrHealthStatus
    reasons: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Human-readable reasons for the health status.",
    )
    age_days: int = Field(
        default=0,
        description="Age of the PR in days (from createdAt).",
        ge=0,
    )


class ModelPrHealthSummary(BaseModel):
    """Aggregate counts across all classified PRs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total: int = 0
    healthy: int = 0
    stale_red: int = 0
    stale_inactive: int = 0
    conflicted: int = 0
    review_blocked: int = 0


class ModelPrHealthReport(BaseModel):
    """Full health report: per-PR entries + aggregate summary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: tuple[ModelPrHealthEntry, ...] = Field(default_factory=tuple)
    summary: ModelPrHealthSummary = Field(default_factory=ModelPrHealthSummary)
    generated_at: str = Field(
        default="",
        description="ISO 8601 timestamp when the report was generated.",
    )

    @property
    def flagged(self) -> list[ModelPrHealthEntry]:
        """All non-healthy entries, ordered by severity then age."""
        _severity: dict[EnumPrHealthStatus, int] = {
            EnumPrHealthStatus.STALE_RED: 0,
            EnumPrHealthStatus.CONFLICTED: 1,
            EnumPrHealthStatus.REVIEW_BLOCKED: 2,
            EnumPrHealthStatus.STALE_INACTIVE: 3,
            EnumPrHealthStatus.HEALTHY: 4,
        }
        return sorted(
            [e for e in self.entries if e.status != EnumPrHealthStatus.HEALTHY],
            key=lambda e: (_severity[e.status], -e.age_days),
        )

    @property
    def healthy_prs(self) -> list[ModelPrHealthEntry]:
        return [e for e in self.entries if e.status == EnumPrHealthStatus.HEALTHY]


__all__: list[str] = [
    "EnumPrHealthStatus",
    "ModelPrHealthEntry",
    "ModelPrHealthReport",
    "ModelPrHealthSummary",
]
