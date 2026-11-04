# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The create path sends an explicit null project and reads every new ticket back.

Linear copies a parent's project onto a sub-issue, so a create is only finished once an
independent query shows the new ticket with no project. These tests fail when the create
mutation omits the explicit null, when the create skips the readback, and when a project still
set after the create is not cleared.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_create_ticket.handlers import handler_create_ticket as hct
from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    LinearTicketHttpGateway,
)
from omnimarket.nodes.node_create_ticket.handlers.handler_ticket_project import (
    TicketProjectNotClearedError,
)


class _Linear:
    """A fake Linear: the new issue carries ``project`` until an issueUpdate clears it."""

    def __init__(self, project: dict[str, str] | None, *, stuck: bool = False) -> None:
        self.project = project
        self.stuck = stuck
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((query, variables))
        if "GetTeamByName" in query:
            return {"data": {"teams": {"nodes": [{"id": "team-1"}]}}}
        if "GetBacklogState" in query:
            return {"data": {"workflowStates": {"nodes": [{"id": "state-1"}]}}}
        if "GetIssueByIdentifier" in query:
            return {"data": {"issue": {"id": "parent-uuid"}}}
        if "CreateIssue" in query:
            return {
                "data": {
                    "issueCreate": {
                        "issue": {"identifier": "OMN-1", "url": "https://x"}
                    }
                }
            }
        if "issueUpdate" in query:
            if not self.stuck:
                self.project = None
            return {"data": {"issueUpdate": {"success": True, "issue": {}}}}
        return {
            "data": {
                "issue": {
                    "identifier": "OMN-1",
                    "createdAt": "2026-11-03T03:00:00.000Z",
                    "labels": {"nodes": []},
                    "project": self.project,
                }
            }
        }

    def of(self, text: str) -> list[tuple[str, dict[str, Any]]]:
        return [c for c in self.calls if text in c[0]]


def _create(fake: _Linear, parent: str | None) -> str:
    class _Offline(LinearTicketHttpGateway):
        def _post(self, query: str, variables: dict[str, Any]) -> Any:
            return fake.post(query, variables)

    ident, _ = _Offline("test-key-not-used").create_ticket(
        title="t", description="d", team="Omninode", parent=parent, assignee_id="u1"
    )
    return ident


def test_create_mutation_sends_an_explicit_null_project_and_never_a_project_variable() -> (
    None
):
    fake = _Linear(None)
    _create(fake, "OMN-17427")
    ((query, variables),) = fake.of("CreateIssue")
    assert "projectId: null" in query
    assert "projectId" not in variables
    assert "$projectId" not in query


def test_the_module_mutation_keeps_the_explicit_null() -> None:
    assert "projectId: null" in hct._ISSUE_CREATE_MUTATION


@pytest.mark.parametrize("parent", ["OMN-17427", None])
def test_every_create_reads_the_new_ticket_back_after_the_create(
    parent: str | None,
) -> None:
    fake = _Linear(None)
    _create(fake, parent)
    kinds = [
        "create"
        if "CreateIssue" in q
        else "read"
        if "GetIssueProject" in q
        else "other"
        for q, _ in fake.calls
    ]
    assert kinds.index("create") < len(kinds) - 1 - kinds[::-1].index("read")
    assert fake.of("issueUpdate") == []


@pytest.mark.parametrize("parent", ["OMN-17427", None])
def test_a_project_still_set_after_the_create_is_cleared_and_read_back(
    parent: str | None,
) -> None:
    fake = _Linear({"name": "Ready"})
    _create(fake, parent)
    assert len(fake.of("issueUpdate")) == 1
    assert "projectId: null" in fake.of("issueUpdate")[0][0]
    assert fake.project is None
    assert "GetIssueProject" in fake.calls[-1][0]  # the last call is the readback


def test_a_create_whose_project_cannot_be_cleared_is_not_reported_created() -> None:
    fake = _Linear({"name": "Ready"}, stuck=True)
    with pytest.raises(TicketProjectNotClearedError):
        _create(fake, "OMN-17427")
