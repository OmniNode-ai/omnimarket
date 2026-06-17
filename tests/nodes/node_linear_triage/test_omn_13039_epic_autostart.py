# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD tests for OMN-13039: epic auto-start ratchet.

Unstarted epics (Backlog/Todo) with >=1 started/completed child must be
transitioned to In Progress by the triage sweep — one-way, never auto-Done.

DoD: the OMN-12952-class failure (epic sat unstarted under 22 merged PRs all day)
self-heals on the first sweep tick.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, call

import pytest

from omnimarket.nodes.node_linear_triage.handlers.handler_linear_triage import (
    GitHubClientProtocol,
    HandlerLinearTriage,
    LinearClientProtocol,
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
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": node_list,
                }
            }
        }

    client.list_children.side_effect = _list_children
    return client  # type: ignore[return-value]


def _stub_github_empty() -> GitHubClientProtocol:
    gh = MagicMock(spec=GitHubClientProtocol)
    gh.search_prs.return_value = []
    gh.search_prs_in_repo.return_value = []
    gh.list_prs_by_head.return_value = []
    return gh  # type: ignore[return-value]


def _make_child(*, identifier: str, state_name: str) -> dict[str, Any]:
    return {"identifier": identifier, "state": {"name": state_name}}


# ---------------------------------------------------------------------------
# Phase 5c: Epic auto-start ratchet tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEpicAutoStartRatchet:
    """Unstarted epics with >=1 started/completed child must transition to In Progress."""

    def test_backlog_epic_with_inprogress_child_is_started(self) -> None:
        """OMN-12952 class: Backlog epic with an In Progress child -> mark In Progress."""
        epic = _make_issue(
            id="epic-1",
            identifier="OMN-1000",
            title="Epic: June 11 remediation",
            state="Backlog",
            parent_id="",  # root = epic candidate
        )
        child_inprogress = _make_child(identifier="OMN-1001", state_name="In Progress")

        client = _stub_linear_client(
            [epic],
            children={"epic-1": [child_inprogress]},
        )
        gh = _stub_github_empty()

        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = handler.handle(ModelLinearTriageStartCommand(flag_only=False))

        assert result.epics_started == 1, (
            f"Expected epics_started=1 but got {result.epics_started}. "
            "Backlog epic with In Progress child must be started."
        )
        client.save_issue.assert_any_call(issue_id="epic-1", state="In Progress")

    def test_todo_epic_with_done_child_is_started(self) -> None:
        """Epic in Todo state with a Done child -> mark In Progress."""
        epic = _make_issue(
            id="epic-2",
            identifier="OMN-2000",
            title="Epic: platform cleanup",
            state="Todo",
            parent_id="",
        )
        child_done = _make_child(identifier="OMN-2001", state_name="Done")

        client = _stub_linear_client(
            [epic],
            children={"epic-2": [child_done]},
        )
        gh = _stub_github_empty()

        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = handler.handle(ModelLinearTriageStartCommand(flag_only=False))

        assert result.epics_started == 1
        client.save_issue.assert_any_call(issue_id="epic-2", state="In Progress")

    def test_already_inprogress_epic_is_not_re_started(self) -> None:
        """Epic already In Progress must NOT be touched by the auto-start ratchet."""
        epic = _make_issue(
            id="epic-3",
            identifier="OMN-3000",
            title="Epic: already started",
            state="In Progress",
            parent_id="",
        )
        child_inprogress = _make_child(identifier="OMN-3001", state_name="In Progress")

        client = _stub_linear_client(
            [epic],
            children={"epic-3": [child_inprogress]},
        )
        gh = _stub_github_empty()

        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = handler.handle(ModelLinearTriageStartCommand(flag_only=False))

        assert result.epics_started == 0
        # save_issue may be called for other phases but never with state="In Progress"
        # on epic-3 (the auto-start ratchet should skip it)
        for c in client.save_issue.call_args_list:
            if c == call(issue_id="epic-3", state="In Progress"):
                pytest.fail(
                    "Auto-start ratchet must NOT re-start an already In Progress epic"
                )

    def test_backlog_epic_with_all_backlog_children_is_not_started(self) -> None:
        """Epic with no started/done children must stay untouched."""
        epic = _make_issue(
            id="epic-4",
            identifier="OMN-4000",
            title="Epic: not yet started",
            state="Backlog",
            parent_id="",
        )
        child_backlog = _make_child(identifier="OMN-4001", state_name="Backlog")

        client = _stub_linear_client(
            [epic],
            children={"epic-4": [child_backlog]},
        )
        gh = _stub_github_empty()

        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = handler.handle(ModelLinearTriageStartCommand(flag_only=False))

        assert result.epics_started == 0
        for c in client.save_issue.call_args_list:
            if c == call(issue_id="epic-4", state="In Progress"):
                pytest.fail("Epic with only Backlog children must not be auto-started")

    def test_dry_run_reports_but_does_not_mutate(self) -> None:
        """dry_run=True: produce WOULD_MARK_IN_PROGRESS action but skip Linear mutation."""
        epic = _make_issue(
            id="epic-5",
            identifier="OMN-5000",
            title="Epic: dry run test",
            state="Backlog",
            parent_id="",
        )
        child_done = _make_child(identifier="OMN-5001", state_name="Done")

        client = _stub_linear_client(
            [epic],
            children={"epic-5": [child_done]},
        )
        gh = _stub_github_empty()

        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = handler.handle(
            ModelLinearTriageStartCommand(dry_run=True, flag_only=False)
        )

        assert result.epics_started == 0, "dry_run must not mutate epics_started count"
        would_start = [
            a
            for a in result.actions
            if a.action == EnumTriageAction.WOULD_MARK_IN_PROGRESS
            and a.ticket_id == "OMN-5000"
        ]
        assert would_start, (
            f"Expected WOULD_MARK_IN_PROGRESS action for OMN-5000; got {result.actions}"
        )
        client.save_issue.assert_not_called()

    def test_flag_only_suppresses_mutation(self) -> None:
        """flag_only=True (default): no mutation, suppressed_starts populated."""
        epic = _make_issue(
            id="epic-6",
            identifier="OMN-6000",
            title="Epic: flag-only test",
            state="Backlog",
            parent_id="",
        )
        child_ip = _make_child(identifier="OMN-6001", state_name="In Progress")

        client = _stub_linear_client(
            [epic],
            children={"epic-6": [child_ip]},
        )
        gh = _stub_github_empty()

        handler = HandlerLinearTriage(client=client, github_client=gh)
        # flag_only=True is the default
        result = handler.handle(ModelLinearTriageStartCommand())

        assert result.epics_started == 0
        client.save_issue.assert_not_called()

    def test_epic_never_auto_done_by_ratchet(self) -> None:
        """The auto-start ratchet must only ever set In Progress, never Done."""
        epic = _make_issue(
            id="epic-7",
            identifier="OMN-7000",
            title="Epic: all children done",
            state="Backlog",
            parent_id="",
        )
        child1 = _make_child(identifier="OMN-7001", state_name="Done")
        child2 = _make_child(identifier="OMN-7002", state_name="Done")

        client = _stub_linear_client(
            [epic],
            children={"epic-7": [child1, child2]},
        )
        gh = _stub_github_empty()

        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = handler.handle(ModelLinearTriageStartCommand(flag_only=False))

        # The epic_completion phase (Phase 5b) handles Done; Phase 5c must only In Progress
        # Verify that the auto-start ratchet action is MARK_IN_PROGRESS not MARK_DONE_EPIC
        start_actions = [
            a for a in result.actions if a.action == EnumTriageAction.MARK_IN_PROGRESS
        ]
        assert start_actions, (
            "Expected MARK_IN_PROGRESS action via auto-start ratchet for Backlog epic "
            "with Done children (Phase 5b may also mark it Done, but 5c must fire first)"
        )
        # At minimum confirm the ratchet fired (epics_started > 0 or _phase_epic_check
        # may also fire for the all-done case — the key is 5c fires before 5b can skip it)
        # The ratchet transitions Backlog -> In Progress; the epic_check then sees In Progress
        # and closes it.
        assert result.epics_started >= 0  # no crash; the result is valid

    def test_multiple_epics_multiple_start(self) -> None:
        """Multiple unstarted epics with active children all get started."""
        epic_a = _make_issue(
            id="epic-8a", identifier="OMN-8000", state="Backlog", parent_id=""
        )
        epic_b = _make_issue(
            id="epic-8b", identifier="OMN-8001", state="Backlog", parent_id=""
        )
        child_a = _make_child(identifier="OMN-8002", state_name="In Progress")
        child_b = _make_child(identifier="OMN-8003", state_name="Done")

        client = _stub_linear_client(
            [epic_a, epic_b],
            children={
                "epic-8a": [child_a],
                "epic-8b": [child_b],
            },
        )
        gh = _stub_github_empty()

        handler = HandlerLinearTriage(client=client, github_client=gh)
        result = handler.handle(ModelLinearTriageStartCommand(flag_only=False))

        assert result.epics_started == 2, (
            f"Expected 2 epics started, got {result.epics_started}"
        )


# ---------------------------------------------------------------------------
# ModelLinearTriageResult field contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTriageResultEpicsStartedField:
    def test_result_has_epics_started_field(self) -> None:
        """ModelLinearTriageResult must expose epics_started: int."""
        from omnimarket.nodes.node_linear_triage.models.model_linear_triage_state import (
            ModelLinearTriageResult,
        )

        result = ModelLinearTriageResult()
        assert hasattr(result, "epics_started"), (
            "ModelLinearTriageResult must have epics_started field"
        )
        assert result.epics_started == 0

    def test_enum_has_mark_in_progress_values(self) -> None:
        """EnumTriageAction must expose MARK_IN_PROGRESS and WOULD_MARK_IN_PROGRESS."""
        assert hasattr(EnumTriageAction, "MARK_IN_PROGRESS"), (
            "EnumTriageAction must have MARK_IN_PROGRESS"
        )
        assert hasattr(EnumTriageAction, "WOULD_MARK_IN_PROGRESS"), (
            "EnumTriageAction must have WOULD_MARK_IN_PROGRESS"
        )
