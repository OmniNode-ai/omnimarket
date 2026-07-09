# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ClosePolicyGate — fail-closed durable-evidence gate for Backlog/Todo -> Done flips.

The OMN-13817 incident (beta-blocker B7 / W1.4): close-batch ``wf_1628d9a5``
flipped implementation tickets Backlog->Done with **no durable evidence** —
``startedAt=null``, zero attachments, zero PRs, sub-second bulk close. The Linear
board was corrupted as a completion signal because the auto-close path was able
to write a ``Done`` state without any proof of delivered work.

This gate is the single fail-closed chokepoint every auto Done flip must pass.
It refuses the transition unless the mutation carries a recognized *durable*
evidence kind (a merged PR, a superseding sibling PR, an all-children-done epic
roll-up, or a tracked OCC receipt) with a non-empty detail string. A close
attempt that carries no evidence — the ``wf_1628d9a5`` signature — is REFUSED,
not closed.

Doctrine (CLAUDE.md deterministic-truth): durable evidence (merged PR + green CI
+ OCC receipt bound to the ticket) is truth; a bare API status-write is not. The
gate is pure logic — no I/O — so it is deterministic and unit-testable without
network or git access.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EnumCloseEvidenceKind(StrEnum):
    """Durable-evidence kinds that authorize an auto Backlog/Todo -> Done flip.

    Each kind corresponds to a delivery proof the auto-close path has already
    assembled before it reaches the gate:

    * ``MERGED_IMPLEMENTING_PR`` — a PR that implements the ticket is MERGED.
    * ``SUPERSEDED_BY_MERGED_PR`` — the ticket's PR was closed unmerged but a
      sibling PR delivering the same work merged elsewhere.
    * ``ALL_CHILDREN_DONE`` — an epic/parent whose every child is Done (roll-up).
    * ``OCC_RECEIPT`` — a durable OCC receipt bound to the ticket on the
      governance ref (the ``node_dod_verify`` ``DurableEvidenceGate`` surface).
    * ``RUNTIME_OPS_READBACK`` — a tracked, independently-verified RUNTIME_OPS
      readback receipt for a no-source-change runtime-ops fix that has NO PR
      (OMN-14168). The auto-close path constructs this only after a fail-closed
      probe positively verifies the durable typed receipt; a Linear label alone
      never constructs it.
    """

    MERGED_IMPLEMENTING_PR = "merged_implementing_pr"
    SUPERSEDED_BY_MERGED_PR = "superseded_by_merged_pr"
    ALL_CHILDREN_DONE = "all_children_done"
    OCC_RECEIPT = "occ_receipt"
    RUNTIME_OPS_READBACK = "runtime_ops_readback"


class ModelCloseEvidence(BaseModel):
    """The durable evidence a Done flip carries.

    ``kind is None`` (or a blank ``detail``) is the no-evidence case the gate
    refuses. The auto-close path constructs this from the proof it gathered;
    a bare bulk-close automation that gathered nothing constructs ``None`` and
    is refused at the chokepoint.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EnumCloseEvidenceKind | None = None
    detail: str = ""


class ModelCloseDecision(BaseModel):
    """The gate verdict for a single close attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed: bool
    reason: str


class CloseEvidenceRefusedError(Exception):
    """Raised when the durable-evidence gate refuses a Done flip.

    Attributes:
        ticket_id: The Linear identifier the close was attempted for.
        decision: The structured refusal verdict (``allowed=False`` + reason).
    """

    def __init__(self, ticket_id: str, decision: ModelCloseDecision) -> None:
        self.ticket_id = ticket_id
        self.decision = decision
        super().__init__(f"Refusing Done flip for {ticket_id}: {decision.reason}")


def evaluate_close_evidence(evidence: ModelCloseEvidence | None) -> ModelCloseDecision:
    """Return the gate verdict for a single close attempt. Pure function — no I/O.

    ALLOW only when a recognized durable evidence ``kind`` is present with a
    non-empty ``detail``. Everything else — no evidence object, ``kind is None``,
    or a blank ``detail`` — is REFUSED. This is the ``wf_1628d9a5`` signature
    (``startedAt=null`` + zero attachments -> no evidence assembled).
    """
    if evidence is None or evidence.kind is None:
        return ModelCloseDecision(
            allowed=False,
            reason=(
                "no durable evidence for the Done flip — a merged PR, a "
                "superseding sibling PR, an all-children-done roll-up, or a "
                "tracked OCC receipt is required. A bare status-write is refused "
                "(OMN-13817)."
            ),
        )
    if not evidence.detail.strip():
        return ModelCloseDecision(
            allowed=False,
            reason=(
                f"durable evidence kind {evidence.kind.value!r} declared but its "
                "detail is empty — the evidence must cite the actual PR / receipt "
                "/ child set before the Done flip (OMN-13817)."
            ),
        )
    return ModelCloseDecision(
        allowed=True,
        reason=f"durable evidence [{evidence.kind.value}]: {evidence.detail}",
    )


def enforce_close_evidence(
    *, ticket_id: str, evidence: ModelCloseEvidence | None
) -> ModelCloseDecision:
    """Run :func:`evaluate_close_evidence` and raise on refusal.

    On refusal raises :class:`CloseEvidenceRefusedError` carrying the structured
    verdict. On success returns the ALLOW verdict. Pure logic — no I/O.
    """
    decision = evaluate_close_evidence(evidence)
    if not decision.allowed:
        raise CloseEvidenceRefusedError(ticket_id, decision)
    return decision


__all__: list[str] = [
    "CloseEvidenceRefusedError",
    "EnumCloseEvidenceKind",
    "ModelCloseDecision",
    "ModelCloseEvidence",
    "enforce_close_evidence",
    "evaluate_close_evidence",
]
