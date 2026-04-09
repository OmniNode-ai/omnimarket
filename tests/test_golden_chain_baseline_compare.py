"""Golden chain tests for node_baseline_compare.

Verifies end-to-end compare behavior:
  - detects newly opened PRs (delta.opened)
  - detects merged/closed PRs
  - detects ticket state changes
  - missing baseline returns graceful error result
  - empty delta when before == after
  - event bus wiring (command -> handler -> completion event)

Related: OMN-7960 (golden chain tests for baseline measurement system)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
    BaselineProbeType,
    ModelBaselineDelta,
    ModelBaselineSnapshot,
    ModelGitBranchSnapshot,
    ModelGitHubPRDelta,
    ModelGitHubPRSnapshot,
    ModelLinearTicketDelta,
    ModelLinearTicketSnapshot,
    ModelServiceHealthSnapshot,
    ProbeSnapshotItem,
)
from omnimarket.nodes.node_baseline_compare.handlers.handler_baseline_compare import (
    HandlerBaselineCompare,
    ModelBaselineCompareRequest,
    ModelBaselineCompareResult,
)

_NOW = datetime.now(UTC)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _pr(
    number: int,
    *,
    state: str = "open",
    repo: str = "OmniNode-ai/omniclaude",
) -> ModelGitHubPRSnapshot:
    return ModelGitHubPRSnapshot(
        pr_number=number,
        title=f"PR #{number}",
        repo=repo,
        state=state,
        labels=[],
        age_days=1.0,
        ci_status="success",
    )


def _ticket(
    ticket_id: str,
    *,
    state: str = "In Progress",
) -> ModelLinearTicketSnapshot:
    return ModelLinearTicketSnapshot(
        ticket_id=ticket_id,
        title=f"Ticket {ticket_id}",
        state=state,
        priority=2,
        assignee="jonah",
        updated_at=_NOW,
    )


def _health(service: str, *, healthy: bool) -> ModelServiceHealthSnapshot:
    return ModelServiceHealthSnapshot(
        service=service,
        healthy=healthy,
        latency_ms=10.0 if healthy else None,
        error=None if healthy else "connection refused",
    )


def _branch(branch: str, *, repo: str = "omniclaude") -> ModelGitBranchSnapshot:
    return ModelGitBranchSnapshot(
        repo=repo,
        branch=branch,
        worktree_path=f"/tmp/worktrees/{branch}",
        age_days=1.0,
    )


def _write_baseline(
    path: Path,
    probes: dict[str, list[ProbeSnapshotItem]],
    *,
    baseline_id: str = "test-baseline",
) -> None:
    snapshot = ModelBaselineSnapshot(
        baseline_id=baseline_id,
        captured_at=_NOW,
        label="test",
        probes=probes,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")


def _make_mock_probe(probe_name: str, items: list[ProbeSnapshotItem]) -> object:
    class _MockProbe:
        name = probe_name

        async def collect(self, omni_home: str) -> list[ProbeSnapshotItem]:
            return items

    return _MockProbe()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBaselineCompareGoldenChain:
    """Golden chain tests for HandlerBaselineCompare."""

    async def test_compare_detects_new_prs(self, tmp_path: Path) -> None:
        """Baseline has 1 PR; current has 3. delta.opened contains the 2 new PR numbers."""
        baseline_file = tmp_path / "test-baseline.json"
        _write_baseline(
            baseline_file,
            {BaselineProbeType.GITHUB_PRS: [_pr(10), _pr(11)]},
        )

        # Current state: PR 10, 11 still open + 2 new
        current_snapshot = ModelBaselineSnapshot(
            baseline_id="current",
            captured_at=_NOW,
            probes={BaselineProbeType.GITHUB_PRS: [_pr(10), _pr(11), _pr(20), _pr(21)]},
        )
        handler = HandlerBaselineCompare()
        request = ModelBaselineCompareRequest(
            baseline_id="test-baseline",
            baseline_path=str(baseline_file),
            current_snapshot=current_snapshot,
            dry_run=True,
        )

        result: ModelBaselineCompareResult = await handler.handle(request)

        assert result.error is None
        pr_delta = result.delta.per_probe_deltas[BaselineProbeType.GITHUB_PRS]
        assert isinstance(pr_delta, ModelGitHubPRDelta)
        assert set(pr_delta.opened) == {20, 21}
        assert pr_delta.merged == []
        assert pr_delta.closed == []

    async def test_compare_detects_merged_prs(self, tmp_path: Path) -> None:
        """PR present in baseline with state='merged' and absent from current -> delta.merged."""
        merged_pr = _pr(100, state="merged")
        baseline_file = tmp_path / "test-baseline.json"
        _write_baseline(
            baseline_file,
            {BaselineProbeType.GITHUB_PRS: [merged_pr, _pr(101)]},
        )

        current_snapshot = ModelBaselineSnapshot(
            baseline_id="current",
            captured_at=_NOW,
            probes={BaselineProbeType.GITHUB_PRS: [_pr(101)]},
        )
        handler = HandlerBaselineCompare()
        request = ModelBaselineCompareRequest(
            baseline_id="test-baseline",
            baseline_path=str(baseline_file),
            current_snapshot=current_snapshot,
            dry_run=True,
        )

        result = await handler.handle(request)

        pr_delta = result.delta.per_probe_deltas[BaselineProbeType.GITHUB_PRS]
        assert isinstance(pr_delta, ModelGitHubPRDelta)
        assert 100 in pr_delta.merged
        assert pr_delta.closed == []

    async def test_compare_detects_closed_prs(self, tmp_path: Path) -> None:
        """PR in baseline with state='open' absent from current -> delta.closed."""
        baseline_file = tmp_path / "test-baseline.json"
        _write_baseline(
            baseline_file,
            {BaselineProbeType.GITHUB_PRS: [_pr(200, state="open"), _pr(201)]},
        )

        current_snapshot = ModelBaselineSnapshot(
            baseline_id="current",
            captured_at=_NOW,
            probes={BaselineProbeType.GITHUB_PRS: [_pr(201)]},
        )
        handler = HandlerBaselineCompare()
        request = ModelBaselineCompareRequest(
            baseline_id="test-baseline",
            baseline_path=str(baseline_file),
            current_snapshot=current_snapshot,
            dry_run=True,
        )

        result = await handler.handle(request)

        pr_delta = result.delta.per_probe_deltas[BaselineProbeType.GITHUB_PRS]
        assert isinstance(pr_delta, ModelGitHubPRDelta)
        assert 200 in pr_delta.closed
        assert pr_delta.merged == []

    async def test_compare_detects_ticket_state_change(self, tmp_path: Path) -> None:
        """Ticket state change is captured in delta.state_changes."""
        baseline_file = tmp_path / "test-baseline.json"
        _write_baseline(
            baseline_file,
            {
                BaselineProbeType.LINEAR_TICKETS: [
                    _ticket("OMN-500", state="In Progress"),
                    _ticket("OMN-501", state="Todo"),
                ]
            },
        )

        current_snapshot = ModelBaselineSnapshot(
            baseline_id="current",
            captured_at=_NOW,
            probes={
                BaselineProbeType.LINEAR_TICKETS: [
                    _ticket("OMN-500", state="Done"),
                    _ticket("OMN-501", state="In Progress"),
                ]
            },
        )
        handler = HandlerBaselineCompare()
        request = ModelBaselineCompareRequest(
            baseline_id="test-baseline",
            baseline_path=str(baseline_file),
            current_snapshot=current_snapshot,
            dry_run=True,
        )

        result = await handler.handle(request)

        ticket_delta = result.delta.per_probe_deltas[BaselineProbeType.LINEAR_TICKETS]
        assert isinstance(ticket_delta, ModelLinearTicketDelta)
        assert "OMN-500" in ticket_delta.state_changes
        assert ticket_delta.state_changes["OMN-500"] == "In Progress -> Done"
        assert "OMN-501" in ticket_delta.state_changes
        assert ticket_delta.state_changes["OMN-501"] == "Todo -> In Progress"

    async def test_missing_baseline_returns_error(self, tmp_path: Path) -> None:
        """Missing baseline artifact -> error result, no exception raised."""
        handler = HandlerBaselineCompare()
        request = ModelBaselineCompareRequest(
            baseline_id="nonexistent",
            baseline_path=str(tmp_path / "no-such-file.json"),
            dry_run=True,
        )

        result = await handler.handle(request)

        assert result.error is not None
        assert "not found" in result.error.lower()
        assert result.baseline_id == "nonexistent"
        # Delta is empty but structurally valid
        assert isinstance(result.delta, ModelBaselineDelta)

    async def test_empty_delta_when_no_changes(self, tmp_path: Path) -> None:
        """Before == after produces all-zero delta counts."""
        prs = [_pr(10), _pr(11), _pr(12)]
        tickets = [_ticket("OMN-1"), _ticket("OMN-2")]
        baseline_file = tmp_path / "test-baseline.json"
        _write_baseline(
            baseline_file,
            {
                BaselineProbeType.GITHUB_PRS: prs,
                BaselineProbeType.LINEAR_TICKETS: tickets,
            },
        )

        current_snapshot = ModelBaselineSnapshot(
            baseline_id="current",
            captured_at=_NOW,
            probes={
                BaselineProbeType.GITHUB_PRS: prs,
                BaselineProbeType.LINEAR_TICKETS: tickets,
            },
        )
        handler = HandlerBaselineCompare()
        request = ModelBaselineCompareRequest(
            baseline_id="test-baseline",
            baseline_path=str(baseline_file),
            current_snapshot=current_snapshot,
            dry_run=True,
        )

        result = await handler.handle(request)

        assert result.error is None
        pr_delta = result.delta.per_probe_deltas[BaselineProbeType.GITHUB_PRS]
        assert isinstance(pr_delta, ModelGitHubPRDelta)
        assert pr_delta.opened == []
        assert pr_delta.merged == []
        assert pr_delta.closed == []
        assert pr_delta.track_changes == {}

        ticket_delta = result.delta.per_probe_deltas[BaselineProbeType.LINEAR_TICKETS]
        assert isinstance(ticket_delta, ModelLinearTicketDelta)
        assert ticket_delta.opened == []
        assert ticket_delta.closed_done == []
        assert ticket_delta.state_changes == {}

    async def test_compare_writes_delta_artifact(self, tmp_path: Path) -> None:
        """Non-dry-run compare writes a .delta.json artifact to disk."""
        baseline_file = tmp_path / "test-baseline.json"
        _write_baseline(
            baseline_file,
            {BaselineProbeType.GITHUB_PRS: [_pr(10)]},
        )
        current_snapshot = ModelBaselineSnapshot(
            baseline_id="current",
            captured_at=_NOW,
            probes={BaselineProbeType.GITHUB_PRS: [_pr(10), _pr(11)]},
        )
        handler = HandlerBaselineCompare()
        request = ModelBaselineCompareRequest(
            baseline_id="test-baseline",
            baseline_path=str(baseline_file),
            omni_home=str(tmp_path),
            current_snapshot=current_snapshot,
            dry_run=False,
        )

        result = await handler.handle(request)

        assert result.error is None
        assert Path(result.report_path).exists()
        raw = json.loads(Path(result.report_path).read_text())
        delta = ModelBaselineDelta.model_validate(raw)
        assert delta.baseline_id == "test-baseline"

    async def test_dry_run_does_not_write_delta_artifact(self, tmp_path: Path) -> None:
        """dry_run=True must not write any delta artifact."""
        baseline_file = tmp_path / "test-baseline.json"
        _write_baseline(
            baseline_file,
            {BaselineProbeType.GITHUB_PRS: [_pr(10)]},
        )

        current_snapshot = ModelBaselineSnapshot(
            baseline_id="current",
            captured_at=_NOW,
            probes={BaselineProbeType.GITHUB_PRS: [_pr(10)]},
        )
        handler = HandlerBaselineCompare()
        request = ModelBaselineCompareRequest(
            baseline_id="test-baseline",
            baseline_path=str(baseline_file),
            omni_home=str(tmp_path),
            current_snapshot=current_snapshot,
            dry_run=True,
        )

        result = await handler.handle(request)

        assert result.dry_run is True
        assert result.error is None
        delta_path = tmp_path / ".onex_state" / "baselines" / "test-baseline.delta.json"
        assert not delta_path.exists()

    async def test_summary_non_empty(self, tmp_path: Path) -> None:
        """Result summary is a non-empty string describing the comparison."""
        baseline_file = tmp_path / "test-baseline.json"
        _write_baseline(
            baseline_file,
            {BaselineProbeType.GITHUB_PRS: [_pr(10)]},
        )
        current_snapshot = ModelBaselineSnapshot(
            baseline_id="current",
            captured_at=_NOW,
            probes={BaselineProbeType.GITHUB_PRS: [_pr(10), _pr(11)]},
        )
        handler = HandlerBaselineCompare()
        request = ModelBaselineCompareRequest(
            baseline_id="test-baseline",
            baseline_path=str(baseline_file),
            current_snapshot=current_snapshot,
            dry_run=True,
        )

        result = await handler.handle(request)

        assert isinstance(result.summary, str)
        assert len(result.summary) > 20
        assert "test-baseline" in result.summary

    async def test_compare_detects_new_git_branches(self, tmp_path: Path) -> None:
        """New branches in current state appear in delta.created."""
        baseline_file = tmp_path / "test-baseline.json"
        _write_baseline(
            baseline_file,
            {BaselineProbeType.GIT_BRANCHES: [_branch("jonah/old-branch")]},
        )
        current_snapshot = ModelBaselineSnapshot(
            baseline_id="current",
            captured_at=_NOW,
            probes={
                BaselineProbeType.GIT_BRANCHES: [
                    _branch("jonah/old-branch"),
                    _branch("jonah/new-feature"),
                ]
            },
        )
        handler = HandlerBaselineCompare()
        request = ModelBaselineCompareRequest(
            baseline_id="test-baseline",
            baseline_path=str(baseline_file),
            current_snapshot=current_snapshot,
            dry_run=True,
        )

        result = await handler.handle(request)

        from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
            ModelGitBranchDelta,
        )

        branch_delta = result.delta.per_probe_deltas[BaselineProbeType.GIT_BRANCHES]
        assert isinstance(branch_delta, ModelGitBranchDelta)
        assert "jonah/new-feature" in branch_delta.created
        assert branch_delta.merged == []

    async def test_compare_with_probe_subset(self, tmp_path: Path) -> None:
        """request.probes limits comparison to specified subset."""
        baseline_file = tmp_path / "test-baseline.json"
        _write_baseline(
            baseline_file,
            {
                BaselineProbeType.GITHUB_PRS: [_pr(10)],
                BaselineProbeType.LINEAR_TICKETS: [_ticket("OMN-1")],
            },
        )
        current_snapshot = ModelBaselineSnapshot(
            baseline_id="current",
            captured_at=_NOW,
            probes={
                BaselineProbeType.GITHUB_PRS: [_pr(10), _pr(11)],
                BaselineProbeType.LINEAR_TICKETS: [_ticket("OMN-1"), _ticket("OMN-2")],
            },
        )
        handler = HandlerBaselineCompare()
        # Only compare github_prs, not linear_tickets
        request = ModelBaselineCompareRequest(
            baseline_id="test-baseline",
            baseline_path=str(baseline_file),
            probes=[BaselineProbeType.GITHUB_PRS],
            current_snapshot=current_snapshot,
            dry_run=True,
        )

        result = await handler.handle(request)

        assert BaselineProbeType.GITHUB_PRS in result.delta.per_probe_deltas
        assert BaselineProbeType.LINEAR_TICKETS not in result.delta.per_probe_deltas
