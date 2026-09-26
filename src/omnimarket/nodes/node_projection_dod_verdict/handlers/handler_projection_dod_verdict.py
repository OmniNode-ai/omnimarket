# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure fold of one completed definition-of-done verification (OMN-18900).

WHY THE DONE PREDICATE LIVES HERE
    The eval metric in section 3.1 of the Jev typed-decision shadow plan
    defines its outcome as "the verify node's terminal payload for that ticket
    reports zero failed checks AND at least one behaviour-proving check". That
    rule exists in the fleet exactly once today, inside the evidence-autoclose
    flip gate, where it reads counters by string key off parsed standard
    output. It is not typed, not shared, and not answerable from anything
    stored -- so nothing can ask it of a run that finished last week.

    Putting it in a pure definition-B handler makes it falsifiable by a unit
    test, and putting the counts beside the answer on the row makes it
    re-runnable over stored history when the rule changes.

WHY THE COUNTS OUTRANK THE STATUS
    The predicate reads ``failed_count`` before it reads ``status``. The
    counts ARE the evidence and the status is a summary of them; a verdict
    whose status says verified while its own failure count is non-zero is a
    defect in the summariser, and the honest reading of that pair is the
    count. This is the same envelope-purity reasoning the runtime-error
    fingerprint reducer records for not trusting a producer's stamped
    category -- a process that grades its own result can be wrong about it in
    exactly the way that hides the problem.

WHY A VERDICT OVER ZERO CHECKS IS REFUSED
    It is green by vacuum. Separating it from the behaviour-proving refusal
    matters because the remedies differ: zero checks means the contract
    declares no evidence at all, and zero behaviour-proving checks means it
    declares evidence that proves nothing. In the one payload the 2026-09-20
    inventory could read, the second was the case -- 88 checks, 72
    non-probative, zero behaviour-proving.

The handler is def-B -- ``handle(ModelX) -> ModelY`` -- stateless,
deterministic, no clock, no broker, no database.
"""

from __future__ import annotations

from omnimarket.enums.enum_dod_verify_status import EnumDodVerifyStatus
from omnimarket.nodes.node_projection_dod_verdict.models import (
    EnumDodEvalOutcome,
    EnumDodEvalRefusal,
    ModelDodEvalVerdict,
    ModelDodVerdictProjectionRequest,
    ModelDodVerdictProjectionResult,
    ModelDodVerdictRow,
)

#: The behaviour-proving floor the eval metric requires. Named rather than
#: spelled inline at the comparison so a test can assert the bound itself, and
#: so a future change to it is one edit with one blame line.
MINIMUM_BEHAVIOR_PROVING_CHECKS = 1


def resolve_dod_eval_outcome(
    *,
    status: EnumDodVerifyStatus,
    failed_count: int,
    total_checks: int,
    behavior_proving_count: int,
) -> ModelDodEvalVerdict:
    """Answer the eval metric's done question over one run's own counts.

    Takes the four facts and nothing else, so it can be applied equally to an
    arriving event and to a row already in the table -- the acceptance
    criterion that the outcome be computable from the row alone is satisfied
    by this signature rather than by a comment.

    Precedence is declared and is not incidental to the order the conditions
    happen to be written in:

    1. a failed check refuses, whatever the status claims;
    2. a status other than verified refuses;
    3. zero verdict-bearing checks refuses;
    4. fewer behaviour-proving checks than the declared floor refuses;
    5. otherwise the run counts as done.
    """
    if failed_count > 0:
        return ModelDodEvalVerdict(
            outcome=EnumDodEvalOutcome.REFUSED,
            refusal=EnumDodEvalRefusal.CHECKS_FAILED,
        )
    if status is not EnumDodVerifyStatus.VERIFIED:
        return ModelDodEvalVerdict(
            outcome=EnumDodEvalOutcome.REFUSED,
            refusal=EnumDodEvalRefusal.STATUS_NOT_VERIFIED,
        )
    if total_checks <= 0:
        return ModelDodEvalVerdict(
            outcome=EnumDodEvalOutcome.REFUSED,
            refusal=EnumDodEvalRefusal.NO_CHECKS_RUN,
        )
    if behavior_proving_count < MINIMUM_BEHAVIOR_PROVING_CHECKS:
        return ModelDodEvalVerdict(
            outcome=EnumDodEvalOutcome.REFUSED,
            refusal=EnumDodEvalRefusal.NO_BEHAVIOR_PROVING_CHECK,
        )
    return ModelDodEvalVerdict(outcome=EnumDodEvalOutcome.DONE)


class HandlerProjectionDodVerdict:
    """Fold one completed verification into the row the writer persists."""

    def handle(
        self, request: ModelDodVerdictProjectionRequest
    ) -> ModelDodVerdictProjectionResult:
        """One verdict in, one row out. Pure and deterministic.

        Nothing here consults a clock. Every timestamp on the row is the
        event's own, so a redelivery of the same verdict reproduces the same
        row rather than re-dating it -- which is what lets the key refuse a
        duplicate in SQL instead of by a check-then-act the runtime cannot
        serialize.
        """
        event = request.event

        verdict = resolve_dod_eval_outcome(
            status=event.status,
            failed_count=event.failed_count,
            total_checks=event.total_checks,
            behavior_proving_count=event.behavior_proving_count,
        )

        if event.dry_run:
            # OMN-18901: a rehearsal is not an attempt. Storing one would
            # inflate the attempts-until-done measure this table feeds, and the
            # stored row would be indistinguishable from a real verdict
            # afterwards. The verdict is still returned so an in-memory caller
            # sees what the rehearsal would have concluded.
            return ModelDodVerdictProjectionResult(row=None, verdict=verdict)

        row = ModelDodVerdictRow(
            ticket_id=event.ticket_id,
            correlation_id=event.correlation_id,
            completed_at=event.completed_at,
            started_at=event.started_at,
            status=event.status,
            unresolved_cause=event.unresolved_cause,
            delegation_correlation_id=event.delegation_correlation_id,
            total_checks=event.total_checks,
            verified_count=event.verified_count,
            failed_count=event.failed_count,
            skipped_count=event.skipped_count,
            superseded_count=event.superseded_count,
            non_probative_count=event.non_probative_count,
            behavior_proving_count=event.behavior_proving_count,
            readback_proving_count=event.readback_proving_count,
            unbindable_overlay_count=event.unbindable_overlay_count,
            outcome=verdict.outcome,
            outcome_refusal=verdict.refusal,
            error_message=event.error_message or "",
        )

        return ModelDodVerdictProjectionResult(row=row, verdict=verdict)


__all__ = [
    "MINIMUM_BEHAVIOR_PROVING_CHECKS",
    "HandlerProjectionDodVerdict",
    "resolve_dod_eval_outcome",
]
