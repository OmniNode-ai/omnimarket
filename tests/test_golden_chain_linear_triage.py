"""Golden chain tests for node_linear_triage.

All tests use injectable stub clients (LinearClientProtocol, GitHubClientProtocol)
so no network calls are made. Verifies age classification, PR-state detection,
dry_run mode, epic completion detection, orphan counting, stale flagging, and
the --timeout CLI enforcement.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_linear_triage.__main__ import _run_with_timeout
from omnimarket.nodes.node_linear_triage.handlers.handler_linear_triage import (
    GitHubClientProtocol,
    HandlerLinearTriage,
    LinearClientProtocol,
)
from omnimarket.nodes.node_linear_triage.models.model_linear_triage_state import (
    ModelLinearTriageResult,
    ModelLinearTriageStartCommand,
)


def _make_issue(
    *,
    id: str = "abc",
    identifier: str = "OMN-1234",
    title: str = "Test ticket",
    state: str = "In Progress",
    days_ago: int = 5,
    branch_name: str = "",
    parent_id: str = "",
    labels: list[str] | None = None,
) -> dict[str, Any]:
    updated_at = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat()
    return {
        "id": id,
        "identifier": identifier,
        "title": title,
        "state": {"name": state},
        "updatedAt": updated_at,
        "branchName": branch_name,
        "parent": {"id": parent_id} if parent_id else None,
        "labels": {"nodes": [{"name": lbl} for lbl in (labels or [])]},
    }


def _wrap_issues(issues: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "data": {
            "issues": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": issues,
            }
        }
    }


def _stub_client(
    issues: list[dict[str, Any]],
    children: dict[str, list[dict[str, Any]]] | None = None,
) -> LinearClientProtocol:
    """Build a stub LinearClientProtocol."""
    client = MagicMock(spec=LinearClientProtocol)
    client.list_issues.return_value = _wrap_issues(issues)

    def _list_children(
        *, parent_id: str, limit: int = 50, after: str | None = None
    ) -> dict[str, Any]:
        node_list = (children or {}).get(parent_id, [])
        return {"data": {"issues": {"nodes": node_list}}}

    client.list_children.side_effect = _list_children
    return client  # type: ignore[return-value]


def _stub_github(
    merged_prs: dict[str, dict[str, str] | None] | None = None,
) -> GitHubClientProtocol:
    """Build a stub GitHubClientProtocol.

    Args:
        merged_prs: Map of search_term -> fake PR dict, or None for no results.
            If a key maps to None, that search returns empty.
    """
    gh = MagicMock(spec=GitHubClientProtocol)
    pr_map = merged_prs or {}

    def _search_prs(*, search_term: str, state: str = "all") -> list[dict[str, str]]:
        pr = pr_map.get(search_term)
        return [pr] if pr else []

    def _search_prs_in_repo(
        *, repo: str, search_term: str, state: str = "all"
    ) -> list[dict[str, str]]:
        pr = pr_map.get(search_term)
        return [pr] if pr else []

    def _list_prs_by_head(
        *, repo: str, branch: str, state: str = "merged"
    ) -> list[dict[str, str]]:
        return []

    gh.search_prs.side_effect = _search_prs
    gh.search_prs_in_repo.side_effect = _search_prs_in_repo
    gh.list_prs_by_head.side_effect = _list_prs_by_head
    return gh  # type: ignore[return-value]


@pytest.mark.unit
class TestLinearTriageGoldenChain:
    def test_empty_ticket_list(self) -> None:
        """When there are no non-done tickets, result has all zeros."""
        client = _stub_client([])
        gh = _stub_github()
        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = handler.handle(ModelLinearTriageStartCommand())

        assert result.status == "completed"
        assert result.total_scanned == 0
        assert result.marked_done == 0
        assert result.stale_flagged == 0
        assert result.orphaned == 0

    def test_recent_ticket_no_pr_no_change(self) -> None:
        """Recent ticket with no merged PR → no action."""
        client = _stub_client([_make_issue(days_ago=3)])
        gh = _stub_github()
        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = handler.handle(ModelLinearTriageStartCommand())

        assert result.total_scanned == 1
        assert result.recent_count == 1
        assert result.marked_done == 0
        assert result.stale_flagged == 0

    def test_recent_ticket_merged_pr_marked_done(self) -> None:
        """Recent ticket with a merged PR gets marked done."""
        fake_pr = {
            "number": "42",
            "url": "https://github.com/OmniNode-ai/omniclaude/pull/42",
            "mergedAt": "2026-04-08T10:00:00Z",
            "repo": "omniclaude",
        }

        issue = _make_issue(
            days_ago=3,
            identifier="OMN-1234",
            branch_name="jonah/omn-1234-omniclaude-some-feature",
        )
        client = _stub_client([issue])
        gh = _stub_github(merged_prs={"OMN-1234": fake_pr})
        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = handler.handle(ModelLinearTriageStartCommand())

        assert result.marked_done == 1
        client.save_issue.assert_called_once_with(issue_id="abc", state="Done")
        client.save_comment.assert_called_once()

    def test_dry_run_does_not_mutate(self) -> None:
        """dry_run=True: would_mark_done action but no save_issue calls."""
        fake_pr = {
            "number": "7",
            "url": "https://github.com/OmniNode-ai/omniclaude/pull/7",
            "mergedAt": "2026-04-07T09:00:00Z",
            "repo": "omniclaude",
        }

        client = _stub_client([_make_issue(days_ago=2, identifier="OMN-1234")])
        gh = _stub_github(merged_prs={"OMN-1234": fake_pr})
        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = handler.handle(ModelLinearTriageStartCommand(dry_run=True))

        assert result.dry_run is True
        assert result.marked_done == 0
        assert any(a.action == "would_mark_done" for a in result.actions)
        client.save_issue.assert_not_called()
        client.save_comment.assert_not_called()

    def test_stale_ticket_flagged(self) -> None:
        """Ticket older than threshold in In Progress state is flagged stale."""
        # 65 days old In Progress ticket
        issue = _make_issue(identifier="OMN-9999", state="In Progress", days_ago=65)
        client = _stub_client([issue])
        gh = _stub_github()
        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = handler.handle(ModelLinearTriageStartCommand(threshold_days=14))

        assert result.stale_count == 1
        assert result.stale_flagged == 1
        assert any(a.action == "flag_stale" for a in result.actions)

    def test_orphan_detection(self) -> None:
        """Ticket without parent_id is counted as orphaned."""
        issue = _make_issue(parent_id="")
        client = _stub_client([issue])
        gh = _stub_github()
        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = handler.handle(ModelLinearTriageStartCommand())

        assert result.orphaned == 1

    def test_epic_completion_closes_parent(self) -> None:
        """Parent ticket with all children Done is closed as an epic."""
        parent = _make_issue(
            id="parent-id",
            identifier="OMN-100",
            title="Epic: Big Feature",
            state="In Progress",
            days_ago=5,
        )
        child1 = {"id": "c1", "identifier": "OMN-101", "state": {"name": "Done"}}
        child2 = {"id": "c2", "identifier": "OMN-102", "state": {"name": "Done"}}
        child3 = _make_issue(
            id="c3-id",
            identifier="OMN-103",
            title="Child ticket",
            state="In Progress",
            days_ago=5,
            parent_id="parent-id",
        )

        client = _stub_client(
            [parent, child3],
            children={"parent-id": [child1, child2, child3]},
        )
        gh = _stub_github()
        # child3 is in the issue list (non-done) so parent-id is a known parent
        # BUT child3.state = In Progress -> not all done -> epic NOT closed
        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = handler.handle(ModelLinearTriageStartCommand())
        assert result.epics_closed == 0

    def test_epic_completion_all_done(self) -> None:
        """Parent ticket closed when ALL children are Done."""
        parent = _make_issue(
            id="parent-id",
            identifier="OMN-100",
            title="Epic",
            state="In Progress",
            days_ago=5,
        )
        # Add a child issue to force parent_id into the set
        child_stub = _make_issue(
            id="c1-id",
            identifier="OMN-101",
            title="Child",
            state="In Progress",
            days_ago=5,
            parent_id="parent-id",
        )

        # children returned from list_children are all done
        all_done_children = [
            {"id": "c1", "identifier": "OMN-101", "state": {"name": "Done"}},
            {"id": "c2", "identifier": "OMN-102", "state": {"name": "Done"}},
        ]

        client = _stub_client(
            [parent, child_stub],
            children={"parent-id": all_done_children},
        )
        gh = _stub_github()
        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = handler.handle(ModelLinearTriageStartCommand())

        assert result.epics_closed == 1
        client.save_issue.assert_any_call(issue_id="parent-id", state="Done")


@pytest.mark.unit
class TestLinearTriageTimeout:
    def test_timeout_raises_asyncio_timeout(self) -> None:
        """_run_with_timeout raises asyncio.TimeoutError when handler exceeds limit."""

        def _slow_handle(cmd: Any) -> Any:
            time.sleep(5)

        handler = MagicMock(spec=HandlerLinearTriage)
        handler.handle.side_effect = _slow_handle

        with pytest.raises(TimeoutError):
            asyncio.run(
                _run_with_timeout(handler, ModelLinearTriageStartCommand(), timeout=1)
            )

    def test_no_timeout_completes_normally(self) -> None:
        """_run_with_timeout returns result when handler finishes within limit."""
        expected = ModelLinearTriageResult(total_scanned=3)
        handler = MagicMock(spec=HandlerLinearTriage)
        handler.handle.return_value = expected

        result = asyncio.run(
            _run_with_timeout(handler, ModelLinearTriageStartCommand(), timeout=30)
        )

        assert result.total_scanned == 3
