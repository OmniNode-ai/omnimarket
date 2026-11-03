# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A create through node_create_ticket sends an explicit null project and verifies it by readback.

Linear copies a parent's project onto a sub-issue created without one. These tests drive the real
HTTP gateway's create path over an offline GraphQL seam and fail when the create omits the
explicit null ``projectId`` or skips the independent readback of the new issue's project.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    LinearTicketHttpGateway,
)
from omnimarket.nodes.node_create_ticket.handlers.handler_ticket_project import (
    TicketProjectNotClearedError,
)

_NEW = "OMN-99100"


class _Linear:
    """An offline Linear: records every call; the new issue's project follows ``plan``.

    ``inherits`` is the project Linear attaches to the new issue ("" for none). ``delay`` makes
    the attachment appear only after that many reads, the way an asynchronous copy would.
    ``stuck`` makes the clear report success while leaving the project in place.
    """

    def __init__(
        self, *, inherits: str = "", delay: int = 0, stuck: bool = False
    ) -> None:
        self.inherits = inherits
        self.delay = delay
        self.stuck = stuck
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.reads = 0
        self.project: str | None = None
        self.created = False

    def post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((query, variables))
        if "GetTeamByName" in query:
            return {"data": {"teams": {"nodes": [{"id": "team-1"}]}}}
        if "GetBacklogState" in query:
            return {"data": {"workflowStates": {"nodes": [{"id": "state-1"}]}}}
        if "GetIssueByIdentifier" in query:
            return {"data": {"issue": {"id": "parent-uuid"}}}
        if "CreateIssue" in query:
            self.created = True
            if self.inherits and not self.delay:
                self.project = self.inherits
            return {
                "data": {
                    "issueCreate": {"issue": {"identifier": _NEW, "url": "https://x"}}
                }
            }
        if "issueUpdate" in query:
            if not self.stuck:
                self.project = None
            return {"data": {"issueUpdate": {"success": True, "issue": {}}}}
        if "GetIssueProject" in query:
            self.reads += 1
            if self.inherits and self.delay and self.reads == self.delay + 1:
                self.project = self.inherits
            return {
                "data": {
                    "issue": {
                        "identifier": _NEW,
                        "createdAt": "2026-11-03T03:00:00.000Z",
                        "labels": {"nodes": []},
                        "project": {"name": self.project} if self.project else None,
                    }
                }
            }
        raise AssertionError(f"unexpected query: {query[:60]}")

    def create_mutation_variables(self) -> dict[str, Any]:
        sent = [v for q, v in self.calls if "CreateIssue" in q]
        assert len(sent) == 1
        return sent[0]

    def order(self) -> list[str]:
        tags = (
            ("CreateIssue", "create"),
            ("GetIssueProject", "read"),
            ("issueUpdate", "clear"),
        )
        return [t for q, _ in self.calls for key, t in tags if key in q]


def _create(fake: _Linear, parent: str | None) -> tuple[str, str]:
    class _Offline(LinearTicketHttpGateway):
        _SETTLE_SECONDS = 0.0

        def _post(self, query: str, variables: dict[str, Any]) -> Any:
            return fake.post(query, variables)

    return _Offline("test-key-not-used").create_ticket(
        title="t",
        description="d",
        team="Omninode",
        parent=parent,
        assignee_id="user-1",
    )


@pytest.mark.parametrize("parent", ["OMN-17427", None])
def test_create_sends_projectid_as_an_explicit_null(parent: str | None) -> None:
    fake = _Linear()
    _create(fake, parent)
    sent = fake.create_mutation_variables()
    assert "projectId" in sent, "the create must name projectId, not omit it"
    assert sent["projectId"] is None
    create_query = next(q for q, _ in fake.calls if "CreateIssue" in q)
    assert "projectId: $projectId" in create_query
    assert "$projectId: String" in create_query


@pytest.mark.parametrize("parent", ["OMN-17427", None])
def test_create_reads_the_new_issue_back_after_creating_it(parent: str | None) -> None:
    fake = _Linear()
    _create(fake, parent)
    order = fake.order()
    assert order[0] == "create"
    assert "read" in order[1:], "the create must read the new issue's project back"


def test_create_clears_a_project_linear_attached_and_verifies_it() -> None:
    fake = _Linear(inherits="Ready")
    ident, _ = _create(fake, "OMN-17427")
    assert ident == _NEW
    assert fake.project is None
    assert fake.order() == ["create", "read", "clear", "read", "read"]


def test_create_clears_a_project_attached_after_the_create_returned() -> None:
    fake = _Linear(inherits="Ready", delay=1)
    _create(fake, "OMN-17427")
    assert fake.project is None, "the settle readback must catch a late project copy"
    assert "clear" in fake.order()


def test_create_without_a_project_sends_no_clear() -> None:
    fake = _Linear()
    _create(fake, "OMN-17427")
    assert "clear" not in fake.order()


def test_create_fails_when_the_readback_still_shows_a_project() -> None:
    fake = _Linear(inherits="Ready", stuck=True)
    with pytest.raises(TicketProjectNotClearedError):
        _create(fake, "OMN-17427")
