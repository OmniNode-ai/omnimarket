# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Turn one ticket's evidence into the closer's decision: the verdict, the action and the comment it writes.

The action follows the verdict and the assignee: an owner's eligible ticket is set Done, a collaborator's eligible
ticket gets an evidence comment and keeps its state, a ticket left gets a comment naming what is missing. The
fingerprint covers the verdict and the reasons (not the actor or the time), so a scheduled run writes no second
comment while nothing has changed.
"""

from __future__ import annotations

import hashlib

from omnimarket.nodes.node_sprint_closer_effect.handlers.handler_sprint_closer_eligibility import (
    decide_eligibility,
)
from omnimarket.nodes.node_sprint_closer_effect.models.model_sprint_closer import (
    EnumCloserAction,
    EnumCloserVerdict,
    ModelEligibility,
    ModelTicketDecision,
    ModelTicketEvidence,
)

FINGERPRINT_PREFIX = "closer-fingerprint: "


def fingerprint(eligibility: ModelEligibility, action: EnumCloserAction) -> str:
    text = "\n".join(
        [
            action.value,
            eligibility.verdict.value,
            *sorted(eligibility.missing),
            *sorted(eligibility.evidence),
        ]
    )
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def render_comment(
    actor: str,
    ev: ModelTicketEvidence,
    el: ModelEligibility,
    action: EnumCloserAction,
    fp: str,
) -> str:
    head = {
        EnumCloserAction.SET_DONE: "Set Done: every acceptance criterion has a passing check or reading.",
        EnumCloserAction.COMMENT_EVIDENCE: (
            "Every acceptance criterion has a passing check or reading. This ticket is assigned to "
            f"{ev.assignee or 'a collaborator'}, so its state is left for them."
        ),
        EnumCloserAction.COMMENT_MISSING: "Left open: not every acceptance criterion has a passing check or reading.",
        EnumCloserAction.NO_CHANGE: "No change.",
    }[action]
    lines = [f"actor: {actor}", "", head]
    if el.evidence:
        lines += ["", "Passing checks and readings:", *[f"- {e}" for e in el.evidence]]
    if el.missing:
        lines += ["", "Missing:", *[f"- {m}" for m in el.missing]]
    lines += ["", f"{FINGERPRINT_PREFIX}{fp}"]
    return "\n".join(lines)


class HandlerSprintCloser:
    """handle(evidence) -> decision, given the actor line and the last fingerprint the closer wrote on the ticket."""

    def __init__(self, actor: str, last_fingerprint: str | None = None) -> None:
        self._actor = actor
        self._last = last_fingerprint

    def handle(self, request: ModelTicketEvidence) -> ModelTicketDecision:
        el = decide_eligibility(request)
        if el.verdict is EnumCloserVerdict.CLOSE:
            action = (
                EnumCloserAction.COMMENT_EVIDENCE
                if request.is_collaborator
                else EnumCloserAction.SET_DONE
            )
        else:
            action = EnumCloserAction.COMMENT_MISSING
        fp = fingerprint(el, action)
        if fp == self._last and action is not EnumCloserAction.SET_DONE:
            return ModelTicketDecision(
                ticket_id=request.ticket_id,
                eligibility=el,
                action=EnumCloserAction.NO_CHANGE,
                fingerprint=fp,
            )
        return ModelTicketDecision(
            ticket_id=request.ticket_id,
            eligibility=el,
            action=action,
            fingerprint=fp,
            comment=render_comment(self._actor, request, el, action, fp),
        )
