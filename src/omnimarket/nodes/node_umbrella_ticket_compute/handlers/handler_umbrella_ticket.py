# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An umbrella-titled PR has no evidence of its own (OMN-21301).

The repo evidence gate (omnibase_core receipt-gate.yml) reads every OMN id in a PR title and needs contracts/<id>.yaml
at the head with dod_evidence of its own. A PR titled with an umbrella ticket shares that one contract with every other
umbrella PR, so its title gives it none: 10 of the 29 open PRs red on dod-verify on 2026-10-22 were titled OMN-17427.

This is the one definition of the umbrella ticket set and of the ``Umbrella evidence: <reason>`` line, read by
``lane_brief.py check`` (exit 3), the remote-lane runner before placement and CLAIM (``umbrella-refused``, exit 2) and
``landing_text.py guard-create`` (exit 3). A lane that may open or push a PR mints a child ticket in the launching
session first (a headless lab lane cannot mint, OMN-21021). On a day the create gate's lane intake budget is spent it
carries ``Umbrella evidence: lane_intake_budget`` instead, and adds its own requirement and dod_evidence item to the
umbrella's contract.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from omnimarket.handlers.node_overlay_reader import NodeOverlayError, resolve_setting
from omnimarket.nodes.node_umbrella_ticket_compute.models.model_umbrella_ticket import (
    ModelUmbrellaTicketRequest,
    ModelUmbrellaTicketResult,
)

EVIDENCE_PREFIX = "Umbrella evidence:"
BUDGET_REASON = "lane_intake_budget"  # the create gate's refusal when the day's lane intake budget is spent
# A line of its own, optionally a list item; a placeholder reason (`<reason>`) or a prose mention is no evidence.
EVIDENCE_RE = re.compile(
    rf"^[ \t]*(?:[-*] )?{re.escape(EVIDENCE_PREFIX)}[ \t]*(?P<reason>[^\s<`][^\n]*?)[ \t]*$",
    re.MULTILINE,
)
CLAIM_HEADING = "### 3a.8 CLAIM template"
CLAIM_TICKET_RE = re.compile(r"\| ticket=(OMN-[0-9]+) \|")
CLAIM_REPO_RE = re.compile(r"\| repo=([^|\s]+)")
NO_REPO: frozenset[str] = frozenset({"none", "-", "n/a", "<repo>"})
# lane_rules_block.CONTRACT_STEP's opening words, as a line of the brief's own prose.
CONTRACT_STEP_RE = re.compile(
    r"^[ \t]*(?:[-*] )?Before the first push, write contracts/OMN-[0-9]+\.yaml",
    re.MULTILINE,
)
WHY = (
    "an umbrella-titled PR shares one contract with every other umbrella PR, so the repo evidence gate finds no "
    "dod_evidence of its own for it (OMN-21301)"
)
LANE_FIX = (
    "mint a child ticket with /omni:create-ticket (parent {umbrella}) in the launching session and pass it with "
    "--ticket; when the create gate refuses with "
    + BUDGET_REASON
    + ", carry the line `"
    + EVIDENCE_PREFIX
    + " "
    + BUDGET_REASON
    + "` in the brief and have the lane add its own requirement and dod_evidence item to "
    "contracts/{umbrella}.yaml with a test-side path that fails at the merge base"
)
PR_FIX = (
    "title the PR with the lane's own child ticket; when the create gate refused that mint with "
    + BUDGET_REASON
    + ", carry a line `"
    + EVIDENCE_PREFIX
    + " <reason>` in the body and add the PR's own requirement and "
    "dod_evidence item to contracts/{umbrella}.yaml with a test-side path that fails at the merge base"
)


def umbrella_evidence(text: str) -> str | None:
    """The reason of the text's ``Umbrella evidence: <reason>`` line, or None when it carries none."""
    m = EVIDENCE_RE.search(text)
    return m.group("reason") if m else None


def claim_ticket(parts: Mapping[str, tuple[str, str]]) -> str | None:
    """The ticket of the 3a.8 CLAIM template: the lane's own ticket."""
    m = CLAIM_TICKET_RE.search(parts.get(CLAIM_HEADING, ("", ""))[1])
    return m.group(1) if m else None


def may_open_pr(parts: Mapping[str, tuple[str, str]], prose: str) -> bool:
    """A brief whose 3a.8 CLAIM names a repo, or whose own prose carries the contract-before-push step."""
    repos = {
        r.strip()
        for cell in CLAIM_REPO_RE.findall(parts.get(CLAIM_HEADING, ("", ""))[1])
        for r in cell.split(",")
    }
    return bool(repos - NO_REPO - {""}) or bool(CONTRACT_STEP_RE.search(prose))


def brief_refusals(
    parts: Mapping[str, tuple[str, str]],
    brief: str,
    prose: str,
    *,
    umbrella_tickets: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """``lane_brief.py check``: the refusal of a brief whose CLAIM ticket is an umbrella ticket on a PR-opening lane."""
    ticket = claim_ticket(parts)
    request = ModelUmbrellaTicketRequest(
        tickets=(ticket,) if ticket else (),
        text=brief,
        opens_pr=may_open_pr(parts, prose),
        umbrella_tickets=tuple(umbrella_tickets)
        if umbrella_tickets is not None
        else None,
    )
    return HandlerUmbrellaTicket().handle(request).refusals


def pr_refusal(
    ticket: str, body: str, *, umbrella_tickets: Sequence[str] | None = None
) -> str:
    """``landing_text.py guard-create``: the refusal of a PR whose only title ticket is an umbrella ticket, or ""."""
    return "".join(
        HandlerUmbrellaTicket()
        .handle(
            ModelUmbrellaTicketRequest(
                tickets=(ticket,),
                text=body,
                opens_pr=True,
                surface="pr",
                umbrella_tickets=tuple(umbrella_tickets)
                if umbrella_tickets is not None
                else None,
            )
        )
        .refusals
    )


def _validated_tickets(values: Sequence[str]) -> frozenset[str]:
    if not values or any(
        re.fullmatch(r"[A-Z][A-Z0-9]*-[0-9]+", ticket) is None for ticket in values
    ):
        raise NodeOverlayError(
            "node_umbrella_ticket_compute: invalid umbrella_tickets; expected a non-empty comma-separated list of ticket ids"
        )
    return frozenset(values)


def umbrella_tickets(environ: Mapping[str, str] | None = None) -> frozenset[str]:
    value = resolve_setting(
        "node_umbrella_ticket_compute",
        "umbrella_tickets",
        fields=frozenset({"umbrella_tickets"}),
        env_vars=("ONEX_UMBRELLA_TICKETS",),
        environ=environ,
    )
    return _validated_tickets(tuple(ticket.strip() for ticket in value.split(",")))


class HandlerUmbrellaTicket:
    def handle(self, request: ModelUmbrellaTicketRequest) -> ModelUmbrellaTicketResult:
        configured = (
            umbrella_tickets(request.environ)
            if request.umbrella_tickets is None
            else _validated_tickets(request.umbrella_tickets)
        )
        umbrella = sorted(t for t in dict.fromkeys(request.tickets) if t in configured)
        if (
            not request.opens_pr
            or not umbrella
            or umbrella_evidence(request.text) is not None
        ):
            return ModelUmbrellaTicketResult(refusals=())
        ticket = umbrella[0]
        if request.surface == "pr":
            what = f"the title's only ticket {ticket} is an umbrella ticket"
            fix = PR_FIX.format(umbrella=ticket)
        else:
            what = f"the lane's ticket {ticket} is an umbrella ticket and the lane may open or push a PR"
            fix = LANE_FIX.format(umbrella=ticket)
        return ModelUmbrellaTicketResult(refusals=(f"{what}: {WHY}; {fix}",))
