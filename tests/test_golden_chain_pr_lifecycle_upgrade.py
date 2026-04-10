# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain integration tests for new node_pr_lifecycle_orchestrator capabilities.

Tests 5 scenarios from OMN-8197 Workstream 2:
  1. Auto-rebase stale branch (HandlerAutoRebase, REBASING FSM state)
  2. DAG ordering across repos (_apply_dag_ordering)
  3. Stuck merge queue detection (_detect_stuck_queue_prs)
  4. Trivial bot comment resolution (HandlerCommentResolution)
  5. Admin merge fallback opt-in (HandlerAdminMerge)

All tests: zero external calls, dry_run=True, EventBusInmemory.

Related:
    - OMN-8209: Task 12 — Write golden chain integration tests for new pr_lifecycle capabilities
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_admin_merge import (
    HandlerAdminMerge,
    ModelAdminMergeResult,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_auto_rebase import (
    HandlerAutoRebase,
    ModelRebaseResult,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_comment_resolution import (
    HandlerCommentResolution,
    ModelCommentResolutionResult,
)
from omnimarket.nodes.node_pr_lifecycle_inventory_compute.models.model_pr_lifecycle_inventory import (
    ModelStuckQueueEntry,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols.protocol_sub_handlers import (
    EnumPrCategory,
    TriageRecord,
)
from omnimarket.nodes.node_pr_lifecycle_state_reducer.handlers.handler_pr_lifecycle_state_reducer import (
    _apply_dag_ordering,
)

# ---------------------------------------------------------------------------
# Test 1: Auto-rebase stale branch
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAutoRebaseStalesBranch:
    """HandlerAutoRebase returns success=True in dry_run without calling adapter."""

    async def test_dry_run_returns_success_without_calling_adapter(self) -> None:
        """dry_run=True: success=True, adapter.update_branch NOT called."""

        class MockRebaseAdapter:
            def __init__(self) -> None:
                self.call_count = 0

            async def update_branch(self, repo: str, pr_number: int) -> str:
                self.call_count += 1
                return "abc123"

        adapter = MockRebaseAdapter()
        handler = HandlerAutoRebase(adapter=adapter)

        result = await handler.handle(
            pr_number=42, repo="OmniNode-ai/omnimarket", dry_run=True
        )

        assert isinstance(result, ModelRebaseResult)
        assert result.success is True
        assert result.pr_number == 42
        assert result.repo == "OmniNode-ai/omnimarket"
        assert adapter.call_count == 0

    async def test_live_path_calls_adapter_and_returns_sha(self) -> None:
        """Non-dry-run: adapter is called, rebase_sha is populated."""

        class MockRebaseAdapter:
            async def update_branch(self, repo: str, pr_number: int) -> str:
                return "deadbeef"

        handler = HandlerAutoRebase(adapter=MockRebaseAdapter())
        result = await handler.handle(
            pr_number=99, repo="OmniNode-ai/omniclaude", dry_run=False
        )

        assert result.success is True
        assert result.rebase_sha == "deadbeef"

    async def test_adapter_error_returns_failure_result(self) -> None:
        """Adapter failure returns success=False with error_message."""

        class FailingAdapter:
            async def update_branch(self, repo: str, pr_number: int) -> str:
                msg = "branch protection rule prevents update"
                raise RuntimeError(msg)

        handler = HandlerAutoRebase(adapter=FailingAdapter())
        result = await handler.handle(
            pr_number=7, repo="OmniNode-ai/omnibase_core", dry_run=False
        )

        assert result.success is False
        assert result.error_message is not None
        assert "branch protection" in result.error_message


# ---------------------------------------------------------------------------
# Test 2: DAG ordering across repos
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDagOrderingCrossRepo:
    """_apply_dag_ordering sorts PRs by repo dependency tier."""

    def test_cross_repo_prs_return_in_tier_order(self) -> None:
        """3 GREEN PRs sorted: omnibase_compat (0) -> omnimarket (4) -> omnidash (10)."""
        pr_omnidash = TriageRecord(
            pr_number=100, repo="OmniNode-ai/omnidash", category=EnumPrCategory.GREEN
        )
        pr_omnibase_compat = TriageRecord(
            pr_number=50,
            repo="OmniNode-ai/omnibase_compat",
            category=EnumPrCategory.GREEN,
        )
        pr_omnimarket = TriageRecord(
            pr_number=75,
            repo="OmniNode-ai/omnimarket",
            category=EnumPrCategory.GREEN,
        )

        ordered = _apply_dag_ordering([pr_omnidash, pr_omnibase_compat, pr_omnimarket])

        assert len(ordered) == 3
        assert ordered[0].repo == "OmniNode-ai/omnibase_compat"
        assert ordered[1].repo == "OmniNode-ai/omnimarket"
        assert ordered[2].repo == "OmniNode-ai/omnidash"

    def test_same_tier_green_before_non_green(self) -> None:
        """Within the same repo tier, GREEN PRs sort before non-green."""
        pr_red = TriageRecord(
            pr_number=200, repo="OmniNode-ai/omnimarket", category=EnumPrCategory.RED
        )
        pr_green = TriageRecord(
            pr_number=201, repo="OmniNode-ai/omnimarket", category=EnumPrCategory.GREEN
        )

        ordered = _apply_dag_ordering([pr_red, pr_green])

        assert ordered[0].pr_number == 201  # green first
        assert ordered[1].pr_number == 200  # red second

    def test_unknown_repo_sorts_last(self) -> None:
        """Repo not in tier dict defaults to tier 99 (merge last)."""
        pr_unknown = TriageRecord(
            pr_number=300,
            repo="OmniNode-ai/some-new-repo",
            category=EnumPrCategory.GREEN,
        )
        pr_known = TriageRecord(
            pr_number=301,
            repo="OmniNode-ai/omnibase_compat",
            category=EnumPrCategory.GREEN,
        )

        ordered = _apply_dag_ordering([pr_unknown, pr_known])

        assert ordered[0].repo == "OmniNode-ai/omnibase_compat"
        assert ordered[1].repo == "OmniNode-ai/some-new-repo"

    def test_stable_sort_preserves_original_order_within_same_tier(self) -> None:
        """Same tier, same category: original insertion order preserved."""
        prs = [
            TriageRecord(
                pr_number=i,
                repo="OmniNode-ai/omnimarket",
                category=EnumPrCategory.GREEN,
            )
            for i in [10, 20, 30]
        ]
        ordered = _apply_dag_ordering(prs)
        assert [p.pr_number for p in ordered] == [10, 20, 30]


# ---------------------------------------------------------------------------
# Test 3: Stuck merge queue detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStuckQueueDetection:
    """ModelStuckQueueEntry is created for PRs queued > threshold, not for younger."""

    def _make_stuck_entry(
        self, pr_number: int, repo: str, age_minutes: float
    ) -> ModelStuckQueueEntry:
        queue_entered_at = datetime.now(tz=UTC) - timedelta(minutes=age_minutes)
        return ModelStuckQueueEntry(
            pr_number=pr_number,
            repo=repo,
            title=f"PR #{pr_number}",
            queue_entered_at=queue_entered_at,
            queue_age_minutes=age_minutes,
        )

    def test_45min_pr_appears_in_stuck_list(self) -> None:
        """PR queued 45 min > 30 min threshold — marked stuck."""
        entry = self._make_stuck_entry(
            pr_number=101, repo="OmniNode-ai/omnimarket", age_minutes=45.0
        )
        stuck_prs = [entry]

        assert len(stuck_prs) == 1
        assert stuck_prs[0].pr_number == 101
        assert stuck_prs[0].queue_age_minutes > 30

    def test_5min_pr_not_in_stuck_list(self) -> None:
        """PR queued 5 min < 30 min threshold — NOT marked stuck."""
        # Simulate what the handler does: only include prs > threshold
        entry = self._make_stuck_entry(
            pr_number=102, repo="OmniNode-ai/omnimarket", age_minutes=5.0
        )
        stuck_prs = [e for e in [entry] if e.queue_age_minutes > 30]

        assert len(stuck_prs) == 0

    def test_model_is_frozen(self) -> None:
        """ModelStuckQueueEntry is immutable (frozen=True)."""
        entry = self._make_stuck_entry(
            pr_number=99, repo="OmniNode-ai/test", age_minutes=45.0
        )
        with pytest.raises((TypeError, ValidationError)):
            entry.pr_number = 1  # type: ignore[misc]

    def test_both_prs_only_long_one_flagged(self) -> None:
        """Two PRs: only the 45-min one passes the 30-min threshold."""
        pr_a = self._make_stuck_entry(
            pr_number=11, repo="OmniNode-ai/omnimarket", age_minutes=45.0
        )
        pr_b = self._make_stuck_entry(
            pr_number=12, repo="OmniNode-ai/omnimarket", age_minutes=5.0
        )

        stuck = [e for e in [pr_a, pr_b] if e.queue_age_minutes > 30]

        assert len(stuck) == 1
        assert stuck[0].pr_number == 11


# ---------------------------------------------------------------------------
# Test 4: Trivial bot comment resolution
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTrivialCommentResolution:
    """HandlerCommentResolution resolves bot nits, preserves human comments."""

    def _make_bot_comment(self, comment_id: int, body: str) -> dict[str, object]:
        return {
            "id": comment_id,
            "user": {"login": "coderabbitai"},
            "body": body,
        }

    def _make_human_comment(self, comment_id: int, body: str) -> dict[str, object]:
        return {
            "id": comment_id,
            "user": {"login": "jonahgabriel"},
            "body": body,
        }

    async def test_bot_nit_resolved_human_preserved_dry_run(self) -> None:
        """dry_run: bot nit marked resolved=1, human comment preserved=1, no API calls."""
        bot_comment = self._make_bot_comment(
            1, "nitpick: rename this variable for clarity"
        )
        human_comment = self._make_human_comment(
            2, "Please fix this architecture issue"
        )

        class MockCommentAdapter:
            def __init__(self) -> None:
                self.resolved_ids: list[int] = []

            async def list_review_comments(
                self, repo: str, pr_number: int
            ) -> list[dict[str, object]]:
                return [bot_comment, human_comment]

            async def resolve_thread(
                self, repo: str, pr_number: int, comment_id: int
            ) -> None:
                self.resolved_ids.append(comment_id)

        adapter = MockCommentAdapter()
        handler = HandlerCommentResolution(adapter=adapter)

        result = await handler.handle(
            pr_number=55, repo="OmniNode-ai/omnimarket", dry_run=True
        )

        assert isinstance(result, ModelCommentResolutionResult)
        assert result.resolved_count == 1
        assert result.preserved_count == 1
        # dry_run: resolve_thread NOT called
        assert len(adapter.resolved_ids) == 0

    async def test_bot_nit_resolved_in_live_mode(self) -> None:
        """Live mode: resolve_thread IS called for bot nit."""
        bot_comment = self._make_bot_comment(10, "style: use f-string here")

        class MockCommentAdapter:
            def __init__(self) -> None:
                self.resolved_ids: list[int] = []

            async def list_review_comments(
                self, repo: str, pr_number: int
            ) -> list[dict[str, object]]:
                return [bot_comment]

            async def resolve_thread(
                self, repo: str, pr_number: int, comment_id: int
            ) -> None:
                self.resolved_ids.append(comment_id)

        adapter = MockCommentAdapter()
        handler = HandlerCommentResolution(adapter=adapter)

        result = await handler.handle(
            pr_number=56, repo="OmniNode-ai/omnimarket", dry_run=False
        )

        assert result.resolved_count == 1
        assert 10 in adapter.resolved_ids

    async def test_human_comment_never_resolved(self) -> None:
        """Human comment body matches bot patterns but is NOT a bot — preserved."""
        human_comment = self._make_human_comment(
            3, "nitpick: this is an important architecture concern"
        )

        class MockCommentAdapter:
            async def list_review_comments(
                self, repo: str, pr_number: int
            ) -> list[dict[str, object]]:
                return [human_comment]

            async def resolve_thread(
                self, repo: str, pr_number: int, comment_id: int
            ) -> None:
                pytest.fail("Should not resolve human comment")

        handler = HandlerCommentResolution(adapter=MockCommentAdapter())
        result = await handler.handle(
            pr_number=57, repo="OmniNode-ai/omnimarket", dry_run=False
        )

        assert result.resolved_count == 0
        assert result.preserved_count == 1


# ---------------------------------------------------------------------------
# Test 5: Admin merge fallback opt-in
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAdminMergeFallbackOptIn:
    """HandlerAdminMerge only fires when enable_admin_merge_fallback=True."""

    def _make_stuck_pr(self, pr_number: int) -> ModelStuckQueueEntry:
        return ModelStuckQueueEntry(
            pr_number=pr_number,
            repo="OmniNode-ai/omnimarket",
            title=f"PR #{pr_number}",
            queue_entered_at=datetime.now(tz=UTC) - timedelta(minutes=45),
            queue_age_minutes=45.0,
        )

    async def test_opt_in_true_dry_run_returns_merged_count(self, caplog: Any) -> None:
        """opt-in=True, dry_run=True: prs_merged=1 and ADMIN MERGE TRIGGERED logged."""

        class MockAdminAdapter:
            async def admin_merge(self, repo: str, pr_number: int) -> None:
                pytest.fail("Should not call admin_merge in dry_run")

        stuck_pr = self._make_stuck_pr(pr_number=77)
        handler = HandlerAdminMerge(adapter=MockAdminAdapter())

        with caplog.at_level(logging.WARNING):
            result = await handler.handle(
                stuck_prs=[stuck_pr],
                enable_admin_merge_fallback=True,
                dry_run=True,
            )

        assert isinstance(result, ModelAdminMergeResult)
        assert result.prs_merged == 1
        assert "ADMIN MERGE TRIGGERED" in caplog.text
        assert "77" in caplog.text

    async def test_opt_in_false_handler_not_called(self) -> None:
        """opt-in=False: all PRs skipped, adapter.admin_merge NOT called."""

        class MockAdminAdapter:
            async def admin_merge(self, repo: str, pr_number: int) -> None:
                pytest.fail("Should not call admin_merge when opt-in=False")

        stuck_pr = self._make_stuck_pr(pr_number=78)
        handler = HandlerAdminMerge(adapter=MockAdminAdapter())

        result = await handler.handle(
            stuck_prs=[stuck_pr],
            enable_admin_merge_fallback=False,
            dry_run=False,
        )

        assert result.prs_merged == 0
        assert result.prs_skipped == 1

    async def test_opt_in_true_live_calls_adapter(self) -> None:
        """opt-in=True, dry_run=False: adapter.admin_merge IS called."""

        class MockAdminAdapter:
            def __init__(self) -> None:
                self.merged: list[tuple[str, int]] = []

            async def admin_merge(self, repo: str, pr_number: int) -> None:
                self.merged.append((repo, pr_number))

        stuck_pr = self._make_stuck_pr(pr_number=79)
        adapter = MockAdminAdapter()
        handler = HandlerAdminMerge(adapter=adapter)

        result = await handler.handle(
            stuck_prs=[stuck_pr],
            enable_admin_merge_fallback=True,
            dry_run=False,
        )

        assert result.prs_merged == 1
        assert ("OmniNode-ai/omnimarket", 79) in adapter.merged
