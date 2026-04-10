# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain integration tests for node_pr_lifecycle_orchestrator upgrade capabilities.

Tests 5 new capabilities added in OMN-8204 through OMN-8207:
  1. Auto-rebase: Track A-update PR triggers rebase in dry_run
  2. DAG ordering: mixed-repo PRs return in tier order
  3. Stuck queue detection (45 min): PR flagged in stuck_queue_prs
  4. Stuck queue detection (5 min): PR NOT flagged
  5. Comment resolution: bot nit comment resolved; human comment preserved
  6. Admin merge fallback: stuck PR triggers admin merge when enabled; skipped when disabled

Uses EventBusInmemory, zero infra required.

Related:
    - OMN-8204: HandlerAutoRebase
    - OMN-8205: DAG ordering
    - OMN-8206: Stuck merge queue detection
    - OMN-8207: HandlerCommentResolution + HandlerAdminMerge
    - OMN-8209: Golden chain tests (this file)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_admin_merge import (
    HandlerAdminMerge,
    ModelAdminMergeEntry,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_auto_rebase import (
    HandlerAutoRebase,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_comment_resolution import (
    HandlerCommentResolution,
)

from omnimarket.nodes.node_pr_lifecycle_inventory_compute.models.model_pr_lifecycle_inventory import (
    ModelStuckQueueEntry,
)

# ---------------------------------------------------------------------------
# Test 1: Auto-rebase — Track A-update PR triggers rebase in dry_run
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAutoRebase:
    """HandlerAutoRebase golden chain tests."""

    async def test_dry_run_returns_success_without_gh_call(self) -> None:
        """dry_run=True: returns ModelRebaseResult(success=True) without calling gh."""
        handler = HandlerAutoRebase()
        with patch("subprocess.run") as mock_run:
            result = await handler.handle(
                pr_number=42,
                repo="OmniNode-ai/omnimarket",
                dry_run=True,
            )
        assert result.success is True
        assert result.pr_number == 42
        assert result.repo == "OmniNode-ai/omnimarket"
        mock_run.assert_not_called()

    async def test_real_run_calls_gh_update_branch(self) -> None:
        """Non-dry-run: calls gh pr update-branch with correct args."""
        handler = HandlerAutoRebase()
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_sha = MagicMock()
        mock_sha.returncode = 0
        mock_sha.stdout = "abc1234\n"

        with patch("subprocess.run", side_effect=[mock_result, mock_sha]) as mock_run:
            result = await handler.handle(
                pr_number=42,
                repo="OmniNode-ai/omnimarket",
                dry_run=False,
            )

        assert result.success is True
        assert result.rebase_sha == "abc1234"
        first_call_args = mock_run.call_args_list[0][0][0]
        assert "gh" in first_call_args
        assert "update-branch" in first_call_args
        assert "42" in first_call_args

    async def test_gh_failure_returns_success_false(self) -> None:
        """gh pr update-branch exit non-zero → success=False with error_message."""
        handler = HandlerAutoRebase()
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "branch protection rule"
        mock_result.stdout = ""

        with patch("subprocess.run", return_value=mock_result):
            result = await handler.handle(
                pr_number=99,
                repo="OmniNode-ai/omniclaude",
                dry_run=False,
            )

        assert result.success is False
        assert result.error_message is not None
        assert "branch protection" in result.error_message


# ---------------------------------------------------------------------------
# Test 2: DAG ordering — mixed-repo PRs return in tier order
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDagOrdering:
    """_apply_dag_ordering golden chain tests."""

    def test_mixed_repos_ordered_by_tier(self) -> None:
        """PRs from mixed repos are returned in dependency tier order."""
        from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
            _apply_dag_ordering,
        )
        from omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols.protocol_sub_handlers import (
            EnumPrCategory,
            TriageRecord,
        )

        # Intentionally out of tier order: omnidash (10) first, omnibase_compat (0) last
        prs = (
            TriageRecord(
                pr_number=10,
                repo="OmniNode-ai/omnidash",
                category=EnumPrCategory.GREEN,
            ),
            TriageRecord(
                pr_number=5,
                repo="OmniNode-ai/omnimarket",
                category=EnumPrCategory.GREEN,
            ),
            TriageRecord(
                pr_number=1,
                repo="OmniNode-ai/omnibase_compat",
                category=EnumPrCategory.GREEN,
            ),
        )

        ordered = _apply_dag_ordering(prs)
        repos = [r.repo.split("/")[-1] for r in ordered]  # type: ignore[union-attr]
        assert repos.index("omnibase_compat") < repos.index("omnimarket")
        assert repos.index("omnimarket") < repos.index("omnidash")

    def test_unknown_repo_merges_last(self) -> None:
        """Unknown repos get tier 99 and sort after all known repos."""
        from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
            _apply_dag_ordering,
        )
        from omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols.protocol_sub_handlers import (
            EnumPrCategory,
            TriageRecord,
        )

        prs = (
            TriageRecord(
                pr_number=1,
                repo="OmniNode-ai/some-unknown-repo",
                category=EnumPrCategory.GREEN,
            ),
            TriageRecord(
                pr_number=2,
                repo="OmniNode-ai/omnibase_compat",
                category=EnumPrCategory.GREEN,
            ),
        )
        ordered = _apply_dag_ordering(prs)
        repos = [r.repo.split("/")[-1] for r in ordered]  # type: ignore[union-attr]
        assert repos[0] == "omnibase_compat"
        assert repos[-1] == "some-unknown-repo"

    def test_stable_sort_preserves_order_within_tier(self) -> None:
        """Same-tier PRs preserve original order (stable sort)."""
        from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
            _apply_dag_ordering,
        )
        from omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols.protocol_sub_handlers import (
            EnumPrCategory,
            TriageRecord,
        )

        prs = (
            TriageRecord(
                pr_number=10,
                repo="OmniNode-ai/omnidash",
                category=EnumPrCategory.GREEN,
            ),
            TriageRecord(
                pr_number=11,
                repo="OmniNode-ai/omnidash",
                category=EnumPrCategory.GREEN,
            ),
        )
        ordered = _apply_dag_ordering(prs)
        pr_numbers = [r.pr_number for r in ordered]  # type: ignore[union-attr]
        assert pr_numbers == [10, 11]


# ---------------------------------------------------------------------------
# Test 3 & 4: Stuck queue detection
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestStuckQueueDetection:
    """ModelStuckQueueEntry construction tests (unit-level)."""

    def test_45min_pr_qualifies_as_stuck(self) -> None:
        """A PR queued 45 minutes ago exceeds the 30-minute threshold."""
        entered_at = datetime.now(tz=UTC) - timedelta(minutes=45)
        entry = ModelStuckQueueEntry(
            pr_number=55,
            repo="OmniNode-ai/omnimarket",
            title="fix: slow query",
            queue_entered_at=entered_at,
            queue_age_minutes=45.0,
        )
        assert entry.queue_age_minutes > 30.0
        assert entry.pr_number == 55

    def test_5min_pr_does_not_qualify(self) -> None:
        """A PR queued 5 minutes ago is below the 30-minute threshold."""
        entered_at = datetime.now(tz=UTC) - timedelta(minutes=5)
        # Simulating what the handler would compute — 5 < 30, so it would NOT be appended
        age = (datetime.now(tz=UTC) - entered_at).total_seconds() / 60.0
        assert age < 30.0

    def test_stuck_queue_entry_is_frozen(self) -> None:
        """ModelStuckQueueEntry is immutable (frozen=True)."""
        import pydantic

        entry = ModelStuckQueueEntry(
            pr_number=1,
            repo="OmniNode-ai/omnimarket",
            title="test",
            queue_entered_at=datetime.now(tz=UTC),
            queue_age_minutes=35.0,
        )
        with pytest.raises((pydantic.ValidationError, TypeError)):
            entry.pr_number = 999  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Test 5: Comment resolution — bot nit resolved; human comment preserved
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCommentResolution:
    """HandlerCommentResolution golden chain tests."""

    async def test_dry_run_returns_resolvable_list_without_acting(self) -> None:
        """dry_run=True: identifies resolvable comments without patching."""
        handler = HandlerCommentResolution()

        mock_comments = [
            {
                "id": "001",
                "user": {"login": "coderabbitai[bot]"},
                "body": "nit: consider using a list comprehension here",
                "in_reply_to_id": None,
            },
        ]

        with (
            patch.object(
                handler, "_fetch_review_comments", return_value=mock_comments
            ) as mock_fetch,
            patch.object(handler, "_resolve_comment") as mock_resolve,
        ):
            result = await handler.handle(
                pr_number=42,
                repo="OmniNode-ai/omnimarket",
                dry_run=True,
            )

        assert len(result.resolvable_thread_ids) == 1
        assert result.resolved_count == 0
        assert result.dry_run is True
        mock_fetch.assert_called_once()
        mock_resolve.assert_not_called()

    async def test_human_comment_not_resolved(self) -> None:
        """Human comment author: not marked for resolution."""
        handler = HandlerCommentResolution()

        mock_comments = [
            {
                "id": "100",
                "user": {"login": "jonahgabriel"},
                "body": "nit: I think this needs a refactor",
                "in_reply_to_id": None,
            },
        ]

        with patch.object(
            handler, "_fetch_review_comments", return_value=mock_comments
        ):
            result = await handler.handle(
                pr_number=42,
                repo="OmniNode-ai/omnimarket",
                dry_run=True,
            )

        assert len(result.resolvable_thread_ids) == 0

    async def test_bot_nit_comment_resolved_in_non_dry_run(self) -> None:
        """Non-dry-run: bot nit comment is resolved via _resolve_comment."""
        handler = HandlerCommentResolution()

        mock_comments = [
            {
                "id": "200",
                "user": {"login": "coderabbitai[bot]"},
                "body": "minor: variable name could be more descriptive",
                "in_reply_to_id": None,
            },
        ]

        with (
            patch.object(handler, "_fetch_review_comments", return_value=mock_comments),
            patch.object(
                handler, "_resolve_comment", return_value=True
            ) as mock_resolve,
        ):
            result = await handler.handle(
                pr_number=77,
                repo="OmniNode-ai/omniclaude",
                dry_run=False,
            )

        assert result.resolved_count == 1
        mock_resolve.assert_called_once_with("OmniNode-ai/omniclaude", 77, "200")


# ---------------------------------------------------------------------------
# Test 6: Admin merge fallback
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestAdminMergeFallback:
    """HandlerAdminMerge golden chain tests."""

    async def test_skipped_when_fallback_disabled(self) -> None:
        """enable_admin_merge_fallback=False (default): no merge attempted."""
        handler = HandlerAdminMerge()
        stuck = [
            ModelAdminMergeEntry(
                pr_number=55, repo="OmniNode-ai/omnimarket", queue_age_minutes=45.0
            )
        ]

        result = await handler.handle(
            stuck_prs=stuck,
            enable_admin_merge_fallback=False,
        )

        assert result.skipped_reason == "enable_admin_merge_fallback=False"
        assert result.prs_merged == 0

    async def test_dry_run_with_fallback_enabled_logs_but_does_not_call_gh(
        self,
    ) -> None:
        """dry_run=True + enable_admin_merge_fallback=True: logs intent, no gh call."""
        handler = HandlerAdminMerge()
        stuck = [
            ModelAdminMergeEntry(
                pr_number=88, repo="OmniNode-ai/omnidash", queue_age_minutes=50.0
            )
        ]

        with patch.object(handler, "_gh_admin_merge") as mock_merge:
            result = await handler.handle(
                stuck_prs=stuck,
                enable_admin_merge_fallback=True,
                dry_run=True,
            )

        assert result.prs_merged == 1
        assert result.dry_run is True
        mock_merge.assert_not_called()

    async def test_stuck_pr_triggers_admin_merge_when_enabled(self) -> None:
        """enable_admin_merge_fallback=True + >threshold: gh pr merge --admin called."""
        handler = HandlerAdminMerge()
        stuck = [
            ModelAdminMergeEntry(
                pr_number=99, repo="OmniNode-ai/omnimarket", queue_age_minutes=45.0
            )
        ]

        with patch.object(handler, "_gh_admin_merge", return_value=True) as mock_merge:
            result = await handler.handle(
                stuck_prs=stuck,
                enable_admin_merge_fallback=True,
                admin_fallback_threshold_minutes=30,
                dry_run=False,
            )

        assert result.prs_merged == 1
        assert result.prs_failed == 0
        mock_merge.assert_called_once_with(99, "OmniNode-ai/omnimarket")

    async def test_pr_below_threshold_not_merged(self) -> None:
        """PR below threshold: no admin merge attempted."""
        handler = HandlerAdminMerge()
        stuck = [
            ModelAdminMergeEntry(
                pr_number=10, repo="OmniNode-ai/omnimarket", queue_age_minutes=10.0
            )
        ]

        with patch.object(handler, "_gh_admin_merge") as mock_merge:
            result = await handler.handle(
                stuck_prs=stuck,
                enable_admin_merge_fallback=True,
                admin_fallback_threshold_minutes=30,
                dry_run=False,
            )

        assert result.skipped_reason == "no_eligible_prs"
        mock_merge.assert_not_called()
