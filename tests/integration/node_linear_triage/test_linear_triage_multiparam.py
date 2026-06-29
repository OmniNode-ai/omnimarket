# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration proof for node_linear_triage (OMN-13679, WS-5).

Variant A (COMPUTE, direct in-process handler call). ``HandlerLinearTriage``
fetches non-done tickets, classifies them by age, cross-checks merged GitHub PRs,
and (only when ``flag_only=False``) mutates Linear. Both the Linear client and
the GitHub client are constructor-injected ``Protocol`` collaborators, so the
test wires deterministic in-memory mocks at the I/O boundary — NEVER live Linear,
never live GitHub, no monkeypatching of subprocess/urllib.

Param axes: all-recent (no action), stale ticket (FLAG_STALE finding =
NEGATIVE CONTROL), flag_only suppression of a merged-PR close, real mutation
when flag_only is disabled, and per-team routing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omnimarket.nodes.node_linear_triage.handlers.handler_linear_triage import (
    HandlerLinearTriage,
)
from omnimarket.nodes.node_linear_triage.models.model_linear_triage_state import (
    EnumTriageAction,
    ModelLinearTriageStartCommand,
)

_NOW = datetime.now(UTC)
_FRESH = _NOW.isoformat()
_STALE = (_NOW - timedelta(days=120)).isoformat()


def _node(
    identifier: str,
    state: str,
    updated_at: str,
    *,
    parent: str | None = None,
    labels: tuple[str, ...] = ("omnimarket",),
) -> dict[str, Any]:
    """Build a Linear GraphQL issue node as ``_parse_tickets`` expects it."""
    return {
        "id": f"id-{identifier}",
        "identifier": identifier,
        "title": f"[omnimarket] {identifier} work",
        "state": {"name": state},
        "updatedAt": updated_at,
        "branchName": "",
        "parent": {"id": parent} if parent else None,
        "labels": {"nodes": [{"name": label} for label in labels]},
    }


class _MockLinearClient:
    """In-memory Linear client implementing ``LinearClientProtocol``."""

    def __init__(self, nodes: list[dict[str, Any]]) -> None:
        self._nodes = nodes
        self.saved: list[tuple[str, str]] = []
        self.comments: list[tuple[str, str]] = []
        self.team_seen: str | None = None

    def list_issues(
        self,
        *,
        team: str,
        state_not_in: list[str] | None = None,
        limit: int = 250,
        after: str | None = None,
    ) -> Any:
        self.team_seen = team
        return {
            "data": {
                "issues": {
                    "nodes": self._nodes,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }

    def list_children(
        self, *, parent_id: str, limit: int = 50, after: str | None = None
    ) -> Any:
        return {
            "data": {
                "issues": {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }

    def get_issue(self, *, issue_id: str) -> Any:
        return {"data": {"issue": {}}}

    def save_issue(self, *, issue_id: str, state: str) -> None:
        self.saved.append((issue_id, state))

    def save_comment(self, *, issue_id: str, body: str) -> None:
        self.comments.append((issue_id, body))


class _MockGitHubClient:
    """In-memory GitHub client implementing ``GitHubClientProtocol``.

    ``merged_for`` is the set of ticket identifiers that have a merged
    implementation PR; everything else returns no PRs.
    """

    def __init__(self, merged_for: frozenset[str] = frozenset()) -> None:
        self._merged_for = merged_for

    def _merged_pr(self, repo: str) -> dict[str, str]:
        return {
            "number": "42",
            "title": "impl",
            "state": "closed",
            "mergedAt": "2026-06-01T00:00:00Z",
            "url": f"https://github.com/OmniNode-ai/{repo}/pull/42",
            "repo": repo,
        }

    def search_prs(
        self, *, search_term: str, state: str = "all"
    ) -> list[dict[str, str]]:
        if state == "merged" and search_term in self._merged_for:
            return [self._merged_pr("omnimarket")]
        return []

    def search_prs_in_repo(
        self, *, repo: str, search_term: str, state: str = "all"
    ) -> list[dict[str, str]]:
        if state == "merged" and search_term in self._merged_for:
            return [self._merged_pr(repo)]
        return []

    def list_prs_by_head(
        self, *, repo: str, branch: str, state: str = "merged"
    ) -> list[dict[str, str]]:
        return []


@pytest.mark.integration
def test_all_recent_no_pr_no_action() -> None:
    client = _MockLinearClient(
        [
            _node("OMN-1", "In Progress", _FRESH),
            _node("OMN-2", "In Review", _FRESH),
        ]
    )
    handler = HandlerLinearTriage(client=client, github_client=_MockGitHubClient())
    result = handler.handle(ModelLinearTriageStartCommand(team="Omninode"))

    assert result.status == "completed"
    assert result.total_scanned == 2
    assert result.recent_count == 2
    assert result.stale_count == 0
    assert result.marked_done == 0
    assert result.stale_flagged == 0
    assert client.saved == []


@pytest.mark.integration
def test_stale_ticket_is_flagged() -> None:
    # NEGATIVE CONTROL: a 120-day-old In Progress ticket must be flagged stale.
    client = _MockLinearClient([_node("OMN-9", "In Progress", _STALE)])
    handler = HandlerLinearTriage(client=client, github_client=_MockGitHubClient())
    result = handler.handle(ModelLinearTriageStartCommand())

    assert result.stale_count == 1
    assert result.stale_flagged == 1
    flag_actions = [
        a for a in result.actions if a.action == EnumTriageAction.FLAG_STALE
    ]
    assert flag_actions, "expected a FLAG_STALE finding"
    assert flag_actions[0].ticket_id == "OMN-9"
    assert flag_actions[0].stale_recommendation == "review_and_close"


@pytest.mark.integration
def test_flag_only_suppresses_merged_close() -> None:
    client = _MockLinearClient([_node("OMN-7", "In Progress", _FRESH)])
    handler = HandlerLinearTriage(
        client=client,
        github_client=_MockGitHubClient(merged_for=frozenset({"OMN-7"})),
    )
    # flag_only defaults to True → no mutation, candidate recorded for review.
    result = handler.handle(ModelLinearTriageStartCommand(flag_only=True))

    assert result.flag_only is True
    assert result.marked_done == 0
    assert client.saved == []
    assert result.suppressed_closes, "expected a suppressed close candidate"
    assert any("OMN-7" in entry for entry in result.suppressed_closes)
    would = [a for a in result.actions if a.action == EnumTriageAction.WOULD_MARK_DONE]
    assert would, "expected a WOULD_MARK_DONE action"
    assert would[0].ticket_id == "OMN-7"


@pytest.mark.integration
def test_merged_pr_marks_done_when_not_flag_only() -> None:
    client = _MockLinearClient([_node("OMN-8", "In Progress", _FRESH)])
    handler = HandlerLinearTriage(
        client=client,
        github_client=_MockGitHubClient(merged_for=frozenset({"OMN-8"})),
    )
    result = handler.handle(
        ModelLinearTriageStartCommand(flag_only=False, dry_run=False)
    )

    assert result.marked_done == 1
    assert ("id-OMN-8", "Done") in client.saved
    done_actions = [a for a in result.actions if a.action == EnumTriageAction.MARK_DONE]
    assert done_actions, "expected a MARK_DONE action"
    assert done_actions[0].ticket_id == "OMN-8"


@pytest.mark.integration
def test_team_routing_is_passed_through() -> None:
    client = _MockLinearClient([_node("OMN-5", "Backlog", _FRESH)])
    handler = HandlerLinearTriage(client=client, github_client=_MockGitHubClient())
    result = handler.handle(ModelLinearTriageStartCommand(team="CustomTeam"))

    assert client.team_seen == "CustomTeam"
    assert result.total_scanned == 1
