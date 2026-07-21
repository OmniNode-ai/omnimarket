# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for the fail-closed completion reconciler (OMN-14915).

The completion reconciler is the REVERSE-path counterpart to the forward
``close_evidence_gate``. The forward gate refuses to *write* Done without durable
evidence on node_linear_triage's auto-close path. But Linear's native
``autoCloseChildIssues`` cascade (enabled on team OMN) writes Done server-side —
recursively closing a completed parent's descendant closure — without ever
executing a single line of OmniNode code, so the forward gate never sees it.

These models describe an *already-completed* ticket and the reconciler's
fail-closed verdict over it. ``ModelCompletionFacts`` is assembled by the
out-of-band live probe (query recent completions, detect same-second clusters,
resolve merged-PR / OCC-receipt evidence) and handed to the pure decision core.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_linear_triage.services.close_evidence_gate import (
    ModelCloseEvidence,
)


class EnumCompletionVerdict(StrEnum):
    """The reconciler verdict for a single already-completed ticket.

    * ``KEEP`` — the completion carries a recognized durable-evidence kind; it is
      legitimately Done and is left untouched.
    * ``REVERT_REQUIRED`` — no durable evidence AND the Linear cascade fingerprint
      (a same-second sibling/parent batch). The completion is an un-gated
      server-side cascade flip and must be reverted to its prior state.
    * ``FLAG_FOR_REVIEW`` — no durable evidence but no cascade fingerprint (a
      possible manual/legacy Done). It is never silently KEPT — a human must
      adjudicate it. This keeps auto-revert precise (cascade population only)
      while still failing closed on every evidence-less completion.
    """

    KEEP = "keep"
    FLAG_FOR_REVIEW = "flag_for_review"
    REVERT_REQUIRED = "revert_required"


class ModelCompletionFacts(BaseModel):
    """Facts about one already-completed ticket, assembled by the live probe.

    The reconciler decision is pure over these facts. ``evidence`` is the durable
    close evidence the probe could resolve (a merged implementing PR, an
    all-children-done roll-up, a tracked OCC receipt, ...) or ``None`` when it
    resolved none. ``evidence_probe_ok`` is ``False`` when the probe itself was
    indeterminate/unreadable — the decision then treats evidence as ABSENT
    (fail-closed), never upgrading it to ALLOW.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Human identifier, e.g. "OMN-14900" — used for logging and the revert comment.
    ticket_id: str
    # Linear internal UUID — required by the revert mutation (save_issue(issue_id=...)).
    linear_id: str = ""
    # State to revert to on REVERT_REQUIRED (from the pre-Done IssueHistory row).
    prior_state_name: str = "Backlog"
    # Whether startedAt was non-null (the ticket was actually worked).
    started: bool = False
    # >=2 siblings completed in the same second under a shared parent (cascade fingerprint).
    same_second_sibling_cluster: bool = False
    # A completed parent transitioned in the same 1s window (cascade fingerprint).
    parent_completed_same_second: bool = False
    # Durable close evidence the probe resolved, or None.
    evidence: ModelCloseEvidence | None = None
    # False => probe indeterminate/unreadable; decision fails closed (treats as no evidence).
    evidence_probe_ok: bool = True


class ModelCompletionVerdictResult(BaseModel):
    """The reconciler verdict for one completed ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str
    linear_id: str = ""
    verdict: EnumCompletionVerdict
    reason: str
    evidence_allowed: bool
    prior_state_name: str = "Backlog"
    cascade_fingerprint: bool = False


class ModelCompletionReconcileReport(BaseModel):
    """Aggregate report over a reconciled batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    results: list[ModelCompletionVerdictResult] = Field(default_factory=list)
    keep_count: int = 0
    flag_count: int = 0
    revert_count: int = 0
    # True only when apply_reverts actually mutated Linear (never in dry-run).
    applied: bool = False
    reverted_ticket_ids: list[str] = Field(default_factory=list)


class ModelCompletionReconcileStartCommand(BaseModel):
    """Dispatch input for HandlerCompletionReconcile — a batch of pre-assembled facts.

    The ``handle`` path is a pure decision (no mutation regardless of this
    command). The actual revert is a separate, explicitly-guarded call
    (``apply_reverts``) so shipping/dispatching the node can never mutate the
    board on its own.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = ""
    completions: list[ModelCompletionFacts] = Field(default_factory=list)


__all__ = [
    "EnumCompletionVerdict",
    "ModelCompletionFacts",
    "ModelCompletionReconcileReport",
    "ModelCompletionReconcileStartCommand",
    "ModelCompletionVerdictResult",
]
