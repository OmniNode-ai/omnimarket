# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Gate-escape audit — post-hoc L3 layer of the done-flip durable-evidence gate.

Design: ``docs/plans/2026-07-02-done-flip-durable-evidence-gate-design.md`` §2 (L3).

The PreToolUse hook (L1) is client-side and currently disabled (OMN-13244
baseline); the node-path gate (L2, OMN-13817) only covers
``node_linear_triage``. Any other Linear client — including the foreground
session write that produced the wf_1628d9a5 incident — can still flip a
ticket to Done with zero durable evidence. This module implements the
post-hoc audit: it does not block a close (it cannot — the write already
happened), it flags the ``wf_1628d9a5`` signature after the fact so a human
or a follow-up automation can react.

Signature (the ``wf_1628d9a5`` fingerprint): a Done ticket with
``startedAt=null`` AND zero recorded attachments/documents AND no merged PR
discoverable anywhere in the org (``gh search prs``). All three must hold —
any one piece of durable evidence clears the ticket.

Carve-outs (encoded explicitly per design §2 — never inferred from free text):

1. Cancel-class states — a human decision, exempt by design.
2. ``ALL_CHILDREN_DONE`` — an epic/parent roll-up when every child is Done.
3. A merged PR referencing the ticket id anywhere in the org. The audit's
   only signal here is ``gh search prs <ticket_id> --state merged`` returning
   a hit — it cannot distinguish "this PR implements the ticket" from "this
   PR superseded it via a merged sibling" without parsing PR bodies across
   every repo, so both of the design's ``MERGED_IMPLEMENTING_PR`` /
   ``SUPERSEDED_BY_MERGED_PR`` kinds collapse into one carve-out here:
   ``MERGED_PR_EVIDENCE``.
4. Decision-only tickets — must carry an explicit label (never free text).

This module is split into pure logic (:func:`evaluate_gate_escape`, fully
unit-testable, no I/O) and an I/O boundary (:class:`LinearAuditClientProtocol`)
that the handler injects, matching the constructor-injectable-seam pattern
already used for the ``gh``-backed checks in
``handler_dod_sweep_orchestrator.py`` (OMN-13783).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

# Mirrors omniclaude's plugins/onex/hooks/lib/linear_done_verify.py CANCEL_STATES —
# duplicated here (not imported) because it lives in a different repo and this
# node has no cross-repo import path to it.
CANCEL_STATES: frozenset[str] = frozenset(
    {"canceled", "cancelled", "duplicate", "won't do", "wont do"}
)

# Explicit decision-only exemption labels. A ticket must carry one of these
# labels verbatim — free-text justification in a description never exempts it.
DECISION_ONLY_LABELS: frozenset[str] = frozenset({"decision-only", "close-if-done"})

# States that count as "done" for children when evaluating ALL_CHILDREN_DONE.
_CHILD_DONE_STATES: frozenset[str] = frozenset({"done", "cancelled", "canceled"})


class EnumGateEscapeCarveOut(StrEnum):
    """Legitimate no-evidence-required close reasons (design §2 carve-out list)."""

    CANCEL_STATE = "cancel_state"
    ALL_CHILDREN_DONE = "all_children_done"
    MERGED_PR_EVIDENCE = "merged_pr_evidence"
    DECISION_ONLY_LABEL = "decision_only_label"


class ModelGateEscapeTicketSnapshot(BaseModel):
    """The subset of a Done Linear ticket's state needed to evaluate the audit.

    Populated by :class:`LinearAuditClientProtocol` implementations from a
    live Linear query, or by test fixtures for deterministic evaluation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(description="Linear internal UUID.")
    identifier: str = Field(description="Human ticket id, e.g. OMN-13854.")
    title: str = Field(default="")
    state_name: str = Field(default="", description="Current workflow state name.")
    started_at: str | None = Field(
        default=None,
        description="ISO timestamp the ticket entered a started state, or None.",
    )
    completed_at: str | None = Field(default=None)
    labels: tuple[str, ...] = Field(default=())
    attachments_count: int = Field(default=0)
    documents_count: int = Field(
        default=0,
        description=(
            "Reserved for parity with the raw ticket payload shape (which "
            "carries a separate documents[] array). Linear's public GraphQL "
            "schema exposes no issue-level documents connection distinct from "
            "attachments, so the real client always reports 0 here; kept as a "
            "field so a future real signal — or a test fixture — can populate it."
        ),
    )
    has_children: bool = Field(default=False)
    all_children_done: bool = Field(default=False)


class ModelGateEscapeFinding(BaseModel):
    """The audit verdict for a single Done ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(description="Human ticket id, e.g. OMN-13797.")
    flagged: bool = Field(description="True when the wf_1628d9a5 signature is present.")
    carve_out: EnumGateEscapeCarveOut | None = Field(
        default=None,
        description="Which legitimate carve-out cleared the ticket, if any.",
    )
    reason: str = Field(default="")
    started_at: str | None = Field(default=None)
    attachments_count: int = Field(default=0)
    documents_count: int = Field(default=0)
    merged_pr_found: bool = Field(default=False)


def evaluate_gate_escape(
    ticket: ModelGateEscapeTicketSnapshot,
    *,
    merged_pr_found: bool,
) -> ModelGateEscapeFinding:
    """Return the audit verdict for one Done ticket. Pure function — no I/O.

    Carve-outs are checked first, in the order listed in the design doc.
    Only when none apply is the bulk-fabrication signature evaluated:
    ``startedAt=null`` AND zero attachments AND zero documents.
    ``merged_pr_found`` is resolved by the caller (a ``gh search prs`` lookup)
    since network/subprocess access does not belong in pure logic.
    """
    state_lc = ticket.state_name.strip().lower()
    if state_lc in CANCEL_STATES:
        return _cleared(
            ticket,
            EnumGateEscapeCarveOut.CANCEL_STATE,
            "cancel-class state is a human decision, exempt by design",
            merged_pr_found=merged_pr_found,
        )

    if ticket.has_children and ticket.all_children_done:
        return _cleared(
            ticket,
            EnumGateEscapeCarveOut.ALL_CHILDREN_DONE,
            "epic/parent roll-up — every child ticket is Done",
            merged_pr_found=merged_pr_found,
        )

    label_set = {label.strip().lower() for label in ticket.labels}
    if label_set & DECISION_ONLY_LABELS:
        return _cleared(
            ticket,
            EnumGateEscapeCarveOut.DECISION_ONLY_LABEL,
            "explicitly labeled decision-only ticket",
            merged_pr_found=merged_pr_found,
        )

    if merged_pr_found:
        return _cleared(
            ticket,
            EnumGateEscapeCarveOut.MERGED_PR_EVIDENCE,
            "a merged PR referencing this ticket id was found via gh search prs",
            merged_pr_found=merged_pr_found,
        )

    no_started = ticket.started_at is None or not ticket.started_at.strip()
    no_artifacts = ticket.attachments_count == 0 and ticket.documents_count == 0
    if no_started and no_artifacts:
        return ModelGateEscapeFinding(
            ticket_id=ticket.identifier,
            flagged=True,
            carve_out=None,
            reason=(
                "startedAt=null + zero attachments + zero documents + no merged "
                "PR discoverable via gh search prs — gate-escape candidate "
                "(wf_1628d9a5 signature)"
            ),
            started_at=ticket.started_at,
            attachments_count=ticket.attachments_count,
            documents_count=ticket.documents_count,
            merged_pr_found=merged_pr_found,
        )

    return ModelGateEscapeFinding(
        ticket_id=ticket.identifier,
        flagged=False,
        carve_out=None,
        reason="durable evidence present (startedAt set, or attachments/documents recorded)",
        started_at=ticket.started_at,
        attachments_count=ticket.attachments_count,
        documents_count=ticket.documents_count,
        merged_pr_found=merged_pr_found,
    )


def _cleared(
    ticket: ModelGateEscapeTicketSnapshot,
    carve_out: EnumGateEscapeCarveOut,
    reason: str,
    *,
    merged_pr_found: bool,
) -> ModelGateEscapeFinding:
    return ModelGateEscapeFinding(
        ticket_id=ticket.identifier,
        flagged=False,
        carve_out=carve_out,
        reason=reason,
        started_at=ticket.started_at,
        attachments_count=ticket.attachments_count,
        documents_count=ticket.documents_count,
        merged_pr_found=merged_pr_found,
    )


def compute_child_done_rollup(child_states: tuple[str, ...]) -> bool:
    """Return True when every child state name counts as done/cancelled.

    An empty ``child_states`` tuple (no children) returns False — the
    ALL_CHILDREN_DONE carve-out only applies to tickets that actually have
    children (see ``has_children`` gating in :func:`evaluate_gate_escape`).
    """
    if not child_states:
        return False
    return all(name.strip().lower() in _CHILD_DONE_STATES for name in child_states)


@runtime_checkable
class LinearAuditClientProtocol(Protocol):
    """I/O boundary for the gate-escape audit — injectable for testing."""

    def list_done_tickets(
        self, *, team: str, since: str, until: str
    ) -> tuple[ModelGateEscapeTicketSnapshot, ...]: ...

    def post_comment(self, *, issue_id: str, body: str) -> None: ...


__all__ = [
    "CANCEL_STATES",
    "DECISION_ONLY_LABELS",
    "EnumGateEscapeCarveOut",
    "LinearAuditClientProtocol",
    "ModelGateEscapeFinding",
    "ModelGateEscapeTicketSnapshot",
    "compute_child_done_rollup",
    "evaluate_gate_escape",
]
