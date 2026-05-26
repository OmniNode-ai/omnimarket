# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pydantic models for node_ticketing_insights_compute.

All models are frozen and pure-data — no I/O, no LLM calls, no side effects.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelVelocityMetrics(BaseModel):
    """Velocity metrics computed over rolling windows.

    Velocity is measured in issues-per-day using a weighted rolling average:
      - 7-day window:  50% weight (recent signal)
      - 14-day window: 30% weight (short-term trend)
      - 30-day window: 20% weight (baseline)
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    velocity_7d: float = Field(ge=0.0, description="Issues/day over last 7 days")
    velocity_14d: float = Field(ge=0.0, description="Issues/day over last 14 days")
    velocity_30d: float = Field(ge=0.0, description="Issues/day over last 30 days")
    velocity_weighted: float = Field(
        ge=0.0, description="Weighted rolling-average velocity"
    )
    burn_rate: float = Field(
        ge=0.0,
        description="Ratio of current velocity to planned velocity (1.0 = on track)",
    )
    confidence: str = Field(
        description="Velocity confidence classification: high | medium | low",
        pattern="^(high|medium|low)$",
    )


class ModelCompletionEstimate(BaseModel):
    """Milestone or project completion estimate derived from velocity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    project: str = Field(description="Project shortcut or name")
    total_issues: int = Field(ge=0)
    completed_issues: int = Field(ge=0)
    in_progress_issues: int = Field(ge=0)
    remaining_issues: int = Field(ge=0)
    estimated_days: float | None = Field(
        default=None,
        ge=0.0,
        description="Days to completion at current velocity; null when velocity is zero",
    )
    eta_date: str | None = Field(
        default=None,
        description="ISO 8601 estimated completion date; null when velocity is zero",
    )
    confidence: str = Field(
        description="ETA confidence: high | medium | low",
        pattern="^(high|medium|low)$",
    )


class ModelTrendData(BaseModel):
    """Historical trend series for velocity, rework, and CI stability."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Per-day velocity series: list of {"date": "YYYY-MM-DD", "velocity": float}
    daily_velocity: list[dict[str, float | str]] = Field(default_factory=list)
    # Weekly fix-vs-feature ratio series: list of {"week": "YYYY-Www", "fix_ratio": float}
    fix_vs_feature_weekly: list[dict[str, float | str]] = Field(default_factory=list)
    # CI clean rate per day: list of {"date": "YYYY-MM-DD", "ci_clean_rate": float}
    ci_clean_rate_daily: list[dict[str, float | str]] = Field(default_factory=list)
    # Trend direction for fix ratio: declining | stable | increasing | insufficient_data
    fix_ratio_trend: str = Field(
        default="insufficient_data",
        pattern="^(declining|stable|increasing|insufficient_data)$",
    )


class ModelPipelineMetrics(BaseModel):
    """Pipeline health metrics: cycle time, CI stability, rework."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cycle_time_p50_hours: float | None = Field(
        default=None,
        ge=0.0,
        description="Median ticket cycle time from in-progress to done, in hours",
    )
    cycle_time_p90_hours: float | None = Field(
        default=None,
        ge=0.0,
        description="90th-percentile cycle time, in hours",
    )
    ci_clean_rate: float = Field(
        ge=0.0,
        le=1.0,
        default=0.0,
        description="Fraction of PRs where CI passed on the first run",
    )
    rework_cycles_per_ticket: float = Field(
        ge=0.0,
        default=0.0,
        description="Average number of rework cycles (re-opens, re-reviews) per ticket",
    )
    feature_velocity: float = Field(
        ge=0.0,
        default=0.0,
        description="Feature issues closed per day (excludes fix/chore)",
    )
    skill_duration_p50_minutes: float | None = Field(
        default=None,
        ge=0.0,
        description="Median end-to-end skill execution duration, in minutes",
    )


class ModelGitHubMetrics(BaseModel):
    """GitHub repository statistics."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_prs_merged: int = Field(ge=0, default=0)
    total_commits: int = Field(ge=0, default=0)
    total_files_changed: int = Field(ge=0, default=0)
    lines_added: int = Field(ge=0, default=0)
    lines_deleted: int = Field(ge=0, default=0)
    # Per-repo breakdown: list of {"repo": str, "prs_merged": int, "commits": int}
    by_repo: list[dict[str, int | str]] = Field(default_factory=list)
    # Per-contributor breakdown: list of {"author": str, "commits": int, "prs": int}
    by_contributor: list[dict[str, int | str]] = Field(default_factory=list)


class ModelTicketingInsightsSummary(BaseModel):
    """High-level summary envelope for the full insights run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    velocity_score: int = Field(
        ge=0,
        le=100,
        description="0-100 score based on commit volume, PRs merged, issues completed",
    )
    effectiveness_score: int = Field(
        ge=0,
        le=100,
        description="0-100 score based on strategic value of completed work",
    )
    overall_assessment: str = Field(
        description="2-3 sentence narrative assessment of the analysis period",
    )
    key_findings: list[str] = Field(
        default_factory=list,
        description="Bullet-point list of key findings from the analysis",
    )
    blockers: list[str] = Field(
        default_factory=list,
        description="Active blockers identified from ticket data",
    )
