# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ticket enrichment refuses unreadable text and keeps credentials out of results."""

import json
from io import StringIO

import pytest

from omnimarket.nodes.node_ticket_text_effect import NodeTicketTextEffect
from omnimarket.nodes.node_ticket_text_effect.handlers import (
    handler_ticket_text as handler,
)
from omnimarket.nodes.node_ticket_text_effect.models.model_ticket_text import (
    ModelTicketTextRequest,
)

pytestmark = pytest.mark.unit


def request(brief="Work.\n", ticket="OMN-7", **over):
    return ModelTicketTextRequest(
        brief, ticket, lambda: ("https://api.linear.app/graphql", "fixture-key"), **over
    )


@pytest.fixture
def issue(monkeypatch):
    value = {
        "identifier": "OMN-7",
        "title": "Carry the fix",
        "state": {"name": "Backlog", "type": "backlog"},
        "description": "Acceptance:\n- [ ] The fix works\n- [ ] The test passes",
    }
    monkeypatch.setattr(handler, "read_ticket", lambda *_args: value)
    return value


def test_ticket_text_and_criteria_are_quoted(issue):
    result = NodeTicketTextEffect().handle(request())
    assert result.source == "linear"
    assert result.criteria == 2
    assert "> - [ ] The fix works" in result.brief
    assert "ticket-text: OMN-7" in result.brief
    assert "fixture-key" not in str(result.receipt())
    assert result.brief.startswith("Work.\n")


def test_existing_text_avoids_a_second_read(issue, monkeypatch):
    first = NodeTicketTextEffect().handle(request())
    monkeypatch.setattr(
        handler, "read_ticket", lambda *_args: pytest.fail("duplicate read")
    )
    result = NodeTicketTextEffect().handle(request(first.brief))
    assert result.source == "brief"
    assert result.brief == first.brief


@pytest.mark.parametrize("description", [None, "", "  "])
def test_missing_description_refuses(issue, description):
    issue["description"] = description
    result = NodeTicketTextEffect().handle(request())
    assert result.refusal == "ticket-text-empty"


def test_unreadable_ticket_refuses(monkeypatch):
    def unreadable(*_args):
        raise handler.TicketTextError("unavailable")

    monkeypatch.setattr(handler, "read_ticket", unreadable)
    result = NodeTicketTextEffect().handle(request())
    assert result.refusal == "ticket-unreadable"
    assert "unavailable" in result.error


@pytest.mark.parametrize(
    ("ticket", "verbatim", "source"),
    [(None, False, "no-ticket"), ("OMN-7", True, "verbatim")],
)
def test_modes_that_need_no_read(ticket, verbatim, source):
    result = NodeTicketTextEffect().handle(request(ticket=ticket, verbatim=verbatim))
    assert result.source == source
    assert result.brief == "Work.\n"


@pytest.mark.parametrize(
    "url", ["https://example.com/graphql", "http://192.0.2.1/graphql"]
)
def test_credentials_never_go_to_an_unapproved_endpoint(url, monkeypatch):
    monkeypatch.setattr(
        handler.urllib.request,
        "urlopen",
        lambda *_args, **_kw: pytest.fail("credential sent"),
    )
    with pytest.raises(handler.TicketTextError, match="key is not sent"):
        handler.read_ticket("OMN-7", url, "fixture-key")


def test_the_http_read_parses_the_linear_response(monkeypatch):
    value = {"identifier": "OMN-7", "description": "Work"}

    def open_reply(req, timeout):
        assert timeout == 30
        assert json.loads(req.data)["variables"] == {"id": "OMN-7"}
        return StringIO(json.dumps({"data": {"issue": value}}))

    monkeypatch.setattr(handler.urllib.request, "urlopen", open_reply)
    assert (
        handler.read_ticket("OMN-7", "https://api.linear.app/graphql", "fixture-key")
        == value
    )
