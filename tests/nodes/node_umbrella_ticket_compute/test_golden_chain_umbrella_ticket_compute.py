# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Umbrella cases from lane-brief, PR-body and dispatch-brief source tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_umbrella_ticket_compute.handlers import (
    handler_umbrella_ticket as umbrella,
)
from omnimarket.nodes.node_umbrella_ticket_compute.models.model_umbrella_ticket import (
    ModelUmbrellaTicketRequest,
)

pytestmark = pytest.mark.unit

TICKETS = ("OMN-17427",)
EVIDENCE = "Umbrella evidence: lane_intake_budget\n"


def parts(
    ticket: str = "OMN-17427", repo: str = "omnimarket"
) -> dict[str, tuple[str, str]]:
    return {
        umbrella.CLAIM_HEADING: (
            "",
            f"| lane=example | ticket={ticket} | repo={repo} |",
        )
    }


def brief_refusals(
    ticket: str = "OMN-17427",
    repo: str = "omnimarket",
    text: str = "Fix the red check.",
) -> tuple[str, ...]:
    return umbrella.brief_refusals(
        parts(ticket, repo), text, text, umbrella_tickets=TICKETS
    )


def test_check_refuses_an_umbrella_claim_ticket_on_a_lane_with_a_repo() -> None:
    (refusal,) = brief_refusals()
    assert "umbrella" in refusal
    assert "/omni:create-ticket (parent OMN-17427)" in refusal
    assert "Umbrella evidence: lane_intake_budget" in refusal
    assert "contracts/OMN-17427.yaml" in refusal


def test_check_admits_an_umbrella_brief_that_carries_umbrella_evidence() -> None:
    assert brief_refusals(text="Fix the red check.\n\n" + EVIDENCE) == ()


def test_check_admits_a_child_ticket_brief() -> None:
    assert brief_refusals(ticket="OMN-21301") == ()


@pytest.mark.parametrize("repo", ["none", "-", "n/a", "<repo>", ""])
def test_check_admits_an_umbrella_brief_with_no_repo(repo: str) -> None:
    assert brief_refusals(repo=repo, text="Report the open PRs. Read-only.") == ()


def test_check_refuses_an_umbrella_brief_whose_own_prose_carries_the_contract_step() -> (
    None
):
    step = "Before the first push, write contracts/OMN-17427.yaml with a dod_evidence entry per acceptance criterion and, where deploy-gate applies, a falsifiable deploy check scoped so the hosted runner can run it; the push is refused without it."
    assert brief_refusals(repo="none", text="Fix the red check.\n\n- " + step)


def test_check_refuses_a_title_whose_only_ticket_is_the_umbrella() -> None:
    why = umbrella.pr_refusal(
        "OMN-17427", "Body\n\nTicket: OMN-17427\n", umbrella_tickets=TICKETS
    )
    assert "umbrella" in why
    assert "Umbrella evidence: <reason>" in why
    assert "the title's only ticket OMN-17427" in why


def test_check_admits_an_umbrella_title_whose_body_carries_umbrella_evidence() -> None:
    body = "Body\n\n" + EVIDENCE + "\nTicket: OMN-17427\n"
    assert umbrella.pr_refusal("OMN-17427", body, umbrella_tickets=TICKETS) == ""


def test_check_admits_a_child_ticket_title() -> None:
    assert (
        umbrella.pr_refusal(
            "OMN-21301", "Body\n\nTicket: OMN-21301\n", umbrella_tickets=TICKETS
        )
        == ""
    )


@pytest.mark.parametrize(
    "body",
    [
        "Umbrella evidence: <reason>\n\nTicket: OMN-17427\n",
        "We carry `Umbrella evidence: lane_intake_budget` later.\n\nTicket: OMN-17427\n",
    ],
)
def test_check_reads_an_evidence_placeholder_or_a_prose_mention_as_no_evidence(
    body: str,
) -> None:
    assert umbrella.umbrella_evidence(body) is None
    assert umbrella.pr_refusal("OMN-17427", body, umbrella_tickets=TICKETS)


def test_the_brief_claim_ticket_is_read_when_it_is_the_umbrella() -> None:
    # Dispatch supplies both its command ticket and the parsed CLAIM ticket.
    ticket = umbrella.claim_ticket(parts())
    assert ticket == "OMN-17427"
    result = umbrella.HandlerUmbrellaTicket().handle(
        ModelUmbrellaTicketRequest(
            tickets=("OMN-21301", ticket),
            text="Fix the red check.",
            opens_pr=True,
            umbrella_tickets=TICKETS,
        )
    )
    assert result.refusals
    assert (
        not umbrella.HandlerUmbrellaTicket()
        .handle(
            ModelUmbrellaTicketRequest(
                tickets=("OMN-21301",),
                text="Fix the red check.",
                opens_pr=True,
                umbrella_tickets=TICKETS,
            )
        )
        .refusals
    )


def test_child_ticket_and_a_lane_with_no_repo_are_admitted() -> None:
    assert brief_refusals(ticket="OMN-21301") == ()
    assert brief_refusals(repo="", text="Report the open PRs. Read-only.") == ()
    assert brief_refusals()


@pytest.mark.parametrize("prefix", ["", "- ", "* ", "  "])
def test_evidence_lines_allow_list_items(prefix: str) -> None:
    assert umbrella.umbrella_evidence(prefix + EVIDENCE) == "lane_intake_budget"


def test_claim_and_pr_detection_helpers() -> None:
    assert umbrella.claim_ticket({}) is None
    assert umbrella.may_open_pr({}, "Read only") is False
    assert umbrella.may_open_pr(parts(repo="none,omnimarket"), "") is True
    assert (
        umbrella.may_open_pr(
            {}, "Before the first push, write contracts/OMN-21301.yaml"
        )
        is True
    )


def test_missing_overlay_names_the_setting() -> None:
    with pytest.raises(NodeOverlayError, match="umbrella_tickets"):
        umbrella.HandlerUmbrellaTicket().handle(
            ModelUmbrellaTicketRequest(
                tickets=("OMN-17427",), text="", opens_pr=True, environ={}
            )
        )


def test_overlay_resolves_ticket_set(tmp_path: Path) -> None:
    overlay = tmp_path / "node_umbrella_ticket_compute" / "overlay.yaml"
    overlay.parent.mkdir()
    overlay.write_text('umbrella_tickets: "OMN-17427, EXAMPLE-42"\n')
    environ = {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    assert umbrella.umbrella_tickets(environ) == frozenset({"OMN-17427", "EXAMPLE-42"})
    result = umbrella.HandlerUmbrellaTicket().handle(
        ModelUmbrellaTicketRequest(
            tickets=("EXAMPLE-42",), text="", opens_pr=True, environ=environ
        )
    )
    assert result.refusals
    assert "EXAMPLE-42" in result.refusals[0]
    assert umbrella.umbrella_tickets(
        {**environ, "ONEX_UMBRELLA_TICKETS": "ENV-1"}
    ) == frozenset({"ENV-1"})


@pytest.mark.parametrize(
    "value", ["", " ", "OMN-1,", "omn-1", "OMN", "OMN-1,not-a-ticket", "OMN-1\nOTHER-2"]
)
def test_empty_or_malformed_configuration_is_refused(value: str) -> None:
    with pytest.raises(NodeOverlayError, match="umbrella_tickets"):
        umbrella.umbrella_tickets({"ONEX_UMBRELLA_TICKETS": value})


@pytest.mark.parametrize("tickets", [(), ("bad",), ("OMN-1", "")])
def test_explicit_ticket_set_is_validated(tickets: tuple[str, ...]) -> None:
    with pytest.raises(NodeOverlayError, match="umbrella_tickets"):
        umbrella.HandlerUmbrellaTicket().handle(
            ModelUmbrellaTicketRequest(
                tickets=("OMN-1",),
                text="",
                opens_pr=True,
                umbrella_tickets=tickets,
                environ={},
            )
        )


def test_helpers_resolve_the_same_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    overlay = tmp_path / "node_umbrella_ticket_compute" / "overlay.yaml"
    overlay.parent.mkdir()
    overlay.write_text('umbrella_tickets: "OMN-42"\n')
    monkeypatch.delenv("ONEX_UMBRELLA_TICKETS", raising=False)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    assert umbrella.brief_refusals(parts("OMN-42"), "Fix", "Fix")
    assert umbrella.pr_refusal("OMN-42", "Body")
