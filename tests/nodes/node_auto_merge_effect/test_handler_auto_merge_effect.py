# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for HandlerAutoMergeEffect.

Covers the deterministic happy path and key error branches.
All subprocess I/O is exercised via the injected `_run` callable -- no real gh calls.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from uuid import uuid4

import pytest

from omnimarket.nodes.node_auto_merge_effect.handlers.handler_auto_merge_effect import (
    HandlerAutoMergeEffect,
)
from omnimarket.nodes.node_auto_merge_effect.models.model_auto_merge_result import (
    ModelAutoMergeResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REPO = "OmniNode-ai/omnimarket"
PR_NUM = 42


def _make_run(
    responses: list[tuple[int, str, str]],
) -> Callable[[list[str]], tuple[int, str, str]]:
    """Return a _run stub that pops responses in order."""
    idx = 0

    def _run(_cmd: list[str]) -> tuple[int, str, str]:
        nonlocal idx
        rc, out, err = responses[idx]
        idx += 1
        return rc, out, err

    return _run


def _pr_view_response(
    merge_state: str = "CLEAN", review_decision: str = "APPROVED"
) -> str:
    return json.dumps(
        {
            "mergeStateStatus": merge_state,
            "statusCheckRollup": [],
            "reviewDecision": review_decision,
            "latestReviews": [],
        }
    )


def _merge_commit_response(sha: str = "abc1234def5678") -> str:
    return json.dumps({"mergeCommit": {"oid": sha}})


def _branch_response(branch: str = "jonah/omn-9999-fix-thing") -> str:
    return json.dumps({"headRefName": branch})


def _make_no_sleep(handler: HandlerAutoMergeEffect) -> None:
    handler._sleep = lambda _s: None


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_merges_and_returns_sha() -> None:
    """CLEAN PR with APPROVED reviews merges successfully."""
    correlation_id = uuid4()
    responses = [
        # Step 1: fetch mergeStateStatus
        (0, _pr_view_response("CLEAN", "APPROVED"), ""),
        # Step 2: CodeRabbit gate check
        (0, _pr_view_response("CLEAN", "APPROVED"), ""),
        # Step 3: execute merge
        (0, "", ""),
        # Step 4: fetch merge commit SHA
        (0, _merge_commit_response("deadbeef1234"), ""),
        # Step 5: extract ticket from branch
        (0, _branch_response("jonah/fix-no-ticket"), ""),
    ]
    handler = HandlerAutoMergeEffect(run_fn=_make_run(responses))
    _make_no_sleep(handler)

    result = await handler.handle(
        correlation_id=correlation_id,
        pr_number=PR_NUM,
        repo=REPO,
    )

    assert isinstance(result, ModelAutoMergeResult)
    assert result.merged is True
    assert result.merge_commit_sha == "deadbeef1234"
    assert result.blocked_reason is None
    assert result.correlation_id == correlation_id
    assert result.ticket_close_status == "skipped"


@pytest.mark.asyncio
async def test_happy_path_closes_ticket_when_branch_has_id() -> None:
    """Branch with OMN-XXXX in name triggers ticket close."""
    closed: list[str] = []

    def _close(tid: str) -> str:
        closed.append(tid)
        return "closed"

    correlation_id = uuid4()
    responses = [
        (0, _pr_view_response("CLEAN", "APPROVED"), ""),
        (0, _pr_view_response("CLEAN", "APPROVED"), ""),
        (0, "", ""),
        (0, _merge_commit_response("abc123"), ""),
        (0, _branch_response("jonah/omn-8340-auto-merge-node"), ""),
    ]
    handler = HandlerAutoMergeEffect(
        run_fn=_make_run(responses), close_ticket_fn=_close
    )
    _make_no_sleep(handler)

    result = await handler.handle(
        correlation_id=correlation_id,
        pr_number=PR_NUM,
        repo=REPO,
    )

    assert result.merged is True
    assert result.ticket_close_status == "closed"
    assert closed == ["OMN-8340"]


@pytest.mark.asyncio
async def test_explicit_ticket_id_takes_priority() -> None:
    """Explicit ticket_id is used without branch name extraction."""
    closed: list[str] = []

    def _close(tid: str) -> str:
        closed.append(tid)
        return "closed"

    correlation_id = uuid4()
    responses = [
        (0, _pr_view_response("CLEAN", "APPROVED"), ""),
        (0, _pr_view_response("CLEAN", "APPROVED"), ""),
        (0, "", ""),
        (0, _merge_commit_response("abc123"), ""),
        # No branch extraction call expected since ticket_id is explicit
    ]
    handler = HandlerAutoMergeEffect(
        run_fn=_make_run(responses), close_ticket_fn=_close
    )
    _make_no_sleep(handler)

    result = await handler.handle(
        correlation_id=correlation_id,
        pr_number=PR_NUM,
        repo=REPO,
        ticket_id="OMN-1234",
    )

    assert result.merged is True
    assert result.ticket_close_status == "closed"
    assert closed == ["OMN-1234"]


# ---------------------------------------------------------------------------
# Gate failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dirty_pr_blocks_immediately() -> None:
    """DIRTY mergeStateStatus must exit without polling."""
    correlation_id = uuid4()
    responses = [
        (0, _pr_view_response("DIRTY"), ""),
    ]
    handler = HandlerAutoMergeEffect(run_fn=_make_run(responses))
    _make_no_sleep(handler)

    result = await handler.handle(
        correlation_id=correlation_id,
        pr_number=PR_NUM,
        repo=REPO,
    )

    assert result.merged is False
    assert "merge conflicts" in (result.blocked_reason or "")


@pytest.mark.asyncio
async def test_changes_requested_review_blocks_merge() -> None:
    """CHANGES_REQUESTED reviewDecision must block after CLEAN CI."""
    correlation_id = uuid4()
    responses = [
        # PR state: CI clean
        (0, _pr_view_response("CLEAN", "CHANGES_REQUESTED"), ""),
        # CodeRabbit gate
        (
            0,
            json.dumps({"reviewDecision": "CHANGES_REQUESTED", "latestReviews": []}),
            "",
        ),
    ]
    handler = HandlerAutoMergeEffect(run_fn=_make_run(responses))
    _make_no_sleep(handler)

    result = await handler.handle(
        correlation_id=correlation_id,
        pr_number=PR_NUM,
        repo=REPO,
    )

    assert result.merged is False
    assert "CHANGES_REQUESTED" in (result.blocked_reason or "")


@pytest.mark.asyncio
async def test_gh_pr_view_failure_returns_blocked() -> None:
    """gh pr view non-zero exit returns merged=False with reason."""
    correlation_id = uuid4()
    responses = [
        (1, "", "Could not resolve to a PullRequest"),
    ]
    handler = HandlerAutoMergeEffect(run_fn=_make_run(responses))
    _make_no_sleep(handler)

    result = await handler.handle(
        correlation_id=correlation_id,
        pr_number=PR_NUM,
        repo=REPO,
    )

    assert result.merged is False
    assert result.blocked_reason is not None
    assert "gh pr view failed" in result.blocked_reason


@pytest.mark.asyncio
async def test_invalid_strategy_blocks_merge() -> None:
    """Invalid merge strategy string must block before executing gh."""
    correlation_id = uuid4()
    responses = [
        (0, _pr_view_response("CLEAN", "APPROVED"), ""),
        (0, _pr_view_response("CLEAN", "APPROVED"), ""),
        # No merge call expected
    ]
    handler = HandlerAutoMergeEffect(run_fn=_make_run(responses))
    _make_no_sleep(handler)

    result = await handler.handle(
        correlation_id=correlation_id,
        pr_number=PR_NUM,
        repo=REPO,
        strategy="fast-forward",
    )

    assert result.merged is False
    assert "Invalid merge strategy" in (result.blocked_reason or "")


@pytest.mark.asyncio
async def test_merge_command_failure_returns_blocked() -> None:
    """gh pr merge non-zero exit returns merged=False."""
    correlation_id = uuid4()
    responses = [
        (0, _pr_view_response("CLEAN", "APPROVED"), ""),
        (0, _pr_view_response("CLEAN", "APPROVED"), ""),
        (1, "", "GraphQL: Resource not accessible by integration"),
    ]
    handler = HandlerAutoMergeEffect(run_fn=_make_run(responses))
    _make_no_sleep(handler)

    result = await handler.handle(
        correlation_id=correlation_id,
        pr_number=PR_NUM,
        repo=REPO,
    )

    assert result.merged is False
    assert "gh pr merge failed" in (result.blocked_reason or "")


@pytest.mark.asyncio
async def test_ticket_close_failure_is_non_blocking() -> None:
    """Ticket close exception does not fail the merge result."""

    def _failing_close(_tid: str) -> str:
        raise RuntimeError("Linear API timeout")

    correlation_id = uuid4()
    responses = [
        (0, _pr_view_response("CLEAN", "APPROVED"), ""),
        (0, _pr_view_response("CLEAN", "APPROVED"), ""),
        (0, "", ""),
        (0, _merge_commit_response("abc123"), ""),
    ]
    handler = HandlerAutoMergeEffect(
        run_fn=_make_run(responses), close_ticket_fn=_failing_close
    )
    _make_no_sleep(handler)

    result = await handler.handle(
        correlation_id=correlation_id,
        pr_number=PR_NUM,
        repo=REPO,
        ticket_id="OMN-9999",
    )

    assert result.merged is True
    assert result.ticket_close_status == "failed"
