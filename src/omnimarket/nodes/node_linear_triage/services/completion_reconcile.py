# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fail-closed completion reconciler decision core (OMN-14915, OMN-15373).

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
(``KEEP``), an evidence-less flip that must be reverted (``REVERT_REQUIRED``),
or an evidence-less completion of unknown provenance a human must adjudicate
(``FLAG_FOR_REVIEW``). It REUSES ``evaluate_close_evidence`` so there is exactly
one definition of "durable evidence" in both directions — this is not a second
evidence checker.

Second ungateable path — the git automation (OMN-15373)
-------------------------------------------------------
The cascade is not the only server-side Done writer. A Linear team's
``GitAutomationState`` for ``event=merge`` moves every linked issue to its
configured state seconds after any PR merges — no closing keyword required,
branch-name linkage or a bare body mention is enough. When that state's type is
``completed``, every merge mints a Done: 16 of them in one 4.5h window on
2026-07-29, each 2.0-3.1s post-merge.

That population needs a rule the cascade path does not: the **circular-evidence
rule**. A merged PR is normally admissible durable evidence, but for a Done the
*merge itself caused*, citing that merge proves nothing — it restates the
trigger. So for the merge-automation fingerprint the merge-derived kinds are
stripped before evaluation, leaving only proof produced by a step separate from
the merge (a ``dod_verify`` OCC receipt, or a verified runtime readback).

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
    EnumCloseEvidenceKind,
    evaluate_close_evidence,
)

# Evidence kinds that are INADMISSIBLE for a Done written by a git automation
# reacting to a PR merge (OMN-15373).
#
# The circular-evidence rule: the merge is what CAUSED the flip, so citing that
# same merge as the flip's evidence proves nothing — it restates the trigger.
# `MERGED_IMPLEMENTING_PR` and `SUPERSEDED_BY_MERGED_PR` are exactly that
# restatement. `ALL_CHILDREN_DONE` is a roll-up over a child population whose
# own Dones were minted by the same automation, so it inherits the circularity
# one level up.
#
# What REMAINS admissible is the non-circular set: `OCC_RECEIPT` (a durable
# dod_verify receipt bound to the ticket on the governance ref) and
# `RUNTIME_OPS_READBACK` (an independently-verified runtime readback). Both are
# produced by a proof step that is separate from the merge, which is the whole
# point — a merge is code-only/receipt-bound at best, and Done requires proof.
#
# Scope note: this narrowing applies ONLY to the merge-automation population.
# `evaluate_close_evidence` still admits a merged PR everywhere else, including
# node_linear_triage's forward auto-close path (OMN-13817). Narrowing it
# globally is a separate, larger behavioural change and is NOT done here.
_AUTOMATION_INADMISSIBLE_KINDS: frozenset[EnumCloseEvidenceKind] = frozenset(
    {
        EnumCloseEvidenceKind.MERGED_IMPLEMENTING_PR,
        EnumCloseEvidenceKind.SUPERSEDED_BY_MERGED_PR,
        EnumCloseEvidenceKind.ALL_CHILDREN_DONE,
    }
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

    cascade_fingerprint = (
        facts.same_second_sibling_cluster or facts.parent_completed_same_second
    )
    automation_fingerprint = facts.merge_automation_fingerprint

    # OMN-15373: for a Done written by a git automation reacting to a merge, the
    # merge itself is inadmissible as that Done's evidence — it is the trigger,
    # not the proof. Strip the circular kinds BEFORE evaluating, so a merged-PR
    # citation cannot rescue a flip the merge caused.
    if (
        automation_fingerprint
        and effective_evidence is not None
        and effective_evidence.kind in _AUTOMATION_INADMISSIBLE_KINDS
    ):
        effective_evidence = None

    decision = evaluate_close_evidence(effective_evidence)

    if decision.allowed:
        verdict = EnumCompletionVerdict.KEEP
        reason = (
            "durable evidence present — completion is legitimately Done: "
            f"{decision.reason}"
        )
    elif automation_fingerprint:
        verdict = EnumCompletionVerdict.REVERT_REQUIRED
        pr = f" driven by {facts.driving_pr}" if facts.driving_pr else ""
        latency = (
            f" (+{facts.merge_to_done_latency_s:.1f}s after the merge)"
            if facts.merge_to_done_latency_s is not None
            else ""
        )
        reason = (
            f"Done was written by a git automation reacting to a PR merge{pr}"
            f"{latency}, and no NON-CIRCULAR durable evidence backs it. The merge "
            "cannot be its own proof: it is what triggered the flip. A dod_verify "
            "receipt (or an independently-verified runtime readback) is required. "
            f"{decision.reason} Revert to {facts.prior_state_name!r} until the "
            "ticket's own acceptance criteria are evidenced (OMN-15373)."
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
        automation_fingerprint=automation_fingerprint,
        driving_pr=facts.driving_pr,
    )


def reconcile_batch(
    facts: list[ModelCompletionFacts],
) -> list[ModelCompletionVerdictResult]:
    """Map :func:`evaluate_completion` over a batch. Pure — no I/O."""
    return [evaluate_completion(f) for f in facts]


__all__ = ["evaluate_completion", "reconcile_batch"]
