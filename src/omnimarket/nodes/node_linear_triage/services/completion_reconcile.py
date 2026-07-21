# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fail-closed completion reconciler decision core (OMN-14915).

Forward gate vs. reverse gate
-----------------------------
``close_evidence_gate.enforce_close_evidence`` (OMN-13817) fails closed on
node_linear_triage's *forward* auto-close path: it refuses to WRITE ``Done``
without a recognized durable-evidence kind. But Linear's native
``autoCloseChildIssues`` cascade — enabled on team OMN — writes ``Done``
server-side (recursively closing a completed parent's descendant closure)
without ever calling that path, so the forward gate cannot see it. On
2026-07-21T21:07:25Z that cascade flipped OMN-14889 → 14900/14901/14902/14903 in
a 611ms server-side batch with zero acceptance criteria met.

This module is the *reverse* gate: given a ticket that is ALREADY ``Done``, it
decides — fail closed — whether the completion is legitimately evidenced
(``KEEP``), an evidence-less cascade flip that must be reverted
(``REVERT_REQUIRED``), or an evidence-less non-cascade completion a human must
adjudicate (``FLAG_FOR_REVIEW``). It REUSES ``evaluate_close_evidence`` so there
is exactly one definition of "durable evidence" in both directions — this is not
a second evidence checker.

Pure logic — no I/O. The live probe that assembles ``ModelCompletionFacts`` from
Linear/gh and the scheduled apply live in the effect layer (OMN-14915 remainder).
"""

from __future__ import annotations

from omnimarket.nodes.node_linear_triage.models.model_completion_reconcile import (
    EnumCompletionVerdict,
    ModelCompletionFacts,
    ModelCompletionVerdictResult,
)
from omnimarket.nodes.node_linear_triage.services.close_evidence_gate import (
    evaluate_close_evidence,
)


def evaluate_completion(facts: ModelCompletionFacts) -> ModelCompletionVerdictResult:
    """Return the fail-closed reconciliation verdict for one completed ticket.

    Fail-closed rule: a completion whose durable evidence is ABSENT or cannot be
    READ is NEVER ``KEEP``. An indeterminate/unreadable probe
    (``evidence_probe_ok is False``) is treated as no evidence, so it can never
    upgrade to ALLOW — the exact "optional check that silently skips == no check"
    failure class this gate exists to prevent.

    Precision: only the Linear cascade fingerprint (a same-second sibling/parent
    batch) escalates an evidence-less completion to ``REVERT_REQUIRED``. Every
    other evidence-less completion is ``FLAG_FOR_REVIEW`` (human adjudication),
    so auto-revert stays scoped to the cascade population and never blanket-reverts
    legitimately-worked older Dones.

    Pure function — no I/O.
    """
    # Fail-closed evidence read: an unreadable probe is treated as no evidence.
    effective_evidence = facts.evidence if facts.evidence_probe_ok else None
    decision = evaluate_close_evidence(effective_evidence)

    cascade_fingerprint = (
        facts.same_second_sibling_cluster or facts.parent_completed_same_second
    )

    if decision.allowed:
        verdict = EnumCompletionVerdict.KEEP
        reason = (
            "durable evidence present — completion is legitimately Done: "
            f"{decision.reason}"
        )
    elif cascade_fingerprint:
        verdict = EnumCompletionVerdict.REVERT_REQUIRED
        reason = (
            "no durable evidence AND Linear autoCloseChildIssues cascade "
            "fingerprint (same-second sibling/parent batch). "
            f"{decision.reason} Revert to {facts.prior_state_name!r} until the "
            "ticket's own acceptance criteria are evidenced (OMN-14915)."
        )
    else:
        verdict = EnumCompletionVerdict.FLAG_FOR_REVIEW
        reason = (
            "no durable evidence but no cascade fingerprint — possible "
            f"manual/legacy Done. {decision.reason} Human adjudication required; "
            "not silently kept (OMN-14915)."
        )

    return ModelCompletionVerdictResult(
        ticket_id=facts.ticket_id,
        linear_id=facts.linear_id,
        verdict=verdict,
        reason=reason,
        evidence_allowed=decision.allowed,
        prior_state_name=facts.prior_state_name,
        cascade_fingerprint=cascade_fingerprint,
    )


def reconcile_batch(
    facts: list[ModelCompletionFacts],
) -> list[ModelCompletionVerdictResult]:
    """Map :func:`evaluate_completion` over a batch. Pure — no I/O."""
    return [evaluate_completion(f) for f in facts]


__all__ = ["evaluate_completion", "reconcile_batch"]
