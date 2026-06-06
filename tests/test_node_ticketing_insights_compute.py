"""Unit tests for node_ticketing_insights_compute."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_ticketing_insights_compute import (
    ModelCompletionEstimate,
    ModelGitHubMetrics,
    ModelPipelineMetrics,
    ModelTicketingInsightsSummary,
    ModelTrendData,
    ModelVelocityMetrics,
    NodeTicketingInsightsCompute,
    TicketingInsightsRequest,
    TicketingInsightsResult,
)

# ---------------------------------------------------------------------------
# Import / public surface
# ---------------------------------------------------------------------------


class TestPublicSurface:
    @pytest.mark.unit
    def test_all_symbols_importable(self) -> None:
        assert NodeTicketingInsightsCompute is not None
        assert TicketingInsightsRequest is not None
        assert TicketingInsightsResult is not None
        assert ModelVelocityMetrics is not None
        assert ModelCompletionEstimate is not None
        assert ModelTrendData is not None
        assert ModelPipelineMetrics is not None
        assert ModelGitHubMetrics is not None
        assert ModelTicketingInsightsSummary is not None


# ---------------------------------------------------------------------------
# Request model validation
# ---------------------------------------------------------------------------


class TestTicketingInsightsRequest:
    @pytest.mark.unit
    def test_default_mode_is_deep_dive(self) -> None:
        req = TicketingInsightsRequest()
        assert req.mode == "deep-dive"

    @pytest.mark.unit
    def test_default_output_format_is_markdown(self) -> None:
        req = TicketingInsightsRequest()
        assert req.output_format == "markdown"

    @pytest.mark.unit
    def test_all_optional_fields_default_to_none_or_empty(self) -> None:
        req = TicketingInsightsRequest()
        assert req.team_filter is None
        assert req.project_filter is None
        assert req.date_from is None
        assert req.date_to is None
        assert req.ticket_ids is None
        assert req.ticket_data == []
        assert req.pr_data == []
        assert req.git_commit_data == []
        assert req.include_confidence_intervals is False

    @pytest.mark.unit
    def test_explicit_mode_roundtrips(self) -> None:
        for mode in (
            "velocity",
            "project",
            "pipeline",
            "github",
            "suggest",
            "all",
            "close-day",
        ):
            req = TicketingInsightsRequest(mode=mode)
            assert req.mode == mode

    @pytest.mark.unit
    def test_request_is_frozen(self) -> None:
        req = TicketingInsightsRequest()
        with pytest.raises(ValidationError):
            req.mode = "velocity"  # type: ignore[misc]

    @pytest.mark.unit
    def test_extra_fields_forbidden(self) -> None:

        with pytest.raises(ValidationError):
            TicketingInsightsRequest(unknown_field="bad")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Model validation — VelocityMetrics
# ---------------------------------------------------------------------------


class TestModelVelocityMetrics:
    @pytest.mark.unit
    def test_valid_velocity_metrics(self) -> None:
        m = ModelVelocityMetrics(
            velocity_7d=2.3,
            velocity_14d=1.8,
            velocity_30d=1.5,
            velocity_weighted=2.0,
            burn_rate=1.1,
            confidence="medium",
        )
        assert m.velocity_weighted == 2.0
        assert m.confidence == "medium"

    @pytest.mark.unit
    def test_confidence_must_be_valid(self) -> None:

        with pytest.raises(ValidationError):
            ModelVelocityMetrics(
                velocity_7d=1.0,
                velocity_14d=1.0,
                velocity_30d=1.0,
                velocity_weighted=1.0,
                burn_rate=1.0,
                confidence="excellent",  # invalid
            )

    @pytest.mark.unit
    def test_velocity_metrics_frozen(self) -> None:
        m = ModelVelocityMetrics(
            velocity_7d=1.0,
            velocity_14d=1.0,
            velocity_30d=1.0,
            velocity_weighted=1.0,
            burn_rate=1.0,
            confidence="high",
        )
        with pytest.raises(ValidationError):
            m.velocity_7d = 99.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Model validation — CompletionEstimate
# ---------------------------------------------------------------------------


class TestModelCompletionEstimate:
    @pytest.mark.unit
    def test_valid_completion_estimate(self) -> None:
        m = ModelCompletionEstimate(
            project="MVP",
            total_issues=71,
            completed_issues=28,
            in_progress_issues=5,
            remaining_issues=43,
            estimated_days=21.5,
            eta_date="2026-06-15",
            confidence="medium",
        )
        assert m.project == "MVP"
        assert m.estimated_days == 21.5

    @pytest.mark.unit
    def test_null_eta_when_velocity_zero(self) -> None:
        m = ModelCompletionEstimate(
            project="Beta",
            total_issues=30,
            completed_issues=0,
            in_progress_issues=0,
            remaining_issues=30,
            estimated_days=None,
            eta_date=None,
            confidence="low",
        )
        assert m.estimated_days is None
        assert m.eta_date is None


# ---------------------------------------------------------------------------
# Handler behavior
# ---------------------------------------------------------------------------


class TestNodeTicketingInsightsComputeBehavior:
    @pytest.mark.unit
    def test_handle_computes_metrics_from_prefetched_data(self) -> None:
        handler = NodeTicketingInsightsCompute()
        req = TicketingInsightsRequest(
            mode="all",
            date_to="2026-05-28",
            ticket_data=[
                {
                    "identifier": "OMN-1",
                    "project": "Runtime",
                    "state": "Done",
                    "startedAt": "2026-05-25T00:00:00",
                    "completedAt": "2026-05-27T00:00:00",
                    "labels": ["feature"],
                },
                {
                    "identifier": "OMN-2",
                    "project": "Runtime",
                    "state": "In Progress",
                    "labels": ["blocked"],
                },
            ],
            pr_data=[
                {
                    "merged": True,
                    "repo": "omnimarket",
                    "changedFiles": 3,
                    "additions": 10,
                    "deletions": 2,
                    "ciPassed": True,
                    "mergedAt": "2026-05-27T00:00:00",
                }
            ],
            git_commit_data=[{"author": "dev"}],
        )

        result = handler.handle(req)

        assert result.velocity_metrics is not None
        assert result.velocity_metrics.velocity_7d == round(1 / 7, 3)
        assert result.pipeline_metrics is not None
        assert result.pipeline_metrics.cycle_time_p50_hours == 48.0
        assert result.github_metrics is not None
        assert result.github_metrics.total_prs_merged == 1
        assert result.completion_estimates[0].remaining_issues == 1
        assert result.summary is not None
        assert result.summary.blockers == ["OMN-2"]
        assert result.report_markdown is not None

    @pytest.mark.unit
    def test_handler_instantiates_without_args(self) -> None:
        # Pure compute — no dependencies injected
        handler = NodeTicketingInsightsCompute()
        assert handler is not None

    @pytest.mark.unit
    def test_missing_date_to_uses_supplied_data_not_wall_clock(self) -> None:
        handler = NodeTicketingInsightsCompute()
        req = TicketingInsightsRequest(
            ticket_data=[
                {
                    "identifier": "OMN-1",
                    "project": "Runtime",
                    "state": "Done",
                    "completedAt": "2026-05-20T00:00:00",
                },
                {
                    "identifier": "OMN-2",
                    "project": "Runtime",
                    "state": "Done",
                    "completedAt": "2026-05-10T00:00:00",
                },
            ],
        )

        result = handler.handle(req)

        assert result.velocity_metrics is not None
        assert result.velocity_metrics.velocity_7d == round(1 / 7, 3)
        assert result.trend_data is not None
        assert result.trend_data.daily_velocity[-1] == {
            "date": "2026-05-20",
            "velocity": 1.0,
        }
