# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeTicketingInsightsCompute — Pure compute handler for ticketing analytics.

Transforms pre-fetched Linear ticket data, GitHub PR metrics, and git history
into structured velocity metrics, trend analysis, and completion estimates.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls, no I/O.

The handler is pure and deterministic: all data must be supplied by the caller.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from statistics import median
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
    """Pure compute handler — transforms pre-fetched data into analytics."""

    def handle(self, request: TicketingInsightsRequest) -> TicketingInsightsResult:
        """Execute ticketing insights analysis."""
        tickets = _filter_tickets(request)
        today = _analysis_end_date(request)
        velocity = _velocity_metrics(tickets, today)
        pipeline = _pipeline_metrics(tickets, request.pr_data, today)
        github = _github_metrics(request.pr_data, request.git_commit_data)
        estimates = _completion_estimates(tickets, velocity, today)
        trend = _trend_data(tickets, request.pr_data, today)
        summary = _summary(tickets, velocity, pipeline, github)
        report = _markdown_report(
            mode=request.mode,
            velocity=velocity,
            pipeline=pipeline,
            github=github,
            estimates=estimates,
            summary=summary,
        )
        return TicketingInsightsResult(
            mode=request.mode,
            velocity_metrics=velocity,
            completion_estimates=estimates,
            trend_data=trend,
            pipeline_metrics=pipeline,
            github_metrics=github,
            report_markdown=report if request.output_format == "markdown" else None,
            summary=summary,
        )


def _filter_tickets(request: TicketingInsightsRequest) -> list[dict[str, Any]]:
    ticket_ids = set(request.ticket_ids or [])
    filtered: list[dict[str, Any]] = []
    for ticket in request.ticket_data:
        identifier = str(ticket.get("identifier") or ticket.get("id") or "")
        team = str(ticket.get("team") or ticket.get("teamKey") or "")
        project = str(ticket.get("project") or ticket.get("projectName") or "")
        if ticket_ids and identifier not in ticket_ids:
            continue
        if request.team_filter and request.team_filter not in team:
            continue
        if request.project_filter and request.project_filter not in project:
            continue
        filtered.append(ticket)
    return filtered


def _velocity_metrics(
    tickets: list[dict[str, Any]], today: date
) -> ModelVelocityMetrics:
    velocity_7d = _closed_count(tickets, today, 7) / 7
    velocity_14d = _closed_count(tickets, today, 14) / 14
    velocity_30d = _closed_count(tickets, today, 30) / 30
    weighted = (velocity_7d * 0.5) + (velocity_14d * 0.3) + (velocity_30d * 0.2)
    confidence = (
        "high" if len(tickets) >= 20 else "medium" if len(tickets) >= 5 else "low"
    )
    return ModelVelocityMetrics(
        velocity_7d=round(velocity_7d, 3),
        velocity_14d=round(velocity_14d, 3),
        velocity_30d=round(velocity_30d, 3),
        velocity_weighted=round(weighted, 3),
        burn_rate=round(weighted, 3),
        confidence=confidence,
    )


def _closed_count(tickets: list[dict[str, Any]], today: date, days: int) -> int:
    start = today - timedelta(days=days - 1)
    return sum(
        1
        for ticket in tickets
        if _is_done(ticket)
        and (closed_at := _ticket_closed_date(ticket)) is not None
        and start <= closed_at <= today
    )


def _pipeline_metrics(
    tickets: list[dict[str, Any]], pr_data: list[dict[str, Any]], today: date
) -> ModelPipelineMetrics:
    cycle_hours = [
        hours for ticket in tickets if (hours := _cycle_hours(ticket)) is not None
    ]
    merged_prs = [pr for pr in pr_data if _truthy(pr.get("merged"))]
    clean_prs = [
        pr for pr in merged_prs if _truthy(pr.get("ci_clean", pr.get("ciPassed")))
    ]
    feature_closed = [
        ticket
        for ticket in tickets
        if _is_done(ticket)
        and not _has_label(ticket, {"fix", "bug", "chore"})
        and (closed_at := _ticket_closed_date(ticket)) is not None
        and today - timedelta(days=29) <= closed_at <= today
    ]
    return ModelPipelineMetrics(
        cycle_time_p50_hours=round(median(cycle_hours), 3) if cycle_hours else None,
        cycle_time_p90_hours=_percentile(cycle_hours, 0.9),
        ci_clean_rate=round(len(clean_prs) / len(merged_prs), 3) if merged_prs else 0.0,
        rework_cycles_per_ticket=round(
            sum(
                int(ticket.get("reworkCycles", ticket.get("rework_cycles", 0)) or 0)
                for ticket in tickets
            )
            / len(tickets),
            3,
        )
        if tickets
        else 0.0,
        feature_velocity=round(len(feature_closed) / 30, 3),
    )


def _github_metrics(
    pr_data: list[dict[str, Any]], commits: list[dict[str, Any]]
) -> ModelGitHubMetrics:
    merged_prs = [pr for pr in pr_data if _truthy(pr.get("merged"))]
    repos: Counter[str] = Counter()
    contributors: Counter[str] = Counter()
    files_changed = lines_added = lines_deleted = 0
    for pr in merged_prs:
        repo = str(pr.get("repo") or pr.get("repository") or "unknown")
        repos[repo] += 1
        files_changed += int(pr.get("files_changed", pr.get("changedFiles", 0)) or 0)
        lines_added += int(pr.get("additions", pr.get("lines_added", 0)) or 0)
        lines_deleted += int(pr.get("deletions", pr.get("lines_deleted", 0)) or 0)
    for commit in commits:
        contributors[str(commit.get("author") or "unknown")] += 1
    return ModelGitHubMetrics(
        total_prs_merged=len(merged_prs),
        total_commits=len(commits),
        total_files_changed=files_changed,
        lines_added=lines_added,
        lines_deleted=lines_deleted,
        by_repo=[
            {"repo": repo, "prs_merged": count, "commits": 0}
            for repo, count in sorted(repos.items())
        ],
        by_contributor=[
            {"author": author, "commits": count, "prs": 0}
            for author, count in sorted(contributors.items())
        ],
    )


def _completion_estimates(
    tickets: list[dict[str, Any]], velocity: ModelVelocityMetrics, today: date
) -> list[ModelCompletionEstimate]:
    by_project: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ticket in tickets:
        by_project[
            str(ticket.get("project") or ticket.get("projectName") or "Unassigned")
        ].append(ticket)

    estimates: list[ModelCompletionEstimate] = []
    for project, project_tickets in sorted(by_project.items()):
        completed = sum(1 for ticket in project_tickets if _is_done(ticket))
        in_progress = sum(1 for ticket in project_tickets if _is_in_progress(ticket))
        remaining = len(project_tickets) - completed
        estimated_days = (
            round(remaining / velocity.velocity_weighted, 1)
            if velocity.velocity_weighted > 0 and remaining > 0
            else None
        )
        estimates.append(
            ModelCompletionEstimate(
                project=project,
                total_issues=len(project_tickets),
                completed_issues=completed,
                in_progress_issues=in_progress,
                remaining_issues=remaining,
                estimated_days=estimated_days,
                eta_date=(today + timedelta(days=int(estimated_days))).isoformat()
                if estimated_days is not None
                else None,
                confidence=velocity.confidence,
            )
        )
    return estimates


def _trend_data(
    tickets: list[dict[str, Any]], pr_data: list[dict[str, Any]], today: date
) -> ModelTrendData:
    daily_velocity = []
    ci_clean_rate_daily = []
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        closed = sum(
            1
            for ticket in tickets
            if _is_done(ticket) and _ticket_closed_date(ticket) == day
        )
        prs = [
            pr
            for pr in pr_data
            if _parse_date(pr.get("mergedAt") or pr.get("merged_at")) == day
        ]
        clean = [pr for pr in prs if _truthy(pr.get("ci_clean", pr.get("ciPassed")))]
        daily_velocity.append({"date": day.isoformat(), "velocity": float(closed)})
        ci_clean_rate_daily.append(
            {
                "date": day.isoformat(),
                "ci_clean_rate": round(len(clean) / len(prs), 3) if prs else 0.0,
            }
        )
    return ModelTrendData(
        daily_velocity=daily_velocity,
        ci_clean_rate_daily=ci_clean_rate_daily,
        fix_ratio_trend="insufficient_data",
    )


def _summary(
    tickets: list[dict[str, Any]],
    velocity: ModelVelocityMetrics,
    pipeline: ModelPipelineMetrics,
    github: ModelGitHubMetrics,
) -> ModelTicketingInsightsSummary:
    completed = sum(1 for ticket in tickets if _is_done(ticket))
    blockers = [
        str(ticket.get("identifier") or ticket.get("id") or ticket.get("title"))
        for ticket in tickets
        if _has_label(ticket, {"blocked", "blocker"}) or _truthy(ticket.get("blocked"))
    ]
    velocity_score = min(
        100, int((velocity.velocity_weighted * 20) + github.total_prs_merged * 5)
    )
    effectiveness_score = min(100, int((completed * 6) + (pipeline.ci_clean_rate * 20)))
    return ModelTicketingInsightsSummary(
        velocity_score=velocity_score,
        effectiveness_score=effectiveness_score,
        overall_assessment=(
            f"Processed {len(tickets)} tickets with {completed} completed. "
            f"Weighted velocity is {velocity.velocity_weighted} issues/day."
        ),
        key_findings=[
            f"{completed} completed tickets",
            f"{github.total_prs_merged} merged PRs",
        ],
        blockers=blockers,
    )


def _markdown_report(
    *,
    mode: str,
    velocity: ModelVelocityMetrics,
    pipeline: ModelPipelineMetrics,
    github: ModelGitHubMetrics,
    estimates: list[ModelCompletionEstimate],
    summary: ModelTicketingInsightsSummary,
) -> str:
    lines = [
        f"# Ticketing Insights ({mode})",
        "",
        summary.overall_assessment,
        "",
        f"- Weighted velocity: {velocity.velocity_weighted}",
        f"- CI clean rate: {pipeline.ci_clean_rate}",
        f"- Merged PRs: {github.total_prs_merged}",
    ]
    for estimate in estimates:
        lines.append(
            f"- {estimate.project}: {estimate.remaining_issues} remaining, ETA {estimate.eta_date or 'n/a'}"
        )
    return "\n".join(lines)


def _ticket_closed_date(ticket: dict[str, Any]) -> date | None:
    return _parse_date(
        ticket.get("completedAt")
        or ticket.get("completed_at")
        or ticket.get("closedAt")
    )


def _parse_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _analysis_end_date(request: TicketingInsightsRequest) -> date:
    explicit_date = _parse_date(request.date_to)
    if explicit_date is not None:
        return explicit_date

    candidate_dates = [
        parsed
        for parsed in (
            *(_ticket_closed_date(ticket) for ticket in request.ticket_data),
            *(
                _parse_date(pr.get("mergedAt") or pr.get("merged_at"))
                for pr in request.pr_data
            ),
            *(
                _parse_date(commit.get("date") or commit.get("committed_at"))
                for commit in request.git_commit_data
            ),
        )
        if parsed is not None
    ]
    return max(candidate_dates) if candidate_dates else date(1970, 1, 1)


def _cycle_hours(ticket: dict[str, Any]) -> float | None:
    started = _parse_datetime(ticket.get("startedAt") or ticket.get("started_at"))
    closed = _parse_datetime(
        ticket.get("completedAt")
        or ticket.get("completed_at")
        or ticket.get("closedAt")
    )
    if started is None or closed is None or closed < started:
        return None
    return (closed - started).total_seconds() / 3600


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_done(ticket: dict[str, Any]) -> bool:
    state = str(ticket.get("state") or ticket.get("status") or "").lower()
    return (
        state in {"done", "completed", "closed", "merged"}
        or _ticket_closed_date(ticket) is not None
    )


def _is_in_progress(ticket: dict[str, Any]) -> bool:
    state = str(ticket.get("state") or ticket.get("status") or "").lower()
    return state in {"in progress", "started", "review", "in review"}


def _has_label(ticket: dict[str, Any], labels: set[str]) -> bool:
    raw_labels = ticket.get("labels") or []
    normalized = {str(label).lower() for label in raw_labels}
    return bool(normalized & labels)


def _truthy(value: object) -> bool:
    return bool(value) and str(value).lower() not in {"false", "0", "none"}


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * percentile))
    return round(ordered[index], 3)
