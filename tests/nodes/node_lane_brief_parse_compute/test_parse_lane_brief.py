# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed lane brief: column-zero metadata lines parse to a model, and a bad engine line names itself."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.models.lane_brief.enum_lane_engine import EnumLaneEngine
from omnimarket.models.lane_brief.model_lane_brief_parse_request import (
    ModelLaneBriefParseRequest,
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
from omnimarket.nodes.node_lane_brief_parse_compute.handlers.handler_lane_brief_parse import (
    HandlerLaneBriefParse,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
CONSTRAINTS = "## 3a. Constraints (pasted, never paraphrased)\n\n### 3a.1 Inbox\n"
RULES = "# Standing rules for every dispatched lane (read fully before the first tool call)\nAuthority.\n"
METADATA = (
    "Lane kind: ticket\n"
    "Required engine: claude-sonnet\n"
    "Required host: h105\n"
    "Opens PR in: omnimarket\n"
    "Proves: the parser reads the assembled briefs\n"
)


def _parse(brief: str):
    return HandlerLaneBriefParse().handle(
        ModelLaneBriefParseRequest(brief=brief, now=NOW)
    )


def _assembled(task: str) -> str:
    """A brief exactly as the assemble operation produces it."""
    result = HandlerLaneBriefAssemble().handle(
        ModelLaneBriefAssembleRequest(
            lane="demo-lane",
            ticket="OMN-17427",
            fact_table="Ticket OMN-17427 is In Progress.",
            drafted_slots=(
                ModelBriefDraftSlot(slot=EnumBriefDraftSlot.TITLE, text="Wire it"),
                ModelBriefDraftSlot(
                    slot=EnumBriefDraftSlot.PREMISES, text="A premise."
                ),
                ModelBriefDraftSlot(slot=EnumBriefDraftSlot.TASK, text=task),
            ),
            shell=ModelBriefShellParts(
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
                        lines=("- [ ] AC5: one saving -- falsifier: x",),
                    ),
                ),
                out_of_scope="OUT OF SCOPE: prod",
                rules_block=RULES,
                constraints_block=CONSTRAINTS,
            ),
        )
    )
    assert result.assembled, result.issues
    return result.brief


def test_parses_the_column_zero_metadata_lines() -> None:
    result = _parse(_assembled(f"Do the work.\n{METADATA}"))
    assert result.parsed is True
    assert result.issues == ()
    brief = result.brief
    assert brief is not None
    assert brief.task == "Wire it"
    assert brief.engine is EnumLaneEngine.CLAUDE_SONNET
    assert brief.required_host == "h105"
    assert brief.ticket == "OMN-17427"
    assert brief.opens_pr_in == "omnimarket"
    assert brief.proves == "the parser reads the assembled briefs"
    assert brief.quoted_criteria == (
        ModelBriefQuotedCriteria(
            ticket="OMN-17427", lines=("- [ ] AC5: one saving -- falsifier: x",)
        ),
    )
    assert result.parsed_at == NOW


@pytest.mark.parametrize(
    ("engine_line", "named"),
    [
        ("Required engine: whichever engine is free", "Required engine"),
        ("Required engine: claude, or codex if claude is down", "Required engine"),
        ("Required engine:", "Required engine"),
        (None, "Required engine"),
    ],
)
def test_missing_or_prose_engine_line_fails_and_names_the_line(
    engine_line: str | None, named: str
) -> None:
    metadata = "\n".join(
        line
        for line in METADATA.splitlines()
        if not line.startswith("Required engine:")
    )
    if engine_line is not None:
        metadata = f"{engine_line}\n{metadata}"
    result = _parse(_assembled(f"Do the work.\n{metadata}\n"))
    assert result.parsed is False
    assert result.brief is None
    assert [issue.line for issue in result.issues] == [named]
    assert result.issues[0].detail


def test_a_metadata_line_inside_a_code_fence_or_indented_is_not_read() -> None:
    task = (
        "Do the work.\n```text\nRequired engine: codex\n```\n  Required engine: glm\n"
    )
    result = _parse(_assembled(task))
    assert result.parsed is False
    assert result.issues[0].line == "Required engine"


def test_a_repeated_engine_line_fails() -> None:
    task = f"Do the work.\n{METADATA}Required engine: codex\n"
    result = _parse(_assembled(task))
    assert result.parsed is False
    assert result.issues[0].line == "Required engine"


def test_parse_is_deterministic() -> None:
    brief = _assembled(f"Do the work.\n{METADATA}")
    assert _parse(brief) == _parse(brief)
