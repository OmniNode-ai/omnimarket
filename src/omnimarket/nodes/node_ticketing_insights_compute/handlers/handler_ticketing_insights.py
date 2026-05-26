# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeTicketingInsightsCompute — Pure compute handler for ticketing analytics.

Transforms pre-fetched Linear ticket data, GitHub PR metrics, and git history
into structured velocity metrics, trend analysis, and completion estimates.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls, no I/O.

Wave 1: contract + stub only.  Full implementation deferred to Wave 2 (OMN-12201).
The handler class is importable and passes type checks; `handle()` raises
NotImplementedError as declared by `node_not_implemented: true` in contract.yaml.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_ticketing_insights_compute.models.model_ticketing_insights import (
    ModelCompletionEstimate,
    ModelGitHubMetrics,
    ModelPipelineMetrics,
    ModelTicketingInsightsSummary,
    ModelTrendData,
    ModelVelocityMetrics,
)

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

_VALID_MODES = frozenset(
    {
        "deep-dive",
        "close-day",
        "project",
        "velocity",
        "suggest",
        "pipeline",
        "github",
        "all",
    }
)

_VALID_FORMATS = frozenset({"markdown", "json"})


class TicketingInsightsRequest(BaseModel):
    """Input envelope for the ticketing insights compute handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: str = Field(
        default="deep-dive",
        description="Report mode: deep-dive | close-day | project | velocity | suggest | pipeline | github | all",
    )
    team_filter: str | None = Field(default=None, description="Linear team key filter")
    project_filter: str | None = Field(
        default=None, description="Linear project shortcut or name"
    )
    date_from: str | None = Field(
        default=None, description="ISO 8601 start date (YYYY-MM-DD)"
    )
    date_to: str | None = Field(
        default=None, description="ISO 8601 end date (YYYY-MM-DD)"
    )
    ticket_ids: list[str] | None = Field(
        default=None, description="Explicit list of ticket IDs to include"
    )
    ticket_data: list[dict[str, Any]] = Field(
        default_factory=list, description="Pre-fetched ticket payloads from Linear MCP"
    )
    pr_data: list[dict[str, Any]] = Field(
        default_factory=list, description="Pre-fetched GitHub PR payloads"
    )
    git_commit_data: list[dict[str, Any]] = Field(
        default_factory=list, description="Pre-fetched git commit log entries"
    )
    include_confidence_intervals: bool = Field(
        default=False, description="Include velocity confidence intervals in output"
    )
    output_format: str = Field(
        default="markdown", description="Output format: markdown | json"
    )


class TicketingInsightsResult(BaseModel):
    """Output envelope for the ticketing insights compute handler."""

    model_config = ConfigDict(extra="forbid")

    mode: str
    velocity_metrics: ModelVelocityMetrics | None = None
    completion_estimates: list[ModelCompletionEstimate] = Field(default_factory=list)
    trend_data: ModelTrendData | None = None
    pipeline_metrics: ModelPipelineMetrics | None = None
    github_metrics: ModelGitHubMetrics | None = None
    report_markdown: str | None = None
    summary: ModelTicketingInsightsSummary | None = None


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class NodeTicketingInsightsCompute:
    """Pure compute handler — transforms pre-fetched data into analytics.

    Wave 1 contract-first node: importable and type-safe.  Full implementation in Wave 2.

    Per contract.yaml `node_not_implemented: true`, `handle()` raises
    NotImplementedError.  Callers should check the contract flag before
    invoking.
    """

    def handle(
        self, request: TicketingInsightsRequest
    ) -> TicketingInsightsResult:  # stub-ok
        """Execute ticketing insights analysis.

        Raises:
            NotImplementedError: contract.yaml node_not_implemented=true, Wave 2 in OMN-12201.
        """
        raise NotImplementedError(  # stub-ok
            "node_ticketing_insights_compute is a Wave 1 contract-first node. "
            "Full implementation is tracked in OMN-12201 Wave 2. "
            "See contract.yaml `node_not_implemented: true`."
        )
