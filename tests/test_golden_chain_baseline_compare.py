# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerBaselineCompare.

Tests diff logic, error handling, and artifact I/O. All external I/O is mocked.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
    ModelBaselineSnapshot,
    ModelGitHubPRSnapshot,
    ModelKafkaTopicSnapshot,
    ModelLinearTicketSnapshot,
    ModelServiceHealthSnapshot,
)
from omnimarket.nodes.node_baseline_compare.handlers.handler_baseline_compare import (
    HandlerBaselineCompare,
    ModelBaselineCompareRequest,
    ModelBaselineCompareResult,
    _diff_db_row_counts,
    _diff_github_prs,
    _diff_kafka_topics,
    _diff_linear_tickets,
    _diff_system_health,
)

_NOW = datetime(2026, 4, 9, 0, 0, 0, tzinfo=UTC)
_EARLIER = _NOW - timedelta(hours=4)


def _make_baseline(
    baseline_id: str = "test-baseline",
    captured_at: datetime = _EARLIER,
    probes: dict | None = None,
) -> ModelBaselineSnapshot:
    return ModelBaselineSnapshot(
        baseline_id=baseline_id,
        captured_at=captured_at,
        label=None,
        probes=probes or {},
    )


def _write_baseline(tmp_path: Path, baseline: ModelBaselineSnapshot) -> Path:
    baselines_dir = tmp_path / ".onex_state" / "baselines"
    baselines_dir.mkdir(parents=True)
    artifact = baselines_dir / f"{baseline.baseline_id}.json"
    artifact.write_text(baseline.model_dump_json(indent=2), encoding="utf-8")
    return artifact


# ---------------------------------------------------------------------------
# Diff function unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDiffGitHubPRs:
    def test_detects_new_prs(self) -> None:
        before: list = []
        after = [
            ModelGitHubPRSnapshot(
                pr_number=42,
                title="feat",
                repo="org/repo",
                state="OPEN",
                labels=[],
                age_days=1.0,
                ci_status=None,
            )
        ]
        delta = _diff_github_prs(before, after)
        assert 42 in delta.opened
        assert delta.merged == []
        assert delta.closed == []

    def test_detects_merged_prs(self) -> None:
        pr = ModelGitHubPRSnapshot(
            pr_number=10,
            title="fix",
            repo="org/repo",
            state="MERGED",
            labels=[],
            age_days=2.0,
            ci_status=None,
        )
        delta = _diff_github_prs([pr], [])
        assert 10 in delta.merged
        assert 10 not in delta.closed

    def test_detects_state_changes(self) -> None:
        pr_before = ModelGitHubPRSnapshot(
            pr_number=7,
            title="wip",
            repo="org/repo",
            state="OPEN",
            labels=[],
            age_days=0.5,
            ci_status="pending",
        )
        pr_after = ModelGitHubPRSnapshot(
            pr_number=7,
            title="wip",
            repo="org/repo",
            state="OPEN",
            labels=[],
            age_days=0.5,
            ci_status="failure",
        )
        delta = _diff_github_prs([pr_before], [pr_after])
        assert 7 in delta.track_changes
        assert "failure" in delta.track_changes[7]

    def test_empty_delta_when_no_changes(self) -> None:
        pr = ModelGitHubPRSnapshot(
            pr_number=1,
            title="stable",
            repo="org/repo",
            state="OPEN",
            labels=[],
            age_days=1.0,
            ci_status="success",
        )
        delta = _diff_github_prs([pr], [pr])
        assert delta.opened == []
        assert delta.merged == []
        assert delta.closed == []
        assert delta.track_changes == {}


@pytest.mark.unit
class TestDiffLinearTickets:
    def test_detects_new_ticket(self) -> None:
        new_ticket = ModelLinearTicketSnapshot(
            ticket_id="OMN-9999",
            title="New work",
            state="Todo",
            priority=2,
            assignee=None,
            updated_at=_NOW,
        )
        delta = _diff_linear_tickets([], [new_ticket])
        assert "OMN-9999" in delta.opened

    def test_detects_done_ticket(self) -> None:
        ticket = ModelLinearTicketSnapshot(
            ticket_id="OMN-1000",
            title="Old work",
            state="In Progress",
            priority=1,
            assignee=None,
            updated_at=_EARLIER,
        )
        delta = _diff_linear_tickets([ticket], [])
        assert "OMN-1000" not in delta.closed_done  # not Done state

    def test_detects_state_change(self) -> None:
        before = ModelLinearTicketSnapshot(
            ticket_id="OMN-2000",
            title="Work",
            state="Todo",
            priority=2,
            assignee=None,
            updated_at=_EARLIER,
        )
        after = ModelLinearTicketSnapshot(
            ticket_id="OMN-2000",
            title="Work",
            state="In Progress",
            priority=2,
            assignee=None,
            updated_at=_NOW,
        )
        delta = _diff_linear_tickets([before], [after])
        assert "OMN-2000" in delta.state_changes
        assert "In Progress" in delta.state_changes["OMN-2000"]


@pytest.mark.unit
class TestDiffSystemHealth:
    def test_detects_degraded(self) -> None:
        before = [
            ModelServiceHealthSnapshot(
                service="llm-coder", healthy=True, latency_ms=50.0, error=None
            )
        ]
        after = [
            ModelServiceHealthSnapshot(
                service="llm-coder", healthy=False, latency_ms=None, error="timeout"
            )
        ]
        delta = _diff_system_health(before, after)
        assert "llm-coder" in delta.degraded
        assert "llm-coder" not in delta.recovered

    def test_detects_recovered(self) -> None:
        before = [
            ModelServiceHealthSnapshot(
                service="qdrant", healthy=False, latency_ms=None, error="refused"
            )
        ]
        after = [
            ModelServiceHealthSnapshot(
                service="qdrant", healthy=True, latency_ms=12.0, error=None
            )
        ]
        delta = _diff_system_health(before, after)
        assert "qdrant" in delta.recovered
        assert "qdrant" not in delta.degraded


@pytest.mark.unit
class TestDiffKafkaTopics:
    def test_detects_created_topic(self) -> None:
        new_topic = ModelKafkaTopicSnapshot(
            topic="onex.cmd.omnimarket.new-event.v1",
            partition_count=3,
            latest_offset=0,
        )
        delta = _diff_kafka_topics([], [new_topic])
        assert "onex.cmd.omnimarket.new-event.v1" in delta.created

    def test_detects_offset_advance(self) -> None:
        before = [
            ModelKafkaTopicSnapshot(
                topic="onex.evt.foo.v1", partition_count=1, latest_offset=100
            )
        ]
        after = [
            ModelKafkaTopicSnapshot(
                topic="onex.evt.foo.v1", partition_count=1, latest_offset=150
            )
        ]
        delta = _diff_kafka_topics(before, after)
        assert delta.offset_advances.get("onex.evt.foo.v1") == 50


@pytest.mark.unit
class TestDiffDbRowCounts:
    def test_detects_growth(self) -> None:
        from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
            ModelDbRowCountSnapshot,
        )

        before = [
            ModelDbRowCountSnapshot(table_name="projection_registration", row_count=100)
        ]
        after = [
            ModelDbRowCountSnapshot(table_name="projection_registration", row_count=150)
        ]
        delta = _diff_db_row_counts(before, after)
        assert "projection_registration" in delta.grown
        assert delta.row_delta_by_table["projection_registration"] == 50

    def test_detects_shrink(self) -> None:
        from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
            ModelDbRowCountSnapshot,
        )

        before = [
            ModelDbRowCountSnapshot(table_name="projection_delegation", row_count=200)
        ]
        after = [
            ModelDbRowCountSnapshot(table_name="projection_delegation", row_count=180)
        ]
        delta = _diff_db_row_counts(before, after)
        assert "projection_delegation" in delta.shrunk
        assert delta.row_delta_by_table["projection_delegation"] == -20

    def test_unchanged_table(self) -> None:
        from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
            ModelDbRowCountSnapshot,
        )

        snap = ModelDbRowCountSnapshot(table_name="projection_savings", row_count=42)
        delta = _diff_db_row_counts([snap], [snap])
        assert "projection_savings" in delta.unchanged
        assert delta.row_delta_by_table["projection_savings"] == 0


# ---------------------------------------------------------------------------
# HandlerBaselineCompare integration tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHandlerBaselineCompare:
    async def test_missing_baseline_returns_error_result(self, tmp_path: Path) -> None:
        """Missing artifact -> error result, does not raise."""
        handler = HandlerBaselineCompare()
        request = ModelBaselineCompareRequest(
            baseline_id="nonexistent",
            omni_home=str(tmp_path),
        )
        result = await handler.handle(request)

        assert isinstance(result, ModelBaselineCompareResult)
        assert result.error is not None
        assert "not found" in result.error.lower()

    async def test_compare_detects_new_prs(self, tmp_path: Path) -> None:
        """Baseline has 1 PR; current has 2 -> delta.opened has 1 new PR."""
        pr_before = ModelGitHubPRSnapshot(
            pr_number=1,
            title="existing",
            repo="org/repo",
            state="OPEN",
            labels=[],
            age_days=1.0,
            ci_status=None,
        )
        pr_new = ModelGitHubPRSnapshot(
            pr_number=2,
            title="new pr",
            repo="org/repo",
            state="OPEN",
            labels=[],
            age_days=0.1,
            ci_status=None,
        )
        baseline = _make_baseline(probes={"github_prs": [pr_before]})
        _write_baseline(tmp_path, baseline)

        current = _make_baseline(
            baseline_id="test-baseline__current",
            captured_at=_NOW,
            probes={"github_prs": [pr_before, pr_new]},
        )

        handler = HandlerBaselineCompare()
        request = ModelBaselineCompareRequest(
            baseline_id="test-baseline",
            omni_home=str(tmp_path),
            current_snapshot=current,
            dry_run=True,
        )
        result = await handler.handle(request)

        assert result.error is None
        gh_delta = result.delta.per_probe_deltas.get("github_prs")
        assert gh_delta is not None
        assert 2 in gh_delta.opened
        assert 1 not in gh_delta.opened

    async def test_compare_detects_merged_prs(self, tmp_path: Path) -> None:
        """Baseline has PR #100; current does not -> delta.merged or closed."""
        pr = ModelGitHubPRSnapshot(
            pr_number=100,
            title="merged pr",
            repo="org/repo",
            state="MERGED",
            labels=[],
            age_days=3.0,
            ci_status="success",
        )
        baseline = _make_baseline(probes={"github_prs": [pr]})
        _write_baseline(tmp_path, baseline)

        current = _make_baseline(
            baseline_id="test-baseline__current",
            captured_at=_NOW,
            probes={"github_prs": []},
        )

        handler = HandlerBaselineCompare()
        request = ModelBaselineCompareRequest(
            baseline_id="test-baseline",
            omni_home=str(tmp_path),
            current_snapshot=current,
            dry_run=True,
        )
        result = await handler.handle(request)

        assert result.error is None
        gh_delta = result.delta.per_probe_deltas.get("github_prs")
        assert gh_delta is not None
        assert 100 in gh_delta.merged

    async def test_compare_detects_ticket_state_change(self, tmp_path: Path) -> None:
        """Ticket state changes -> captured in delta.state_changes."""
        ticket_before = ModelLinearTicketSnapshot(
            ticket_id="OMN-7777",
            title="task",
            state="Todo",
            priority=2,
            assignee=None,
            updated_at=_EARLIER,
        )
        ticket_after = ModelLinearTicketSnapshot(
            ticket_id="OMN-7777",
            title="task",
            state="In Progress",
            priority=2,
            assignee=None,
            updated_at=_NOW,
        )
        baseline = _make_baseline(probes={"linear_tickets": [ticket_before]})
        _write_baseline(tmp_path, baseline)

        current = _make_baseline(
            baseline_id="test-baseline__current",
            captured_at=_NOW,
            probes={"linear_tickets": [ticket_after]},
        )

        handler = HandlerBaselineCompare()
        request = ModelBaselineCompareRequest(
            baseline_id="test-baseline",
            omni_home=str(tmp_path),
            current_snapshot=current,
            dry_run=True,
        )
        result = await handler.handle(request)

        linear_delta = result.delta.per_probe_deltas.get("linear_tickets")
        assert linear_delta is not None
        assert "OMN-7777" in linear_delta.state_changes

    async def test_empty_delta_when_no_changes(self, tmp_path: Path) -> None:
        """Before == after -> all delta counts are zero."""
        pr = ModelGitHubPRSnapshot(
            pr_number=5,
            title="stable",
            repo="org/repo",
            state="OPEN",
            labels=[],
            age_days=1.0,
            ci_status="success",
        )
        baseline = _make_baseline(probes={"github_prs": [pr]})
        _write_baseline(tmp_path, baseline)

        current = _make_baseline(
            baseline_id="test-baseline__current",
            captured_at=_NOW,
            probes={"github_prs": [pr]},
        )

        handler = HandlerBaselineCompare()
        request = ModelBaselineCompareRequest(
            baseline_id="test-baseline",
            omni_home=str(tmp_path),
            current_snapshot=current,
            dry_run=True,
        )
        result = await handler.handle(request)

        gh_delta = result.delta.per_probe_deltas.get("github_prs")
        assert gh_delta is not None
        assert len(gh_delta.opened) == 0
        assert len(gh_delta.merged) == 0
        assert len(gh_delta.closed) == 0

    async def test_writes_delta_artifact(self, tmp_path: Path) -> None:
        """Non-dry-run -> delta JSON written to disk, deserializable."""
        baseline = _make_baseline(probes={})
        _write_baseline(tmp_path, baseline)

        current = _make_baseline(
            baseline_id="test-baseline__current",
            captured_at=_NOW,
            probes={},
        )

        handler = HandlerBaselineCompare()
        request = ModelBaselineCompareRequest(
            baseline_id="test-baseline",
            omni_home=str(tmp_path),
            current_snapshot=current,
            dry_run=False,
        )
        result = await handler.handle(request)

        delta_path = Path(result.report_path)
        assert delta_path.exists()
        raw = json.loads(delta_path.read_text())
        assert raw["baseline_id"] == "test-baseline"

    async def test_dry_run_does_not_write_artifact(self, tmp_path: Path) -> None:
        """dry_run=True -> no delta file written."""
        baseline = _make_baseline(probes={})
        _write_baseline(tmp_path, baseline)

        current = _make_baseline(
            baseline_id="test-baseline__current",
            captured_at=_NOW,
            probes={},
        )

        handler = HandlerBaselineCompare()
        request = ModelBaselineCompareRequest(
            baseline_id="test-baseline",
            omni_home=str(tmp_path),
            current_snapshot=current,
            dry_run=True,
        )
        result = await handler.handle(request)

        delta_path = Path(result.report_path)
        assert not delta_path.exists()
        assert result.dry_run is True

    async def test_re_runs_probes_when_no_current_snapshot(
        self, tmp_path: Path
    ) -> None:
        """Without current_snapshot, handler calls capture handler to get current state."""
        baseline = _make_baseline(probes={"github_prs": []})
        _write_baseline(tmp_path, baseline)

        # Mock the capture handler to return a known current snapshot
        current_snap = _make_baseline(
            baseline_id="test-baseline__current",
            captured_at=_NOW,
            probes={"github_prs": []},
        )

        from omnimarket.nodes.node_baseline_capture.handlers import (
            handler_baseline_capture,
        )
        from omnimarket.nodes.node_baseline_capture.handlers.handler_baseline_capture import (
            ModelBaselineCaptureResult,
        )

        mock_result = MagicMock(spec=ModelBaselineCaptureResult)
        mock_result.snapshot = current_snap

        with patch.object(
            handler_baseline_capture.HandlerBaselineCapture,
            "handle",
            AsyncMock(return_value=mock_result),
        ):
            handler = HandlerBaselineCompare()
            request = ModelBaselineCompareRequest(
                baseline_id="test-baseline",
                omni_home=str(tmp_path),
                dry_run=True,
            )
            result = await handler.handle(request)

        assert result.error is None
        assert result.delta is not None
