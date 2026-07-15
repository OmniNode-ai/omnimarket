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

from unittest.mock import patch

import pytest

from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    HandlerCreateTicket,
    ModelCreateTicketRequest,
)


class _EmptyIdLinearClient:
    """Mock client that mimics a Linear response with no issue identifier.

    This is the exact shape of the OMN-14547 bug: Linear returns a 200 with
    no usable identifier (or the prior implementation never called Linear at
    all and defaulted to ""). Either way, the handler must not report
    success.
    """

    def create_ticket(
        self, *, title: str, description: str, team: str, parent: str | None
    ) -> tuple[str, str]:
        del title, description, team, parent
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
        self, *, title: str, description: str, team: str, parent: str | None
    ) -> tuple[str, str]:
        self.calls.append(
            {"title": title, "description": description, "team": team, "parent": parent}
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
    handler = HandlerCreateTicket(linear_client=_EmptyIdLinearClient())
    request = ModelCreateTicketRequest(title="A ticket Linear silently drops")

    with pytest.raises(RuntimeError, match="empty ticket_id"):
        handler.handle(request)


# ---------------------------------------------------------------------------
# Happy path: a real (mocked) create yields a non-empty ticket_id
# ---------------------------------------------------------------------------


def test_successful_create_yields_non_empty_ticket_id() -> None:
    """A successful Linear create must produce a real ticket_id and ticket_url."""
    client = _SuccessLinearClient()
    handler = HandlerCreateTicket(linear_client=client)
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
    # description_body is the synthesized DoD checklist, not the raw input
    assert "## Definition of Done" in str(client.calls[0]["description"])


def test_successful_create_with_no_parent_passes_none() -> None:
    """When no parent is given, the client receives parent=None (not "")."""
    client = _SuccessLinearClient()
    handler = HandlerCreateTicket(linear_client=client)
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
        handler = HandlerCreateTicket()
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
    handler = HandlerCreateTicket()
    result = handler.handle(request)
    assert result.status == "dry_run"


def test_validation_error_never_calls_linear_client() -> None:
    """A validation error must short-circuit before any Linear client is constructed."""
    request = ModelCreateTicketRequest(title="Bad parent", parent="NOT-VALID")
    handler = HandlerCreateTicket()
    result = handler.handle(request)
    assert result.status == "error"
