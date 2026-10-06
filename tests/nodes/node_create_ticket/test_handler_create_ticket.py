# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the OMN-14547 fix on HandlerCreateTicket.

Prior to this change, ``node_create_ticket`` always returned
``status="created"`` with an empty ``ticket_id`` — a fake-success facade
that never called Linear. This file proves the two-part fix:

1. Fail-closed: an empty ``ticket_id`` from the (injected) Linear client
   raises rather than being reported as a successful creation. This is the
   RED→GREEN core of the ticket: before the fix, this scenario silently
   returned ``status="created"``; after the fix, it raises.
2. Real call happy-path: a successful create yields a non-empty
   ``ticket_id``/``ticket_url``, and the handler passes through the
   title/description/team/parent it was given.

A third test covers the adjacent fail-closed path: no injectable client and
no resolvable secret also raises, matching the sibling convention in
``node_repo_health_repair_effect``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    HandlerCreateTicket,
    ModelCreateTicketRequest,
    ModelTicketGuardDecision,
)


class _AdmitGuard:
    """The ticket-creation guard seam (OMN-20595), admitting every create."""

    def check(self, tool_input: dict[str, object]) -> ModelTicketGuardDecision:
        del tool_input
        return ModelTicketGuardDecision(admitted=True, guard_path="/test/guard.py")


class _EmptyIdLinearClient:
    """Mock client that mimics a Linear response with no issue identifier.

    This is the exact shape of the OMN-14547 bug: Linear returns a 200 with
    no usable identifier (or the prior implementation never called Linear at
    all and defaulted to ""). Either way, the handler must not report
    success.
    """

    def create_ticket(
        self,
        *,
        title: str,
        description: str,
        team: str,
        parent: str | None,
        assignee_id: str | None,
    ) -> tuple[str, str]:
        del title, description, team, parent, assignee_id
        return "", ""


class _SuccessLinearClient:
    """Mock client that mimics a real, successful Linear issueCreate call."""

    def __init__(
        self,
        ticket_id: str = "OMN-88123",
        ticket_url: str = "https://linear.app/omninode/issue/OMN-88123",
    ) -> None:
        self._ticket_id = ticket_id
        self._ticket_url = ticket_url
        self.calls: list[dict[str, object]] = []

    def create_ticket(
        self,
        *,
        title: str,
        description: str,
        team: str,
        parent: str | None,
        assignee_id: str | None,
    ) -> tuple[str, str]:
        self.calls.append(
            {
                "title": title,
                "description": description,
                "team": team,
                "parent": parent,
                "assignee_id": assignee_id,
            }
        )
        return self._ticket_id, self._ticket_url


# ---------------------------------------------------------------------------
# RED -> GREEN core: empty ticket_id must raise (fail-closed)
# ---------------------------------------------------------------------------


def test_empty_ticket_id_raises_instead_of_reporting_created() -> None:
    """A create response with no ticket_id must never surface as status='created'.

    This is the core regression: pre-fix, HandlerCreateTicket always
    returned status="created" with ticket_id="" — a green-over-nothing
    facade. Post-fix, that same empty-id shape raises.
    """
    handler = HandlerCreateTicket(
        ticket_guard=_AdmitGuard(), linear_client=_EmptyIdLinearClient()
    )
    request = ModelCreateTicketRequest(title="A ticket Linear silently drops")

    with pytest.raises(RuntimeError, match="empty ticket_id"):
        handler.handle(request)


# ---------------------------------------------------------------------------
# Happy path: a real (mocked) create yields a non-empty ticket_id
# ---------------------------------------------------------------------------


def test_successful_create_yields_non_empty_ticket_id() -> None:
    """A successful Linear create must produce a real ticket_id and ticket_url."""
    client = _SuccessLinearClient()
    handler = HandlerCreateTicket(ticket_guard=_AdmitGuard(), linear_client=client)
    request = ModelCreateTicketRequest(
        title="Add rate limiting to API",
        description="Protect the public endpoints.",
        team="Omninode",
        parent="OMN-1000",
    )

    result = handler.handle(request)

    assert result.status == "created"
    assert result.ticket_id == "OMN-88123"
    assert result.ticket_url == "https://linear.app/omninode/issue/OMN-88123"
    assert len(client.calls) == 1
    assert client.calls[0]["title"] == "Add rate limiting to API"
    assert client.calls[0]["team"] == "Omninode"
    assert client.calls[0]["parent"] == "OMN-1000"
    # OMN-20595: the caller's description reaches Linear unchanged
    assert client.calls[0]["description"] == "Protect the public endpoints."


def test_successful_create_with_no_parent_passes_none() -> None:
    """When no parent is given, the client receives parent=None (not "")."""
    client = _SuccessLinearClient()
    handler = HandlerCreateTicket(ticket_guard=_AdmitGuard(), linear_client=client)
    result = handler.handle(ModelCreateTicketRequest(title="No parent here"))

    assert result.status == "created"
    assert client.calls[0]["parent"] is None


# ---------------------------------------------------------------------------
# Missing secret: no injectable client + unresolved LINEAR_API_KEY raises
# ---------------------------------------------------------------------------


def test_missing_secret_raises_when_no_injectable_client() -> None:
    """When no client is injected, the handler raises if the secret is unset."""
    request = ModelCreateTicketRequest(title="No client, no secret")
    with patch(
        "omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket.resolve_api_key_loop_safe",
        return_value=None,
    ):
        handler = HandlerCreateTicket(ticket_guard=_AdmitGuard())
        with pytest.raises(RuntimeError, match="LINEAR_API_KEY"):
            handler.handle(request)


# ---------------------------------------------------------------------------
# dry_run and validation-error paths never touch the Linear client
# ---------------------------------------------------------------------------


def test_dry_run_never_calls_linear_client() -> None:
    """dry_run must short-circuit before any Linear client is constructed."""
    request = ModelCreateTicketRequest(title="Dry run only", dry_run=True)
    # No linear_client injected and no secret patched — if the handler tried
    # to resolve one, this would raise. It must not reach that code path.
    handler = HandlerCreateTicket(ticket_guard=_AdmitGuard())
    result = handler.handle(request)
    assert result.status == "dry_run"


def test_validation_error_never_calls_linear_client() -> None:
    """A validation error must short-circuit before any Linear client is constructed."""
    request = ModelCreateTicketRequest(title="Bad parent", parent="NOT-VALID")
    handler = HandlerCreateTicket(ticket_guard=_AdmitGuard())
    result = handler.handle(request)
    assert result.status == "error"


# ---------------------------------------------------------------------------
# Operator ruling 2026-09-30T14:30:05Z (OMN-17427): Backlog, no project
# ---------------------------------------------------------------------------


def _recording_gateway() -> tuple[object, list[tuple[str, dict[str, object]]]]:
    from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
        LinearTicketHttpGateway,
    )

    gateway = LinearTicketHttpGateway("test-key")
    calls: list[tuple[str, dict[str, object]]] = []

    def fake_post(query: str, variables: dict[str, object]) -> object:
        calls.append((query, variables))
        if "GetTeamByName" in query:
            return {"data": {"teams": {"nodes": [{"id": "team-1"}]}}}
        if "GetBacklogState" in query:
            return {"data": {"workflowStates": {"nodes": [{"id": "state-backlog"}]}}}
        if "GetViewer" in query:
            return {"data": {"viewer": {"id": "viewer-1"}}}
        if "GetIssueByIdentifier" in query:
            return {"data": {"issue": {"id": "parent-uuid"}}}
        return {"data": {"issueCreate": {"issue": {"identifier": "OMN-1", "url": "u"}}}}

    gateway._post = fake_post  # type: ignore[method-assign]
    return gateway, calls


def test_gateway_creates_in_backlog_with_no_project() -> None:
    gateway, calls = _recording_gateway()
    gateway.create_ticket(  # type: ignore[attr-defined]
        title="t",
        description="d",
        team="Omninode",
        parent="OMN-5",
        assignee_id=None,
    )
    query, variables = calls[-1]
    assert "issueCreate" in query
    assert variables["stateId"] == "state-backlog"
    assert variables["parentId"] == "parent-uuid"
    assert "projectId" not in variables
    assert "projectId" not in query


def test_gateway_refuses_when_the_team_has_no_backlog_state() -> None:
    from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
        LinearTicketHttpGateway,
    )

    gateway = LinearTicketHttpGateway("test-key")

    def fake_post(query: str, variables: dict[str, object]) -> object:
        del variables
        if "GetTeamByName" in query:
            return {"data": {"teams": {"nodes": [{"id": "team-1"}]}}}
        return {"data": {"workflowStates": {"nodes": []}}}

    gateway._post = fake_post  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="Backlog"):
        gateway.create_ticket(
            title="t",
            description="d",
            team="Omninode",
            parent=None,
            assignee_id=None,
        )


def test_request_refuses_a_project_naming_the_ruling() -> None:
    from pydantic import ValidationError

    assert ModelCreateTicketRequest(title="ok").project == ""
    with pytest.raises(ValidationError, match="OMN-17427"):
        ModelCreateTicketRequest(title="x", project="Sprint 2026-09-28")


# ---------------------------------------------------------------------------
# Operator rulings 2026-09-30 (OMN-17427): assignee by pillar, from the shared map
# ---------------------------------------------------------------------------

_DASH = "11111111-1111-1111-1111-111111111111"
_ONB = "22222222-2222-2222-2222-222222222222"


def _owners_file(tmp_path: Path) -> Path:
    path = tmp_path / "pillar_owners.yaml"
    path.write_text(
        "pillars:\n"
        f"  dashboard:\n    owner_linear_user_id: {_DASH}\n"
        f"  onboarding:\n    owner_linear_user_id: {_ONB}\n"
    )
    return path


def _create(tmp_path: Path, **kwargs: object) -> dict[str, object]:
    client = _SuccessLinearClient()
    handler = HandlerCreateTicket(
        ticket_guard=_AdmitGuard(),
        linear_client=client,
        pillar_owners_path=_owners_file(tmp_path),
    )
    handler.handle(ModelCreateTicketRequest(title="t", **kwargs))  # type: ignore[arg-type]
    return client.calls[0]


def test_pillar_dashboard_assigns_the_dashboard_owner(tmp_path: Path) -> None:
    assert _create(tmp_path, pillar="dashboard")["assignee_id"] == _DASH


def test_pillar_onboarding_assigns_the_onboarding_owner(tmp_path: Path) -> None:
    assert _create(tmp_path, pillar="onboarding")["assignee_id"] == _ONB


def test_no_pillar_leaves_the_creator_default(tmp_path: Path) -> None:
    assert _create(tmp_path)["assignee_id"] is None


def test_undeclared_pillar_fails_loud(tmp_path: Path) -> None:
    handler = HandlerCreateTicket(
        ticket_guard=_AdmitGuard(),
        linear_client=_SuccessLinearClient(),
        pillar_owners_path=_owners_file(tmp_path),
    )
    with pytest.raises(RuntimeError, match="payments"):
        handler.handle(ModelCreateTicketRequest(title="t", pillar="payments"))


def test_missing_map_fails_loud_when_a_pillar_applies(tmp_path: Path) -> None:
    handler = HandlerCreateTicket(
        ticket_guard=_AdmitGuard(),
        linear_client=_SuccessLinearClient(),
        pillar_owners_path=tmp_path / "absent.yaml",
    )
    with pytest.raises(RuntimeError, match="pillar_owners"):
        handler.handle(ModelCreateTicketRequest(title="t", pillar="dashboard"))


def test_gateway_assigns_the_pillar_owner_else_the_viewer() -> None:
    gateway, calls = _recording_gateway()
    gateway.create_ticket(  # type: ignore[attr-defined]
        title="t", description="d", team="Omninode", parent=None, assignee_id=_DASH
    )
    assert calls[-1][1]["assigneeId"] == _DASH
    assert not any("GetViewer" in q for q, _ in calls)


def test_gateway_without_a_pillar_assigns_the_creator() -> None:
    gateway, calls = _recording_gateway()
    gateway.create_ticket(  # type: ignore[attr-defined]
        title="t", description="d", team="Omninode", parent=None, assignee_id=None
    )
    assert calls[-1][1]["assigneeId"] == "viewer-1"
