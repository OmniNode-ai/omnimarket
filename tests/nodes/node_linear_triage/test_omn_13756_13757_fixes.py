# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD tests for OMN-13756 + OMN-13757.

OMN-13756: _phase_pr_check must include Backlog tickets (not just In Progress /
            In Review) when looking for merged implementation PRs, consistent
            with _ACTIVE_STATES = frozenset({"In Progress", "In Review", "Backlog"}).

OMN-13757: ModelLinearTriageResult.orphaned_tickets must be a list of every
            orphaned ticket (no cap/sample/truncate), and
            len(orphaned_tickets) == summary.orphaned. Similarly
            stale_tickets must enumerate stale tickets and satisfy
            len(stale_tickets) == stale_count.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_linear_triage.handlers.handler_linear_triage import (
    GitHubClientProtocol,
    HandlerLinearTriage,
    LinearClientProtocol,
)
from omnimarket.nodes.node_linear_triage.models.model_linear_triage_state import (
    EnumTriageAction,
    ModelLinearTriageResult,
    ModelLinearTriageStartCommand,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _stub_linear_client(
    issues: list[dict[str, Any]],
    children: dict[str, list[dict[str, Any]]] | None = None,
) -> LinearClientProtocol:
    client = MagicMock(spec=LinearClientProtocol)
    client.list_issues.return_value = _wrap_issues(issues)

    def _list_children(
        *, parent_id: str, limit: int = 50, after: str | None = None
    ) -> dict[str, Any]:
        node_list = (children or {}).get(parent_id, [])
        return {
            "data": {
                "issues": {
                    "nodes": node_list,
                    "pageInfo": {"hasNextPage": False},
                }
            }
        }

    client.list_children.side_effect = _list_children
    return client  # type: ignore[return-value]


def _stub_github(
    search_prs_results: list[dict[str, str]] | None = None,
    search_prs_in_repo_results: list[dict[str, str]] | None = None,
) -> GitHubClientProtocol:
    gh = MagicMock(spec=GitHubClientProtocol)
    gh.search_prs.return_value = search_prs_results or []
    gh.search_prs_in_repo.return_value = search_prs_in_repo_results or []
    gh.list_prs_by_head.return_value = []
    return gh  # type: ignore[return-value]


def _make_merged_pr(
    *,
    number: str = "42",
    repo: str = "omniclaude",
    merged_at: str = "2026-04-08T10:00:00Z",
) -> dict[str, str]:
    return {
        "number": number,
        "title": f"Fix something (#{number})",
        "state": "closed",
        "mergedAt": merged_at,
        "url": f"https://github.com/OmniNode-ai/{repo}/pull/{number}",
        "repo": repo,
    }


# ---------------------------------------------------------------------------
# OMN-13756: Backlog tickets with merged PRs should be detected
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOmn13756BacklogPrCheck:
    async def test_backlog_ticket_with_merged_pr_is_would_mark_done_candidate(
        self,
    ) -> None:
        """A Backlog ticket with a merged implementation PR must appear in
        suppressed_closes (flag_only=True) as a WOULD_MARK_DONE candidate.

        Regression: _phase_pr_check previously hardcoded {"In Progress", "In Review"},
        silently dropping Backlog tickets even though _ACTIVE_STATES includes Backlog.
        """
        merged_pr = _make_merged_pr(number="99", repo="omniclaude")
        issue = _make_issue(
            id="backlog-1",
            identifier="OMN-BACKLOG-1",
            state="Backlog",
            days_ago=3,
        )
        client = _stub_linear_client([issue])
        gh = _stub_github(search_prs_results=[merged_pr])

        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = await handler.handle(ModelLinearTriageStartCommand(flag_only=True))

        would_mark_done_actions = [
            a for a in result.actions if a.action == EnumTriageAction.WOULD_MARK_DONE
        ]
        assert len(would_mark_done_actions) >= 1, (
            "Backlog ticket with merged PR must appear as WOULD_MARK_DONE candidate. "
            f"Actions: {[a.action for a in result.actions]}"
        )
        assert any("OMN-BACKLOG-1" in entry for entry in result.suppressed_closes), (
            f"Backlog ticket missing from suppressed_closes: {result.suppressed_closes}"
        )
        pr_url = merged_pr["url"]
        assert any(pr_url in a.evidence for a in would_mark_done_actions), (
            f"Merged PR URL not cited in any WOULD_MARK_DONE evidence: "
            f"{[a.evidence for a in would_mark_done_actions]}"
        )

    async def test_backlog_ticket_marked_done_when_flag_only_false(self) -> None:
        """Backlog ticket with merged PR is closed when flag_only=False."""
        merged_pr = _make_merged_pr(number="77", repo="omniclaude")
        issue = _make_issue(
            id="backlog-2",
            identifier="OMN-BACKLOG-2",
            state="Backlog",
            days_ago=2,
        )
        client = _stub_linear_client([issue])
        gh = _stub_github(search_prs_results=[merged_pr])

        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = await handler.handle(ModelLinearTriageStartCommand(flag_only=False))

        assert result.marked_done == 1, (
            f"Backlog+merged PR with flag_only=False must mark_done. "
            f"marked_done={result.marked_done}"
        )
        client.save_issue.assert_called_once_with(issue_id="backlog-2", state="Done")

    async def test_in_progress_and_backlog_both_detected(self) -> None:
        """Both In Progress and Backlog tickets with merged PRs are detected."""
        merged_pr_1 = _make_merged_pr(number="10", repo="omniclaude")
        merged_pr_2 = _make_merged_pr(number="11", repo="omnimarket")
        issues = [
            _make_issue(
                id="ip-1", identifier="OMN-IP-1", state="In Progress", days_ago=2
            ),
            _make_issue(id="bl-1", identifier="OMN-BL-1", state="Backlog", days_ago=2),
        ]
        client = _stub_linear_client(issues)

        def _search(*, search_term: str, state: str = "all") -> list[dict[str, str]]:
            if "OMN-IP-1" in search_term:
                return [merged_pr_1]
            if "OMN-BL-1" in search_term:
                return [merged_pr_2]
            return []

        gh = MagicMock(spec=GitHubClientProtocol)
        gh.search_prs.side_effect = _search
        gh.search_prs_in_repo.return_value = []
        gh.list_prs_by_head.return_value = []

        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = await handler.handle(ModelLinearTriageStartCommand(flag_only=True))

        would_mark_done = [
            a for a in result.actions if a.action == EnumTriageAction.WOULD_MARK_DONE
        ]
        assert len(would_mark_done) == 2, (
            f"Both In Progress and Backlog tickets should be candidates. "
            f"Got: {[a.ticket_id for a in would_mark_done]}"
        )


# ---------------------------------------------------------------------------
# OMN-13757: orphaned_tickets / stale_tickets enumeration invariant
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOmn13757OrphanedStaleLists:
    def test_model_has_orphaned_tickets_field(self) -> None:
        """ModelLinearTriageResult must have an orphaned_tickets list field."""
        result = ModelLinearTriageResult(orphaned=0, orphaned_tickets=[])
        assert hasattr(result, "orphaned_tickets")
        assert isinstance(result.orphaned_tickets, list)

    def test_model_has_stale_tickets_field(self) -> None:
        """ModelLinearTriageResult must have a stale_tickets list field."""
        result = ModelLinearTriageResult(stale_count=0, stale_tickets=[])
        assert hasattr(result, "stale_tickets")
        assert isinstance(result.stale_tickets, list)

    async def test_orphaned_tickets_len_equals_orphaned_count(self) -> None:
        """len(result.orphaned_tickets) must equal result.orphaned."""
        issues = [
            _make_issue(
                id="orphan-1",
                identifier="OMN-ORPHAN-1",
                state="In Progress",
                parent_id="",
                days_ago=5,
            ),
            _make_issue(
                id="orphan-2",
                identifier="OMN-ORPHAN-2",
                state="Backlog",
                parent_id="",
                days_ago=10,
            ),
            _make_issue(
                id="child-1",
                identifier="OMN-CHILD-1",
                state="In Progress",
                parent_id="epic-parent",
                days_ago=3,
            ),
        ]
        client = _stub_linear_client(issues, children={"epic-parent": []})
        gh = _stub_github()

        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = await handler.handle(ModelLinearTriageStartCommand(flag_only=True))

        assert result.orphaned == len(result.orphaned_tickets), (
            f"Enumeration invariant violated: orphaned={result.orphaned} but "
            f"len(orphaned_tickets)={len(result.orphaned_tickets)}"
        )
        assert result.orphaned >= 2
        orphan_ids = {t.identifier for t in result.orphaned_tickets}
        assert "OMN-ORPHAN-1" in orphan_ids
        assert "OMN-ORPHAN-2" in orphan_ids
        assert "OMN-CHILD-1" not in orphan_ids

    async def test_orphaned_tickets_not_capped_or_sampled(self) -> None:
        """All 20 orphaned tickets must appear — no cap."""
        issues = [
            _make_issue(
                id=f"orphan-{i}",
                identifier=f"OMN-ORPHAN-{i:03d}",
                state="In Progress",
                parent_id="",
                days_ago=5,
            )
            for i in range(20)
        ]
        client = _stub_linear_client(issues)
        gh = _stub_github()

        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = await handler.handle(ModelLinearTriageStartCommand(flag_only=True))

        assert result.orphaned == 20
        assert len(result.orphaned_tickets) == 20, (
            f"All 20 orphaned tickets must be listed; got {len(result.orphaned_tickets)}"
        )

    async def test_stale_tickets_len_equals_stale_count(self) -> None:
        """len(result.stale_tickets) must equal result.stale_count."""
        old_date = (datetime.now(UTC) - timedelta(days=20)).isoformat()
        recent_date = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        issues = [
            {
                "id": "stale-1",
                "identifier": "OMN-STALE-1",
                "title": "Stale ticket",
                "state": {"name": "In Progress"},
                "updatedAt": old_date,
                "branchName": "",
                "parent": None,
                "labels": {"nodes": []},
            },
            {
                "id": "recent-1",
                "identifier": "OMN-RECENT-1",
                "title": "Recent ticket",
                "state": {"name": "In Progress"},
                "updatedAt": recent_date,
                "branchName": "",
                "parent": None,
                "labels": {"nodes": []},
            },
        ]
        client = _stub_linear_client(issues)
        gh = _stub_github()

        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = await handler.handle(
            ModelLinearTriageStartCommand(flag_only=True, threshold_days=14)
        )

        assert result.stale_count == len(result.stale_tickets), (
            f"Enumeration invariant violated: stale_count={result.stale_count} but "
            f"len(stale_tickets)={len(result.stale_tickets)}"
        )
        stale_ids = {t.identifier for t in result.stale_tickets}
        assert "OMN-STALE-1" in stale_ids
        assert "OMN-RECENT-1" not in stale_ids
