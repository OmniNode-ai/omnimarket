# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD tests for OMN-13759: gate done-detection on the IMPLEMENTING PR.

node_linear_triage previously closed a ticket on ANY merged PR that merely
*mentioned* the ticket id (61/62 false positives, ~1.6% precision). The close
path is now gated on positive implementing-PR signals:

  1. The PR title's primary OMN id == this ticket id
     (conventional ``type(OMN-id): summary``).
  2. A ``Closes/Fixes/Resolves OMN-<id>`` keyword in the PR body.
  3. A GitHub GraphQL ``closingIssuesReferences`` link to this ticket.

PLUS two suppression guards:
  - not-reopened-after-merge (ticket transitioned Done -> active after merge).
  - for epics/parents, all children must be Done (a parent with any open child
    is never closed on a merged PR).

Incidental mention-only matches are rejected.
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
    _body_closes_ticket,
    _pr_implements_ticket,
    _ticket_reopened_after_merge,
    _title_primary_omn_id,
)
from omnimarket.nodes.node_linear_triage.models.model_linear_triage_state import (
    EnumTriageAction,
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


def _history(
    transitions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a Linear issue-history GraphQL response."""
    return {
        "data": {
            "issue": {
                "history": {
                    "nodes": transitions or [],
                }
            }
        }
    }


def _stub_linear_client(
    issues: list[dict[str, Any]],
    children: dict[str, list[dict[str, Any]]] | None = None,
    history: dict[str, dict[str, Any]] | None = None,
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

    def _list_history(*, issue_id: str) -> dict[str, Any]:
        return (history or {}).get(issue_id, _history([]))

    client.list_issue_history.side_effect = _list_history
    return client  # type: ignore[return-value]


def _stub_github(
    *,
    search_prs_results: list[dict[str, str]] | None = None,
    search_prs_in_repo_results: list[dict[str, str]] | None = None,
    closing_refs: dict[str, list[str]] | None = None,
) -> GitHubClientProtocol:
    gh = MagicMock(spec=GitHubClientProtocol)
    gh.search_prs.return_value = search_prs_results or []
    gh.search_prs_in_repo.return_value = search_prs_in_repo_results or []
    gh.list_prs_by_head.return_value = []

    def _closing(*, repo: str, number: int) -> list[str]:
        return (closing_refs or {}).get(str(number), [])

    gh.pr_closing_ticket_refs.side_effect = _closing
    return gh  # type: ignore[return-value]


def _make_pr(
    *,
    number: str = "42",
    repo: str = "omniclaude",
    title: str = "Fix something",
    body: str = "",
    merged_at: str = "2026-04-08T10:00:00Z",
) -> dict[str, str]:
    return {
        "number": number,
        "title": title,
        "body": body,
        "state": "closed",
        "mergedAt": merged_at,
        "url": f"https://github.com/OmniNode-ai/{repo}/pull/{number}",
        "repo": repo,
    }


def _would_close_ids(result: Any) -> set[str]:
    return {
        a.ticket_id
        for a in result.actions
        if a.action
        in (
            EnumTriageAction.WOULD_MARK_DONE,
            EnumTriageAction.WOULD_MARK_DONE_SUPERSEDED,
        )
    }


# ---------------------------------------------------------------------------
# Pure-helper unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestImplementingPrHelpers:
    def test_title_primary_id_from_conventional_paren_form(self) -> None:
        assert _title_primary_omn_id("fix(OMN-13759): gate close path") == "OMN-13759"

    def test_title_primary_id_prefers_paren_over_later_mention(self) -> None:
        # type(OMN-A): ... see also OMN-B  -> primary is OMN-A
        assert _title_primary_omn_id("feat(OMN-100): builds on OMN-200") == "OMN-100"

    def test_title_primary_id_falls_back_to_first_bare_id(self) -> None:
        assert _title_primary_omn_id("OMN-555 do the thing") == "OMN-555"

    def test_title_primary_id_none_when_absent(self) -> None:
        assert _title_primary_omn_id("chore: bump deps") is None

    def test_body_closes_keyword_variants(self) -> None:
        for body in (
            "Closes OMN-42",
            "closes OMN-42",
            "Fixes OMN-42",
            "fixed OMN-42",
            "Resolves: OMN-42",
            "this resolved OMN-42 finally",
        ):
            assert _body_closes_ticket(body, "OMN-42"), body

    def test_body_mention_without_keyword_is_not_a_close(self) -> None:
        assert not _body_closes_ticket("Related to OMN-42, see also", "OMN-42")
        assert not _body_closes_ticket("Closes OMN-99", "OMN-42")

    def test_reopened_after_merge_true_when_done_to_active_after_merge(self) -> None:
        hist = _history(
            [
                {
                    "createdAt": "2026-05-01T00:00:00Z",
                    "fromState": {"name": "Done"},
                    "toState": {"name": "In Progress"},
                }
            ]
        )
        assert _ticket_reopened_after_merge(hist, "2026-04-01T00:00:00Z")

    def test_reopened_after_merge_false_when_reopen_before_merge(self) -> None:
        hist = _history(
            [
                {
                    "createdAt": "2026-03-01T00:00:00Z",
                    "fromState": {"name": "Done"},
                    "toState": {"name": "In Progress"},
                }
            ]
        )
        assert not _ticket_reopened_after_merge(hist, "2026-04-01T00:00:00Z")

    def test_reopened_after_merge_false_for_forward_transition(self) -> None:
        hist = _history(
            [
                {
                    "createdAt": "2026-05-01T00:00:00Z",
                    "fromState": {"name": "Backlog"},
                    "toState": {"name": "In Progress"},
                }
            ]
        )
        assert not _ticket_reopened_after_merge(hist, "2026-04-01T00:00:00Z")

    def test_pr_implements_via_title(self) -> None:
        gh = _stub_github()
        pr = _make_pr(title="fix(OMN-7): do it", repo="omniclaude")
        assert _pr_implements_ticket("OMN-7", pr, gh=gh)

    def test_pr_implements_via_body_close(self) -> None:
        gh = _stub_github()
        pr = _make_pr(title="chore: misc", body="Closes OMN-7", repo="omniclaude")
        assert _pr_implements_ticket("OMN-7", pr, gh=gh)

    def test_pr_implements_via_closing_refs(self) -> None:
        gh = _stub_github(closing_refs={"42": ["OMN-7"]})
        pr = _make_pr(number="42", title="chore: misc", body="", repo="omniclaude")
        assert _pr_implements_ticket("OMN-7", pr, gh=gh)

    def test_pr_mention_only_is_not_implementing(self) -> None:
        gh = _stub_github()
        pr = _make_pr(
            title="fix(OMN-1): other ticket",
            body="incidentally touches OMN-7",
            repo="omniclaude",
        )
        assert not _pr_implements_ticket("OMN-7", pr, gh=gh)


# ---------------------------------------------------------------------------
# DoD #1 — mention-only PR implementing OMN-A does NOT flag OMN-B
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMentionOnlyDoesNotFlag:
    async def test_mention_only_pr_does_not_flag_other_ticket(self) -> None:
        # Ticket OMN-B; the only merged PR found implements OMN-A and merely
        # mentions OMN-B in its body.
        mention_pr = _make_pr(
            number="500",
            repo="omniclaude",
            title="fix(OMN-820001): implement A",
            body="Closes OMN-820001. Also relates to OMN-820002 for context.",
        )
        issue = _make_issue(id="b-1", identifier="OMN-820002", state="In Progress")
        client = _stub_linear_client([issue])
        gh = _stub_github(search_prs_results=[mention_pr])

        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = await handler.handle(ModelLinearTriageStartCommand(flag_only=True))

        assert "OMN-820002" not in _would_close_ids(result), (
            "mention-only PR must NOT flag OMN-820002 as done; "
            f"actions={[(a.ticket_id, a.action) for a in result.actions]}"
        )
        assert not any("OMN-820002" in s for s in result.suppressed_closes)

    async def test_mention_only_does_not_close_when_flag_only_false(self) -> None:
        mention_pr = _make_pr(
            number="501",
            repo="omniclaude",
            title="fix(OMN-820003): implement A",
            body="Closes OMN-820003; mentions OMN-820004.",
        )
        issue = _make_issue(id="b-2", identifier="OMN-820004", state="In Progress")
        client = _stub_linear_client([issue])
        gh = _stub_github(search_prs_results=[mention_pr])

        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = await handler.handle(ModelLinearTriageStartCommand(flag_only=False))

        assert result.marked_done == 0
        client.save_issue.assert_not_called()


# ---------------------------------------------------------------------------
# DoD #2 — a 'Closes OMN-B' PR DOES flag OMN-B
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestImplementingPrFlags:
    async def test_closes_keyword_flags_ticket(self) -> None:
        impl_pr = _make_pr(
            number="600",
            repo="omniclaude",
            title="chore: housekeeping",
            body="Closes OMN-820005",
        )
        issue = _make_issue(id="b-3", identifier="OMN-820005", state="In Progress")
        client = _stub_linear_client([issue])
        gh = _stub_github(search_prs_results=[impl_pr])

        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = await handler.handle(ModelLinearTriageStartCommand(flag_only=True))

        assert "OMN-820005" in _would_close_ids(result)
        assert any("OMN-820005" in s for s in result.suppressed_closes)

    async def test_title_primary_id_flags_ticket(self) -> None:
        impl_pr = _make_pr(
            number="601",
            repo="omniclaude",
            title="fix(OMN-820006): implement C",
            body="",
        )
        issue = _make_issue(id="c-1", identifier="OMN-820006", state="In Progress")
        client = _stub_linear_client([issue])
        gh = _stub_github(search_prs_results=[impl_pr])

        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = await handler.handle(ModelLinearTriageStartCommand(flag_only=True))

        assert "OMN-820006" in _would_close_ids(result)

    async def test_closing_refs_flags_ticket(self) -> None:
        impl_pr = _make_pr(
            number="602",
            repo="omniclaude",
            title="chore: misc",
            body="",
        )
        issue = _make_issue(id="d-1", identifier="OMN-820007", state="In Progress")
        client = _stub_linear_client([issue])
        gh = _stub_github(
            search_prs_results=[impl_pr],
            closing_refs={"602": ["OMN-820007"]},
        )

        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = await handler.handle(ModelLinearTriageStartCommand(flag_only=True))

        assert "OMN-820007" in _would_close_ids(result)

    async def test_implementing_pr_closes_when_flag_only_false(self) -> None:
        impl_pr = _make_pr(
            number="603",
            repo="omniclaude",
            title="fix(OMN-820008): real work",
        )
        issue = _make_issue(id="e-1", identifier="OMN-820008", state="In Progress")
        client = _stub_linear_client([issue])
        gh = _stub_github(search_prs_results=[impl_pr])

        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = await handler.handle(ModelLinearTriageStartCommand(flag_only=False))

        assert result.marked_done == 1
        client.save_issue.assert_called_once_with(issue_id="e-1", state="Done")


# ---------------------------------------------------------------------------
# DoD #3 — a ticket reopened after merge is NOT flagged
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReopenedAfterMergeNotFlagged:
    async def test_reopened_after_merge_suppresses_close(self) -> None:
        impl_pr = _make_pr(
            number="700",
            repo="omniclaude",
            title="fix(OMN-820009): first attempt",
            merged_at="2026-04-01T00:00:00Z",
        )
        issue = _make_issue(id="r-1", identifier="OMN-820009", state="In Progress")
        # Reopened (Done -> In Progress) AFTER the PR merged.
        reopen_hist = {
            "r-1": _history(
                [
                    {
                        "createdAt": "2026-05-01T00:00:00Z",
                        "fromState": {"name": "Done"},
                        "toState": {"name": "In Progress"},
                    }
                ]
            )
        }
        client = _stub_linear_client([issue], history=reopen_hist)
        gh = _stub_github(search_prs_results=[impl_pr])

        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = await handler.handle(ModelLinearTriageStartCommand(flag_only=True))

        assert "OMN-820009" not in _would_close_ids(result), (
            "ticket reopened after merge must NOT be flagged as done"
        )

    async def test_not_reopened_still_flags(self) -> None:
        impl_pr = _make_pr(
            number="701",
            repo="omniclaude",
            title="fix(OMN-820010): work",
            merged_at="2026-04-01T00:00:00Z",
        )
        issue = _make_issue(id="r-2", identifier="OMN-820010", state="In Progress")
        # Only a forward transition; no Done -> active reopen.
        hist = {
            "r-2": _history(
                [
                    {
                        "createdAt": "2026-03-01T00:00:00Z",
                        "fromState": {"name": "Backlog"},
                        "toState": {"name": "In Progress"},
                    }
                ]
            )
        }
        client = _stub_linear_client([issue], history=hist)
        gh = _stub_github(search_prs_results=[impl_pr])

        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = await handler.handle(ModelLinearTriageStartCommand(flag_only=True))

        assert "OMN-820010" in _would_close_ids(result)


# ---------------------------------------------------------------------------
# DoD #4 — an epic with open children is NOT flagged via the PR path
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEpicWithOpenChildrenNotFlagged:
    async def test_parent_with_open_child_not_flagged_by_merged_pr(self) -> None:
        impl_pr = _make_pr(
            number="800",
            repo="omniclaude",
            title="feat(OMN-820011): epic-level work",
        )
        parent = _make_issue(id="epic-1", identifier="OMN-820011", state="In Progress")
        child = _make_issue(
            id="child-1",
            identifier="OMN-820012",
            state="In Progress",
            parent_id="epic-1",
        )
        client = _stub_linear_client([parent, child])

        def _search(*, search_term: str, state: str = "all") -> list[dict[str, str]]:
            return [impl_pr] if "OMN-820011" in search_term else []

        gh = MagicMock(spec=GitHubClientProtocol)
        gh.search_prs.side_effect = _search
        gh.search_prs_in_repo.return_value = []
        gh.list_prs_by_head.return_value = []
        gh.pr_closing_ticket_refs.return_value = []

        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = await handler.handle(ModelLinearTriageStartCommand(flag_only=True))

        assert "OMN-820011" not in _would_close_ids(result), (
            "epic with an open child must NOT be flagged on a merged PR"
        )

    async def test_parent_with_all_children_done_can_be_flagged(self) -> None:
        # Children all Done are filtered out of the non-done list query, so the
        # parent has no open children present -> the PR path may flag it.
        impl_pr = _make_pr(
            number="801",
            repo="omniclaude",
            title="feat(OMN-820013): final epic work",
        )
        parent = _make_issue(id="epic-2", identifier="OMN-820013", state="In Progress")
        client = _stub_linear_client([parent])

        def _search(*, search_term: str, state: str = "all") -> list[dict[str, str]]:
            return [impl_pr] if "OMN-820013" in search_term else []

        gh = MagicMock(spec=GitHubClientProtocol)
        gh.search_prs.side_effect = _search
        gh.search_prs_in_repo.return_value = []
        gh.list_prs_by_head.return_value = []
        gh.pr_closing_ticket_refs.return_value = []

        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = await handler.handle(ModelLinearTriageStartCommand(flag_only=True))

        assert "OMN-820013" in _would_close_ids(result)
