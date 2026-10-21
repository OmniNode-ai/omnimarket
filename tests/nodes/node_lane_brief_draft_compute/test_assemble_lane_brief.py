# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The model writes the free-text slots; code fills every shell part and refuses a draft that wrote one."""

from __future__ import annotations

import pytest

from omnimarket.models.lane_brief_draft.enum_brief_draft_refusal import (
    EnumBriefDraftRefusal,
)
from omnimarket.models.lane_brief_draft.enum_brief_draft_slot import EnumBriefDraftSlot
from omnimarket.models.lane_brief_draft.model_brief_draft_slot import (
    ModelBriefDraftSlot,
)
from omnimarket.models.lane_brief_draft.model_brief_quoted_criteria import (
    ModelBriefQuotedCriteria,
)
from omnimarket.models.lane_brief_draft.model_brief_shell_parts import (
    ModelBriefShellParts,
)
from omnimarket.models.lane_brief_draft.model_brief_ticket_state_ack import (
    ModelBriefTicketStateAck,
)
from omnimarket.models.lane_brief_draft.model_lane_brief_assemble_request import (
    ModelLaneBriefAssembleRequest,
)
from omnimarket.nodes.node_lane_brief_draft_compute.handlers.handler_lane_brief_assemble import (
    HandlerLaneBriefAssemble,
)

pytestmark = pytest.mark.unit

CONSTRAINTS = (
    "## 3a. Constraints (pasted, never paraphrased)\n\n"
    "### 3a.1 Inbox (inbox.sh demo-lane, rc=0)\n\n```text\ninbox demo-lane: 0 open item(s)\n```\n"
)
RULES = "# Standing rules for every dispatched lane (read fully before the first tool call)\nAuthority: verify-authority-first.\n"
FACTS = "Ticket OMN-17427 is In Progress. The change touches omnimarket#4074 and one config file."


def _shell() -> ModelBriefShellParts:
    return ModelBriefShellParts(
        parent="orchestrator-bb1a",
        repo="omnimarket",
        model="sonnet",
        effort="high",
        ticket_state_acks=(
            ModelBriefTicketStateAck(ticket="OMN-17427", state="merged"),
        ),
        quoted_criteria=(
            ModelBriefQuotedCriteria(
                ticket="OMN-17427",
                lines=("- [ ] AC5: one saving -- falsifier: x", "- [ ] AC6: metering"),
            ),
        ),
        out_of_scope="OUT OF SCOPE: GitHub reads with gh; prod",
        rules_block=RULES,
        constraints_block=CONSTRAINTS,
    )


def _slots(
    title: str = "Wire the brief drafting class",
    premises: str = "| Premise | Source |\n| -- | -- |\n| The class is declared | OMN-17427 |",
    task: str = "Add the class to the routing contract and open one PR for omnimarket#4074.",
) -> tuple[ModelBriefDraftSlot, ...]:
    return (
        ModelBriefDraftSlot(slot=EnumBriefDraftSlot.TITLE, text=title),
        ModelBriefDraftSlot(slot=EnumBriefDraftSlot.PREMISES, text=premises),
        ModelBriefDraftSlot(slot=EnumBriefDraftSlot.TASK, text=task),
    )


def _request(
    slots: tuple[ModelBriefDraftSlot, ...] | None = None,
) -> ModelLaneBriefAssembleRequest:
    return ModelLaneBriefAssembleRequest(
        lane="demo-lane",
        ticket="OMN-17427",
        fact_table=FACTS,
        drafted_slots=slots if slots is not None else _slots(),
        shell=_shell(),
    )


def _refusals(request: ModelLaneBriefAssembleRequest) -> set[EnumBriefDraftRefusal]:
    result = HandlerLaneBriefAssemble().handle(request)
    assert result.brief == ""
    assert result.assembled is False
    return {issue.refusal for issue in result.issues}


def test_assembles_drafted_slots_around_code_filled_shell_parts() -> None:
    result = HandlerLaneBriefAssemble().handle(_request())
    assert result.assembled is True
    assert result.issues == ()
    brief = result.brief
    assert brief.startswith("# Lane demo-lane: Wire the brief drafting class\n")
    assert "ROUTE: model=sonnet effort=high\n" in brief
    assert (
        "Parent: orchestrator-bb1a. Ticket: OMN-17427. Repo: omnimarket. "
        "Model: sonnet. Effort: high.\n" in brief
    )
    # Every shell part lands byte for byte, in the order the checker reads them.
    assert "ticket-state-ack=OMN-17427:merged\n" in brief
    assert (
        "ticket-criteria: OMN-17427\n> - [ ] AC5: one saving -- falsifier: x\n"
        "> - [ ] AC6: metering\n" in brief
    )
    assert "OUT OF SCOPE: GitHub reads with gh; prod\n" in brief
    assert RULES in brief
    assert brief.endswith(CONSTRAINTS)
    assert brief.index("## 1. Premises") < brief.index("## 2. Task")
    assert brief.index("## 2. Task") < brief.index("# Standing rules")
    assert brief.index("# Standing rules") < brief.index("## 3a. Constraints")
    assert "The class is declared" in brief
    assert len(result.brief_sha256) == 64


def test_assembly_is_deterministic() -> None:
    first = HandlerLaneBriefAssemble().handle(_request())
    second = HandlerLaneBriefAssemble().handle(_request())
    assert first == second


@pytest.mark.parametrize(
    "marker",
    [
        "## 3a. Constraints (pasted, never paraphrased)",
        "### 3a.3 Landing",
        "ticket-state-ack=OMN-17427:done",
        "ticket-criteria: OMN-17427",
        "# Standing rules for every dispatched lane",
        "ROUTE: model=opus effort=max",
        "Parent: orchestrator-bb1a. Ticket: OMN-17427.",
        "OUT OF SCOPE: nothing",
        "consent=docs/tracking/ROLLING_WORK_LEDGER.md:1",
    ],
)
def test_refuses_a_draft_that_wrote_a_shell_part(marker: str) -> None:
    slots = _slots(task=f"Do the work.\n{marker}\nThen stop.")
    assert _refusals(_request(slots)) == {EnumBriefDraftRefusal.SHELL_PART_IN_DRAFT}


def test_refuses_an_identifier_the_fact_table_does_not_hold() -> None:
    slots = _slots(task="Fix OMN-99999 and then omnimarket#4074.")
    result = HandlerLaneBriefAssemble().handle(_request(slots))
    assert result.assembled is False
    assert [(issue.refusal, issue.detail) for issue in result.issues] == [
        (EnumBriefDraftRefusal.UNGROUNDED_IDENTIFIER, "OMN-99999")
    ]


def test_accepts_identifiers_the_fact_table_holds() -> None:
    slots = _slots(task="Work OMN-17427 against omnimarket#4074.")
    assert HandlerLaneBriefAssemble().handle(_request(slots)).assembled is True


def test_refuses_a_missing_empty_or_repeated_slot() -> None:
    no_task = _slots()[:2]
    assert _refusals(_request(no_task)) == {EnumBriefDraftRefusal.MISSING_SLOT}
    assert _refusals(_request(_slots(premises="   \n"))) == {
        EnumBriefDraftRefusal.EMPTY_SLOT
    }
    twice = (*_slots(), ModelBriefDraftSlot(slot=EnumBriefDraftSlot.TASK, text="more"))
    assert _refusals(_request(twice)) == {EnumBriefDraftRefusal.DUPLICATE_SLOT}


def test_refuses_a_title_over_one_line() -> None:
    assert _refusals(_request(_slots(title="Wire it\nand more"))) == {
        EnumBriefDraftRefusal.MULTILINE_TITLE
    }
