# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for compute_triage_state — covers all 8 triage states.

Verifies the deterministic state machine defined in
handler_github_api_poll.py. No external services are called.

Run with:
    uv run pytest tests/unit/nodes/node_github_pr_poller_effect/ -m unit -v

Related Tickets:
    - OMN-2656: Phase 2 — Effect Nodes & CLIs (omnibase_infra)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.nodes.node_github_pr_poller_effect.handlers.handler_github_api_poll import (
    HandlerGitHubApiPoll,
    compute_triage_state,
)
from omnimarket.nodes.node_github_pr_poller_effect.models.model_github_poller_config import (
    ModelGitHubPollerConfig,
)


def _pr(
    *,
    draft: bool = False,
    labels: list[str] | None = None,
    updated_hours_ago: int = 1,
    combined_status: str = "pending",
    review_states: list[str] | None = None,
) -> dict[object, object]:
    """Helper to build a minimal GitHub PR payload dict."""
    updated_at = (datetime.now(tz=UTC) - timedelta(hours=updated_hours_ago)).isoformat()
    return {
        "draft": draft,
        "labels": [{"name": lbl} for lbl in (labels or [])],
        "updated_at": updated_at,
        "combined_status": combined_status,
        "review_states": review_states or [],
        "head": {"sha": "abc123"},
        "number": 42,
        "title": "Test PR",
    }


class TestTriageStateDraft:
    @pytest.mark.unit
    def test_draft_pr_returns_draft(self) -> None:
        pr = _pr(draft=True)
        assert compute_triage_state(pr) == "draft"

    @pytest.mark.unit
    def test_draft_pr_ignores_other_signals(self) -> None:
        """Draft takes precedence over blocking labels, stale, and CI."""
        pr = _pr(
            draft=True,
            labels=["blocked"],
            updated_hours_ago=100,
            combined_status="failure",
        )
        assert compute_triage_state(pr) == "draft"


class TestTriageStateBlocked:
    @pytest.mark.unit
    def test_blocked_label(self) -> None:
        pr = _pr(labels=["blocked"])
        assert compute_triage_state(pr) == "blocked"

    @pytest.mark.unit
    def test_do_not_merge_label(self) -> None:
        pr = _pr(labels=["do-not-merge"])
        assert compute_triage_state(pr) == "blocked"

    @pytest.mark.unit
    def test_wip_label(self) -> None:
        pr = _pr(labels=["wip"])
        assert compute_triage_state(pr) == "blocked"

    @pytest.mark.unit
    def test_blocked_label_case_insensitive(self) -> None:
        pr = _pr(labels=["WIP"])
        assert compute_triage_state(pr) == "blocked"

    @pytest.mark.unit
    def test_non_blocking_label_does_not_block(self) -> None:
        pr = _pr(labels=["enhancement"], combined_status="pending")
        # No CI failure, no reviews, recent — falls through to needs_review
        assert compute_triage_state(pr) == "needs_review"


class TestTriageStateStale:
    @pytest.mark.unit
    def test_stale_default_threshold_48h(self) -> None:
        pr = _pr(updated_hours_ago=49)
        assert compute_triage_state(pr) == "stale"

    @pytest.mark.unit
    def test_not_stale_at_47h(self) -> None:
        pr = _pr(updated_hours_ago=47, combined_status="pending")
        # Not stale, no CI failure, no reviews — needs_review
        assert compute_triage_state(pr) != "stale"

    @pytest.mark.unit
    def test_stale_custom_threshold(self) -> None:
        pr = _pr(updated_hours_ago=25)
        assert compute_triage_state(pr, stale_hours=24) == "stale"

    @pytest.mark.unit
    def test_stale_skips_on_invalid_date(self) -> None:
        """Invalid updated_at string should not cause stale classification."""
        pr: dict[object, object] = {
            "draft": False,
            "labels": [],
            "updated_at": "not-a-date",
            "combined_status": "pending",
            "review_states": [],
        }
        result = compute_triage_state(pr)
        assert result != "stale"


class TestTriageStateCiFailing:
    @pytest.mark.unit
    def test_ci_failure_returns_ci_failing(self) -> None:
        pr = _pr(combined_status="failure")
        assert compute_triage_state(pr) == "ci_failing"

    @pytest.mark.unit
    def test_ci_failure_despite_approval(self) -> None:
        """CI failure takes precedence over approval."""
        pr = _pr(combined_status="failure", review_states=["APPROVED"])
        assert compute_triage_state(pr) == "ci_failing"


class TestTriageStateChangesRequested:
    @pytest.mark.unit
    def test_changes_requested(self) -> None:
        pr = _pr(combined_status="pending", review_states=["CHANGES_REQUESTED"])
        assert compute_triage_state(pr) == "changes_requested"

    @pytest.mark.unit
    def test_changes_requested_with_approval(self) -> None:
        """changes_requested takes precedence over approval per evaluation order."""
        pr = _pr(
            combined_status="pending",
            review_states=["APPROVED", "CHANGES_REQUESTED"],
        )
        assert compute_triage_state(pr) == "changes_requested"


class TestTriageStateReadyToMerge:
    @pytest.mark.unit
    def test_ready_to_merge_ci_green_and_approved(self) -> None:
        pr = _pr(combined_status="success", review_states=["APPROVED"])
        assert compute_triage_state(pr) == "ready_to_merge"

    @pytest.mark.unit
    def test_ready_to_merge_multiple_approvals(self) -> None:
        pr = _pr(combined_status="success", review_states=["APPROVED", "APPROVED"])
        assert compute_triage_state(pr) == "ready_to_merge"

    @pytest.mark.unit
    def test_not_ready_to_merge_without_ci(self) -> None:
        pr = _pr(combined_status="pending", review_states=["APPROVED"])
        assert compute_triage_state(pr) == "approved_pending_ci"


class TestTriageStateApprovedPendingCi:
    @pytest.mark.unit
    def test_approved_pending_ci_when_ci_pending(self) -> None:
        pr = _pr(combined_status="pending", review_states=["APPROVED"])
        assert compute_triage_state(pr) == "approved_pending_ci"

    @pytest.mark.unit
    def test_approved_pending_ci_when_no_status(self) -> None:
        pr: dict[object, object] = {
            "draft": False,
            "labels": [],
            "updated_at": datetime.now(tz=UTC).isoformat(),
            "review_states": ["APPROVED"],
            # combined_status absent — defaults to "pending" in handler
        }
        assert compute_triage_state(pr) == "approved_pending_ci"


class TestTriageStateNeedsReview:
    @pytest.mark.unit
    def test_needs_review_no_reviews_pending_ci(self) -> None:
        pr = _pr(combined_status="pending", review_states=[])
        assert compute_triage_state(pr) == "needs_review"

    @pytest.mark.unit
    def test_needs_review_fallback(self) -> None:
        """Clean PR with no reviews and pending CI falls to needs_review."""
        pr = _pr()
        assert compute_triage_state(pr) == "needs_review"


class TestTriageStatePrecedenceOrder:
    """Verify the evaluation order: draft > blocked > stale > ci_failing >
    changes_requested > ready_to_merge > approved_pending_ci > needs_review."""

    @pytest.mark.unit
    def test_draft_beats_blocked(self) -> None:
        pr = _pr(draft=True, labels=["blocked"])
        assert compute_triage_state(pr) == "draft"

    @pytest.mark.unit
    def test_blocked_beats_stale(self) -> None:
        pr = _pr(labels=["blocked"], updated_hours_ago=100)
        assert compute_triage_state(pr) == "blocked"

    @pytest.mark.unit
    def test_stale_beats_ci_failing(self) -> None:
        pr = _pr(updated_hours_ago=100, combined_status="failure")
        assert compute_triage_state(pr) == "stale"

    @pytest.mark.unit
    def test_ci_failing_beats_changes_requested(self) -> None:
        pr = _pr(combined_status="failure", review_states=["CHANGES_REQUESTED"])
        assert compute_triage_state(pr) == "ci_failing"

    @pytest.mark.unit
    def test_all_8_states_reachable(self) -> None:
        """Smoke test: assert all 8 states can be produced."""
        states = {
            compute_triage_state(_pr(draft=True)),
            compute_triage_state(_pr(labels=["blocked"])),
            compute_triage_state(_pr(updated_hours_ago=100)),
            compute_triage_state(_pr(combined_status="failure")),
            compute_triage_state(_pr(review_states=["CHANGES_REQUESTED"])),
            compute_triage_state(
                _pr(combined_status="success", review_states=["APPROVED"])
            ),
            compute_triage_state(
                _pr(combined_status="pending", review_states=["APPROVED"])
            ),
            compute_triage_state(_pr()),
        }
        expected = {
            "draft",
            "blocked",
            "stale",
            "ci_failing",
            "changes_requested",
            "ready_to_merge",
            "approved_pending_ci",
            "needs_review",
        }
        assert states == expected


@pytest.mark.asyncio
@pytest.mark.unit
async def test_handler_runtime_tick_uses_contract_config_without_polling() -> None:
    """Runtime tick inputs resolve to the handler's configured poller settings."""
    now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
    tick = ModelRuntimeTick(
        now=now,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=now,
        correlation_id=uuid4(),
        scheduler_id="test-scheduler",
        tick_interval_ms=1000,
    )
    handler = HandlerGitHubApiPoll(config=ModelGitHubPollerConfig(repos=[]))

    result = await handler.handle(tick)

    assert result.repos_polled == []
    assert result.prs_polled == 0
    assert result.pending_events == []
    assert result.errors == []
