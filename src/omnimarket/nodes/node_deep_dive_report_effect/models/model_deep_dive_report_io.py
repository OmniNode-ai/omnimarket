# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed I/O models for node_deep_dive_report_effect (OMN-13725).

The EFFECT consumes a ``ModelDeepDiveReportCommand`` and emits a
``ModelDeepDiveReportResultEvent`` carrying the report markdown plus
structured metrics.  All git/``gh`` reads are performed through the injected
``ProtocolReportDataSource`` adapter — never in the handler body — so these
models are deterministic given the adapter's returned data.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelDeepDiveReportCommand(BaseModel):
    """Command: generate the daily deep-dive report for a workspace + date.

    The ``root`` and date are *intent only* — the actual git/``gh`` reads are
    resolved by the injected ``ProtocolReportDataSource``, so the deployment
    lane (local clones, ``.201`` clones, remote fetch) is a DI concern, not a
    field on this command.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: UUID = Field(..., description="Trace correlation id.")
    root: str = Field(
        ...,
        description="Workspace root whose direct-child repos are scanned. "
        "Interpreted by the injected data-source adapter.",
    )
    date: str | None = Field(
        default=None,
        description="Target day YYYY-MM-DD; None means the adapter resolves "
        "'today' at the effect boundary.",
    )
    repo_prefixes: tuple[str, ...] = Field(
        default=("omni", "onex_"),
        description="Only repos whose name starts with one of these prefixes "
        "are included in the report.",
    )
    include_dirty: bool = Field(
        default=False,
        description="Include repos with only dirty working trees (no commits).",
    )
    dry_run: bool = Field(
        default=False,
        description="When True the adapter performs no reads and the report is "
        "an empty quiet-day payload (used for wiring smoke tests).",
    )


class ModelDeepDiveMetrics(BaseModel):
    """Structured, machine-readable metrics derived from the day's scan."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    effectiveness_score: int = Field(..., ge=0, le=100)
    velocity_score: int = Field(..., ge=0, le=100)
    drift_level: str = Field(..., description="green | yellow | red")
    drift_penalty: int = Field(...)
    total_prs: int = Field(..., ge=0)
    unlinked_pr_count: int = Field(..., ge=0)
    category_counts: dict[str, int] = Field(default_factory=dict)
    unique_commits: int = Field(..., ge=0)
    repos_active: int = Field(..., ge=0)
    ticket_ids: tuple[str, ...] = Field(default_factory=tuple)


class ModelDeepDiveReportResultEvent(BaseModel):
    """Result event: rendered report markdown + structured metrics."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    correlation_id: UUID = Field(...)
    report_date: str = Field(..., description="Resolved report day YYYY-MM-DD.")
    report_markdown: str = Field(..., description="Rendered deep-dive markdown.")
    metrics: ModelDeepDiveMetrics = Field(...)
    quiet_day: bool = Field(
        ...,
        description="True when no commits and no merged PRs were found "
        "(decision #4: always emit a payload so the channel proves the job ran).",
    )
