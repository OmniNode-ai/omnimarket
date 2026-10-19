# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Assemble a lane brief from the model's free-text slots and code-filled shell parts.

The model drafts a title, the premises prose and the task. Everything the brief checker reads as a
fixed form is filled here from the request, byte for byte: the header, the ticket-state
acknowledgements, the quoted criteria, the out-of-scope line, the standing rules and the 3a block.
A draft that wrote one of those, or states an identifier its fact table does not hold, is refused
before the checker sees it. Assembling is not accepting: the checker's result goes to
``HandlerLaneBriefGate``.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter

from omnimarket.models.lane_brief_draft.enum_brief_draft_refusal import (
    EnumBriefDraftRefusal,
)
from omnimarket.models.lane_brief_draft.enum_brief_draft_slot import EnumBriefDraftSlot
from omnimarket.models.lane_brief_draft.model_brief_draft_issue import (
    ModelBriefDraftIssue,
)
from omnimarket.models.lane_brief_draft.model_brief_shell_parts import (
    ModelBriefShellParts,
)
from omnimarket.models.lane_brief_draft.model_lane_brief_assemble_request import (
    ModelLaneBriefAssembleRequest,
)
from omnimarket.models.lane_brief_draft.model_lane_brief_assemble_result import (
    ModelLaneBriefAssembleResult,
)

# A line of a draft that opens one of the fixed forms code fills.
_SHELL_LINE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*3a\b|ticket-criteria:|ROUTE:|Parent:|OUT OF SCOPE:"
    r"|# Standing rules for every dispatched lane)",
    re.MULTILINE,
)
_SHELL_TOKEN = re.compile(r"ticket-state-ack=|consent=")
_IDENTIFIER = re.compile(r"\bOMN-[0-9]+\b|\b[A-Za-z][A-Za-z0-9_-]*#[0-9]+\b")


def _identifiers(text: str) -> set[str]:
    return set(_IDENTIFIER.findall(text))


def _shell_offenders(text: str) -> list[str]:
    found = [m.group(0).strip() for m in _SHELL_LINE.finditer(text)]
    found.extend(m.group(0) for m in _SHELL_TOKEN.finditer(text))
    return found


def _render(
    request: ModelLaneBriefAssembleRequest, slots: dict[EnumBriefDraftSlot, str]
) -> str:
    shell: ModelBriefShellParts = request.shell
    lines = [
        f"# Lane {request.lane}: {slots[EnumBriefDraftSlot.TITLE]}",
        "",
        f"ROUTE: model={shell.model} effort={shell.effort}",
        f"Parent: {shell.parent}. Ticket: {request.ticket}. Repo: {shell.repo}. "
        f"Model: {shell.model}. Effort: {shell.effort}.",
        "",
        "## 1. Premises",
        "",
        slots[EnumBriefDraftSlot.PREMISES],
        "",
    ]
    lines.extend(
        f"ticket-state-ack={ack.ticket}:{ack.state}" for ack in shell.ticket_state_acks
    )
    for criteria in shell.quoted_criteria:
        lines.append(f"ticket-criteria: {criteria.ticket}")
        lines.extend(f"> {line}" for line in criteria.lines)
    lines.extend(
        ["", "## 2. Task", "", slots[EnumBriefDraftSlot.TASK], "", shell.out_of_scope]
    )
    head = "\n".join(lines).rstrip("\n")
    return f"{head}\n{shell.rules_block.rstrip(chr(10))}\n{shell.constraints_block}"


class HandlerLaneBriefAssemble:
    """Pure: reads no file, calls no model and reads no clock."""

    def handle(
        self, request: ModelLaneBriefAssembleRequest
    ) -> ModelLaneBriefAssembleResult:
        issues: list[ModelBriefDraftIssue] = []
        counts = Counter(s.slot for s in request.drafted_slots)
        slots: dict[EnumBriefDraftSlot, str] = {}
        for slot in EnumBriefDraftSlot:
            if counts[slot] == 0:
                issues.append(
                    ModelBriefDraftIssue(
                        refusal=EnumBriefDraftRefusal.MISSING_SLOT, slot=slot.value
                    )
                )
            elif counts[slot] > 1:
                issues.append(
                    ModelBriefDraftIssue(
                        refusal=EnumBriefDraftRefusal.DUPLICATE_SLOT, slot=slot.value
                    )
                )
        for drafted in request.drafted_slots:
            text = drafted.text.strip()
            name = drafted.slot.value
            if not text:
                issues.append(
                    ModelBriefDraftIssue(
                        refusal=EnumBriefDraftRefusal.EMPTY_SLOT, slot=name
                    )
                )
                continue
            if drafted.slot is EnumBriefDraftSlot.TITLE and "\n" in text:
                issues.append(
                    ModelBriefDraftIssue(
                        refusal=EnumBriefDraftRefusal.MULTILINE_TITLE, slot=name
                    )
                )
            issues.extend(
                ModelBriefDraftIssue(
                    refusal=EnumBriefDraftRefusal.SHELL_PART_IN_DRAFT,
                    slot=name,
                    detail=offender,
                )
                for offender in _shell_offenders(text)
            )
            known = _identifiers(request.fact_table) | {request.ticket}
            issues.extend(
                ModelBriefDraftIssue(
                    refusal=EnumBriefDraftRefusal.UNGROUNDED_IDENTIFIER,
                    slot=name,
                    detail=identifier,
                )
                for identifier in sorted(_identifiers(text) - known)
            )
            slots.setdefault(drafted.slot, text)
        if issues:
            return ModelLaneBriefAssembleResult(assembled=False, issues=tuple(issues))
        brief = _render(request, slots)
        return ModelLaneBriefAssembleResult(
            assembled=True,
            brief=brief,
            brief_sha256=hashlib.sha256(brief.encode()).hexdigest(),
        )
