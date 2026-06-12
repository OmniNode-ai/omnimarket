# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD tests for OMN-12863: fix stateId mutation and OCC false-positive done-detection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from omnimarket.nodes.node_linear_triage.handlers.handler_linear_triage import (
    GitHubClientProtocol,
    HandlerLinearTriage,
    LinearClientProtocol,
    LinearHttpClient,
)
from omnimarket.nodes.node_linear_triage.models.model_linear_triage_state import (
    ModelLinearTriageStartCommand,
)

# ---------------------------------------------------------------------------
# Helpers shared across tests
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
        return {"data": {"issues": {"nodes": node_list}}}

    client.list_children.side_effect = _list_children
    return client  # type: ignore[return-value]


def _stub_github(
    search_prs_results: list[dict[str, str]] | None = None,
    search_prs_in_repo_results: list[dict[str, str]] | None = None,
) -> GitHubClientProtocol:
    """Stub GitHubClientProtocol with explicit per-method results."""
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
# Bug A — save_issue must send stateId, NOT stateName
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBugASaveIssueUsesStateId:
    def test_save_issue_mutation_uses_state_id_not_state_name(self) -> None:
        """LinearHttpClient.save_issue must send stateId, not stateName.

        The test captures the GraphQL query string sent to _post and asserts:
        - 'stateId' is present in the mutation
        - 'stateName' is NOT present in the mutation
        """
        client = LinearHttpClient(api_key="test-key")

        captured_queries: list[str] = []
        captured_vars: list[dict[str, Any]] = []

        def _fake_post(query: str, variables: dict[str, Any]) -> Any:
            captured_queries.append(query)
            captured_vars.append(variables)
            return {"data": {"issueUpdate": {"success": True}}}

        client._post = _fake_post  # type: ignore[method-assign]

        # Patch _get_workflow_states to return a known map
        client._workflow_state_cache = {
            "Done": "state-uuid-done",
            "In Progress": "state-uuid-ip",
        }  # type: ignore[attr-defined]

        client.save_issue(issue_id="issue-123", state="Done")

        assert len(captured_queries) == 1, "Expected exactly one _post call"
        mutation = captured_queries[0]
        assert "stateId" in mutation, f"'stateId' missing from mutation:\n{mutation}"
        assert "stateName" not in mutation, (
            f"'stateName' must not appear in mutation:\n{mutation}"
        )
        assert captured_vars[0].get("stateId") == "state-uuid-done"

    def test_save_issue_raises_on_unresolvable_state(self) -> None:
        """save_issue must raise a clear error when state name cannot be resolved."""
        client = LinearHttpClient(api_key="test-key")
        client._workflow_state_cache = {"Done": "state-uuid-done"}  # type: ignore[attr-defined]

        with pytest.raises(ValueError, match="Unknown workflow state"):
            client.save_issue(issue_id="issue-xyz", state="NonExistentState")

    def test_get_workflow_states_caches_result(self) -> None:
        """_get_workflow_states fetches once and caches; second call skips network."""
        client = LinearHttpClient(api_key="test-key")

        call_count = 0

        def _fake_post(query: str, variables: dict[str, Any]) -> Any:
            nonlocal call_count
            call_count += 1
            return {
                "data": {
                    "workflowStates": {
                        "nodes": [
                            {"id": "id-done", "name": "Done"},
                            {"id": "id-ip", "name": "In Progress"},
                        ]
                    }
                }
            }

        client._post = _fake_post  # type: ignore[method-assign]
        # Fresh LinearHttpClient starts with _workflow_state_cache = None (set in __init__)

        result1 = client._get_workflow_states(team="Omninode")  # type: ignore[attr-defined]
        result2 = client._get_workflow_states(team="Omninode")  # type: ignore[attr-defined]

        assert call_count == 1, "Network call should happen only once (cached)"
        assert result1 == result2
        assert result1["Done"] == "id-done"
        assert result1["In Progress"] == "id-ip"


# ---------------------------------------------------------------------------
# Bug B — OCC receipt PRs must NOT trigger mark_done
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBugBOccReceiptPrsExcluded:
    def test_occ_only_merged_pr_does_not_mark_done(self) -> None:
        """A ticket whose ONLY merged PR match is from onex_change_control must NOT
        be marked done — OCC PRs are receipts, not implementation work."""
        occ_pr = _make_merged_pr(number="2391", repo="onex_change_control")

        issue = _make_issue(identifier="OMN-9999", state="In Progress", days_ago=3)
        client = _stub_linear_client([issue])

        # search_prs_in_repo → called with the inferred repo (no repo slug resolved),
        # search_prs → org-wide search → returns the OCC PR only
        gh = _stub_github(
            search_prs_results=[occ_pr],
            search_prs_in_repo_results=[],
        )

        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = handler.handle(ModelLinearTriageStartCommand())

        assert result.marked_done == 0, (
            "OCC-only merged PR must not trigger mark_done; "
            f"got marked_done={result.marked_done}"
        )
        client.save_issue.assert_not_called()

    def test_real_repo_merged_pr_still_marks_done(self) -> None:
        """A ticket with a merged PR from a real implementation repo IS marked done
        when flag_only=False (the approved-close path)."""
        real_pr = _make_merged_pr(number="99", repo="omniclaude")

        issue = _make_issue(identifier="OMN-5555", state="In Progress", days_ago=3)
        client = _stub_linear_client([issue])
        gh = _stub_github(
            search_prs_results=[real_pr],
            search_prs_in_repo_results=[],
        )

        handler = HandlerLinearTriage(client=client, github_client=gh)
        # flag_only=False: this test exercises the approved-close path
        result = handler.handle(ModelLinearTriageStartCommand(flag_only=False))

        assert result.marked_done == 1, (
            "Merged PR in real repo must trigger mark_done; "
            f"got marked_done={result.marked_done}"
        )
        client.save_issue.assert_called_once_with(issue_id="abc", state="Done")

    def test_mixed_occ_and_real_merged_pr_marks_done(self) -> None:
        """If both an OCC PR and a real PR are matched, the ticket is still marked done
        when flag_only=False (real PR is the implementation signal)."""
        occ_pr = _make_merged_pr(number="2391", repo="onex_change_control")
        real_pr = _make_merged_pr(number="55", repo="omnimarket")

        issue = _make_issue(identifier="OMN-7777", state="In Progress", days_ago=2)
        client = _stub_linear_client([issue])

        # Org-wide search returns OCC first, then real — handler must pick real
        gh = _stub_github(
            search_prs_results=[occ_pr, real_pr],
            search_prs_in_repo_results=[],
        )

        handler = HandlerLinearTriage(client=client, github_client=gh)
        # flag_only=False: this test exercises the approved-close path
        result = handler.handle(ModelLinearTriageStartCommand(flag_only=False))

        assert result.marked_done == 1, (
            "Mixed OCC+real merged PRs: the real PR should still trigger mark_done"
        )

    def test_occ_repo_excluded_from_repo_scoped_search(self) -> None:
        """Even when a ticket's inferred repo IS onex_change_control, its merged PRs
        must not trigger mark_done (no implementation PRs come from OCC)."""
        # Branch name that would infer repo as onex_change_control
        issue = _make_issue(
            identifier="OMN-8888",
            state="In Progress",
            days_ago=2,
            branch_name="jonah/omn-8888-onex_change_control-receipt",
        )
        occ_pr = _make_merged_pr(number="100", repo="onex_change_control")

        client = _stub_linear_client([issue])
        gh = _stub_github(
            search_prs_results=[occ_pr],
            search_prs_in_repo_results=[occ_pr],
        )

        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = handler.handle(ModelLinearTriageStartCommand())

        assert result.marked_done == 0, (
            "OCC-repo PR from repo-scoped search must not mark done"
        )
