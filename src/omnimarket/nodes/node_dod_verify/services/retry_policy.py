# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Pure reconciliation policy over per-item DoD retry state (OMN-17022 / A15).

Deterministic and I/O-free: every input is a recorded state, a policy and an
explicit ``now``. The durable read/write is the ledger's job
(``handlers/dod_verify_retry_ledger.py``), which is the effect boundary.

Two policies, not one. The nine items the 2026-08-29 closeout held under
``RUN_ERROR_OR_TIMEOUT`` describe a run that faulted, so another bounded
attempt can legitimately land somewhere else. OMN-14993 held under
``PR_LOOKUP_FAILED`` describes the verifier's own configuration, which a retry
reproduces byte-for-byte; it is refused on the first observation rather than
after spending the budget to learn nothing.
"""

from __future__ import annotations

from datetime import datetime

from omnimarket.enums.enum_dod_verify_unresolved_cause import (
    EnumDodVerifyUnresolvedCause,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_retry_state import (
    EnumDodVerifyRetryDisposition,
    ModelDodVerifyRetryDecision,
    ModelDodVerifyRetryPolicy,
    ModelDodVerifyRetryState,
    backoff_deadline,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
)


def reconcile_abandoned_attempt(
    state: ModelDodVerifyRetryState,
    *,
    now: datetime,
    detail: str,
) -> ModelDodVerifyRetryState:
    """Materialise a trailing abandoned attempt as a typed run fault.

    An attempt with a start and no completion is a run that was killed. The
    state already *reads* UNRESOLVED for it (fail-closed), but until it is
    written down explicitly the record cannot say which cause it was, and no
    further attempt may be appended after it. This is the reconciliation step
    the ticket names: it converts "the process vanished" into the typed
    ``RUN_ERROR_OR_TIMEOUT`` the closeout's ad-hoc label was reaching for.

    A no-op for any state without an abandoned trailing attempt.
    """
    if not state.has_abandoned_attempt:
        return state
    return state.complete_attempt(
        status=EnumDodVerifyStatus.UNRESOLVED,
        cause=EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT,
        now=now,
        detail=detail,
    )


def plan_next_attempt(
    state: ModelDodVerifyRetryState,
    *,
    policy: ModelDodVerifyRetryPolicy,
    now: datetime,
) -> ModelDodVerifyRetryDecision:
    """Decide what to do with one item, given everything recorded about it.

    Never mutates and never reads the clock itself — ``now`` is supplied so a
    caller (and a test) can prove the backoff window rather than wait it out.
    """
    status = state.status

    if status is EnumDodVerifyStatus.PENDING:
        return ModelDodVerifyRetryDecision(
            ticket_id=state.ticket_id,
            disposition=EnumDodVerifyRetryDisposition.ATTEMPT_NOW,
            status=status,
            attempt_count=state.attempt_count,
            reason="no attempt has ever been recorded for this item",
        )

    if status is not EnumDodVerifyStatus.UNRESOLVED:
        return ModelDodVerifyRetryDecision(
            ticket_id=state.ticket_id,
            disposition=EnumDodVerifyRetryDisposition.RESOLVED,
            status=status,
            attempt_count=state.attempt_count,
            reason=f"item reached a verdict: {status.value}",
        )

    cause = state.latest_cause
    if cause is None:
        # Unreachable via the model (UNRESOLVED implies a cause, and an
        # abandoned attempt reports RUN_ERROR_OR_TIMEOUT), but an absent cause
        # must never inherit a retryable policy by accident.
        cause = EnumDodVerifyUnresolvedCause.UNKNOWN

    if not cause.retry_eligible:
        return ModelDodVerifyRetryDecision(
            ticket_id=state.ticket_id,
            disposition=EnumDodVerifyRetryDisposition.TERMINAL_NOT_RETRYABLE,
            status=status,
            cause=cause,
            attempt_count=state.attempt_count,
            reason=(
                f"{cause.value} is a configuration or resolution defect that a "
                "retry reproduces exactly; remedy the binding or the "
                "credential rather than re-running"
            ),
        )

    if state.attempt_count >= policy.max_attempts:
        return ModelDodVerifyRetryDecision(
            ticket_id=state.ticket_id,
            disposition=(EnumDodVerifyRetryDisposition.TERMINAL_ATTEMPTS_EXHAUSTED),
            status=status,
            cause=cause,
            attempt_count=state.attempt_count,
            reason=(
                f"{state.attempt_count} of {policy.max_attempts} attempts spent "
                f"on {cause.value}; the item stays unresolved and blocks any "
                "'sweep clean' claim"
            ),
        )

    last = state.attempts[-1]
    # Anchored on ``started_at``, deliberately, not on ``completed_at``.
    # Backoff exists to space ATTEMPTS apart, and for an attempt reconciled
    # long after the process that ran it died, ``completed_at`` is the instant
    # we NOTICED — anchoring on it would make a two-hour-dead attempt wait a
    # fresh full window, which is how a held item stays held. For a normal
    # attempt the two instants are seconds apart and the choice is immaterial.
    deadline = backoff_deadline(
        last_attempt_at=last.started_at,
        attempt_number=state.attempt_count,
        policy=policy,
    )
    if now >= deadline:
        return ModelDodVerifyRetryDecision(
            ticket_id=state.ticket_id,
            disposition=EnumDodVerifyRetryDisposition.ATTEMPT_NOW,
            status=status,
            cause=cause,
            attempt_count=state.attempt_count,
            reason=(
                f"backoff window for attempt {state.attempt_count} elapsed at "
                f"{deadline.isoformat()}"
            ),
        )

    return ModelDodVerifyRetryDecision(
        ticket_id=state.ticket_id,
        disposition=EnumDodVerifyRetryDisposition.RETRY_SCHEDULED,
        status=status,
        cause=cause,
        attempt_count=state.attempt_count,
        next_attempt_not_before=deadline,
        reason=(
            f"attempt {state.attempt_count} of {policy.max_attempts} failed with "
            f"{cause.value}; next attempt is held until {deadline.isoformat()}"
        ),
    )


__all__: list[str] = ["plan_next_attempt", "reconcile_abandoned_attempt"]
