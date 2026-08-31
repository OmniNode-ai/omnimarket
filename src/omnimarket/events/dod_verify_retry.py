# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Durable per-item retry state and reconciliation policy for DoD verification.

OMN-17022 / off-rails A15. Shared, not node-local: ``node_dod_verify`` and
``node_dod_sweep_orchestrator`` both produce and reconcile this state, and a
node may never reach into a sibling's models package (OMN-9263).

``node_dod_verify`` had no retry policy at all — grepping
``retry|backoff|attempt|resume|checkpoint`` across it yielded a comment, a
message string, a docstring *disclaiming* retry logic, and one hardcoded
``git fetch`` re-attempt. ``node_dod_sweep_orchestrator`` had zero hits. So a
run that faulted left no per-item trace, and the only way to revisit one held
item was to re-run the whole audit.

Three invariants are structural here rather than conventional:

* **PENDING is not a recordable outcome.** ``ModelDodVerifyAttempt`` rejects
  it. ``PENDING`` therefore means exactly "no attempt exists", and an item
  that has been attempted can never read as never-attempted (DoD 4).
* **An attempt with no completion fails closed to UNRESOLVED.** A process
  killed mid-attempt leaves a started-but-uncompleted record; reading that as
  PENDING is the precise defect this ticket exists to remove.
* **Attempts are append-only and contiguous.** History is replayable rather
  than inferred from the last write, the same property the OMN-17018 dispatch
  lifecycle ledger keeps one layer up.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum, unique
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_dod_verify_status import EnumDodVerifyStatus
from omnimarket.enums.enum_dod_verify_unresolved_cause import (
    EnumDodVerifyUnresolvedCause,
)

#: Outcomes an attempt may record. ``PENDING`` is deliberately absent — see the
#: module docstring; it is the whole point of the ticket.
_RECORDABLE_OUTCOMES: frozenset[EnumDodVerifyStatus] = frozenset(
    {
        EnumDodVerifyStatus.VERIFIED,
        EnumDodVerifyStatus.FAILED,
        EnumDodVerifyStatus.SKIPPED,
        EnumDodVerifyStatus.UNRESOLVED,
    }
)


@unique
class EnumDodVerifyRetryDisposition(StrEnum):
    """What reconciliation should do with an item, given its recorded state."""

    #: Run it now — it has never been attempted, or a scheduled backoff
    #: window has elapsed.
    ATTEMPT_NOW = "attempt_now"
    #: Unresolved for a retry-eligible cause, with attempts remaining and the
    #: backoff window still open. Do not run it now; do not report it clean.
    RETRY_SCHEDULED = "retry_scheduled"
    #: Unresolved for a retry-eligible cause, but the bounded attempt budget
    #: is spent. Terminal: it stays unresolved and blocks any "sweep clean"
    #: claim until a human changes something.
    TERMINAL_ATTEMPTS_EXHAUSTED = "terminal_attempts_exhausted"
    #: Unresolved for a cause a retry reproduces exactly (OMN-14993's
    #: ``PR_LOOKUP_FAILED``, or an unclassifiable ``UNKNOWN``). Refused on the
    #: first observation rather than after burning the budget.
    TERMINAL_NOT_RETRYABLE = "terminal_not_retryable"
    #: The item reached a real verdict. Nothing for retry policy to do — a
    #: fresh sweep is free to run it again, because evidence changes.
    RESOLVED = "resolved"

    @property
    def blocks_attempt(self) -> bool:
        """Whether reconciliation must NOT run the item now.

        Encoded on the member for the same reason retry eligibility is: a
        caller that forgets to branch must not re-run an item inside its
        backoff window or re-run a credential defect. ``RESOLVED`` does not
        block — re-running a verified item is a normal sweep, not a retry.
        """
        return self in _ATTEMPT_BLOCKING_DISPOSITIONS


#: Dispositions under which an item is deliberately not run this pass.
_ATTEMPT_BLOCKING_DISPOSITIONS: frozenset[EnumDodVerifyRetryDisposition] = frozenset(
    {
        EnumDodVerifyRetryDisposition.RETRY_SCHEDULED,
        EnumDodVerifyRetryDisposition.TERMINAL_ATTEMPTS_EXHAUSTED,
        EnumDodVerifyRetryDisposition.TERMINAL_NOT_RETRYABLE,
    }
)


class ModelDodVerifyRetryPolicy(BaseModel):
    """A bounded exponential-backoff policy. Every field is required.

    No field carries a default: a policy assembled from defaults is a policy
    nobody chose, and this one decides how many times a failing verification
    is allowed to re-run against live ``gh``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_attempts: int = Field(
        ..., ge=1, description="Total attempts allowed per item, including the first."
    )
    base_delay_seconds: float = Field(
        ..., gt=0.0, description="Delay after the first failed attempt."
    )
    multiplier: float = Field(
        ..., ge=1.0, description="Growth factor applied per subsequent attempt."
    )
    max_delay_seconds: float = Field(
        ..., gt=0.0, description="Upper bound on any single backoff delay."
    )

    @model_validator(mode="after")
    def _cap_must_not_undercut_the_base(self) -> Self:
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError(
                f"max_delay_seconds ({self.max_delay_seconds}) is below "
                f"base_delay_seconds ({self.base_delay_seconds}); the cap would "
                "silently shorten the very first backoff"
            )
        return self

    def delay_for_attempt(self, attempt_number: int) -> float:
        """Seconds to wait after the ``attempt_number``-th attempt failed.

        ``attempt_number`` is 1-based and names the attempt that JUST failed,
        so the first backoff is ``base_delay_seconds`` exactly.
        """
        if attempt_number < 1:
            raise ValueError(f"attempt_number must be >= 1, got {attempt_number}")
        raw = self.base_delay_seconds * (self.multiplier ** (attempt_number - 1))
        return min(raw, self.max_delay_seconds)


#: The policy the sweep uses unless a caller supplies its own. Three attempts,
#: 30s → 120s → capped at 600s: enough to ride out a transient ``gh`` fault or
#: a loaded host, short of re-running a full audit's worth of live lookups.
CANONICAL_DOD_VERIFY_RETRY_POLICY: ModelDodVerifyRetryPolicy = (
    ModelDodVerifyRetryPolicy(
        max_attempts=3,
        base_delay_seconds=30.0,
        multiplier=4.0,
        max_delay_seconds=600.0,
    )
)


class ModelDodVerifyAttempt(BaseModel):
    """One verification attempt against one ticket.

    ``completed_at is None`` means the attempt started and no completion was
    ever written — the process was killed mid-run. That record is the whole
    reason this model exists: it is what makes a timeout distinguishable from
    a never-started item.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempt_number: int = Field(..., ge=1)
    started_at: datetime = Field(...)
    completed_at: datetime | None = Field(
        default=None,
        description="None when the attempt was abandoned without an outcome.",
    )
    status: EnumDodVerifyStatus | None = Field(
        default=None,
        description="Outcome. None iff the attempt was abandoned. Never PENDING.",
    )
    cause: EnumDodVerifyUnresolvedCause | None = Field(
        default=None, description="Set iff status is UNRESOLVED."
    )
    error_code: str | None = Field(
        default=None,
        description=(
            "The producer's own error code verbatim, preserved even when the "
            "cause mapping collapsed it to UNKNOWN."
        ),
    )
    detail: str | None = Field(default=None)

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.started_at.tzinfo is None:
            raise ValueError("started_at must be timezone-aware")
        if (self.completed_at is None) != (self.status is None):
            raise ValueError(
                "an attempt has a completion timestamp exactly when it has an "
                f"outcome; got completed_at={self.completed_at!r}, "
                f"status={self.status!r}"
            )
        if self.completed_at is not None:
            if self.completed_at.tzinfo is None:
                raise ValueError("completed_at must be timezone-aware")
            if self.completed_at < self.started_at:
                raise ValueError("completed_at precedes started_at")
        if self.status is not None and self.status not in _RECORDABLE_OUTCOMES:
            # The DoD-4 guard, at the only place a producer can write one.
            raise ValueError(
                f"{self.status.value!r} is not a recordable attempt outcome; "
                "PENDING means 'no attempt exists' and an attempted item may "
                "never decay back to it"
            )
        if (self.cause is not None) != (self.status is EnumDodVerifyStatus.UNRESOLVED):
            raise ValueError(
                "cause is set exactly on an UNRESOLVED outcome; got "
                f"status={self.status!r}, cause={self.cause!r}"
            )
        return self

    @property
    def abandoned(self) -> bool:
        """True when the attempt started and no outcome was ever recorded."""
        return self.completed_at is None


class ModelDodVerifyRetryState(BaseModel):
    """The full, replayable attempt history for one ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., min_length=1)
    attempts: tuple[ModelDodVerifyAttempt, ...] = Field(default=())

    @model_validator(mode="after")
    def _history_is_append_only(self) -> Self:
        for index, attempt in enumerate(self.attempts):
            if attempt.attempt_number != index + 1:
                raise ValueError(
                    "attempt numbers must be contiguous and 1-based; got "
                    f"{attempt.attempt_number} at position {index}"
                )
            if index and attempt.started_at < self.attempts[index - 1].started_at:
                raise ValueError("attempts must be ordered by started_at")
            if index and self.attempts[index - 1].abandoned:
                # An abandoned attempt is reconciled into a completed one
                # before another may follow it, so history never silently
                # skips over a run that vanished.
                raise ValueError(
                    f"attempt {index} was abandoned and must be reconciled "
                    "before a further attempt is appended"
                )
        return self

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def has_abandoned_attempt(self) -> bool:
        return bool(self.attempts) and self.attempts[-1].abandoned

    @property
    def status(self) -> EnumDodVerifyStatus:
        """The item's current status.

        PENDING **only** when no attempt exists. A trailing abandoned attempt
        reads UNRESOLVED, not PENDING: the run was killed, which is a fact
        about the attempt, and reading it as "not yet attempted" is exactly
        the confusion that stranded the ten held items.
        """
        if not self.attempts:
            return EnumDodVerifyStatus.PENDING
        last = self.attempts[-1]
        if last.status is None:
            return EnumDodVerifyStatus.UNRESOLVED
        return last.status

    @property
    def latest_cause(self) -> EnumDodVerifyUnresolvedCause | None:
        """The cause of the current UNRESOLVED status, if it is unresolved.

        An abandoned trailing attempt has no recorded cause yet; it is a run
        fault by construction, so it reports as such.
        """
        if not self.attempts:
            return None
        last = self.attempts[-1]
        if last.abandoned:
            return EnumDodVerifyUnresolvedCause.RUN_ERROR_OR_TIMEOUT
        return last.cause

    def start_attempt(self, *, now: datetime) -> Self:
        """Append an in-flight attempt. Written BEFORE the run, so a process
        killed mid-run leaves a durable trace instead of nothing."""
        if self.has_abandoned_attempt:
            raise ValueError(
                f"{self.ticket_id} already has an unreconciled abandoned "
                "attempt; reconcile it before starting another"
            )
        return self.model_copy(
            update={
                "attempts": (
                    *self.attempts,
                    ModelDodVerifyAttempt(
                        attempt_number=self.attempt_count + 1,
                        started_at=now,
                    ),
                )
            }
        )

    def complete_attempt(
        self,
        *,
        status: EnumDodVerifyStatus,
        cause: EnumDodVerifyUnresolvedCause | None,
        now: datetime,
        detail: str | None,
        error_code: str | None = None,
    ) -> Self:
        """Close the in-flight attempt with a real outcome."""
        if not self.has_abandoned_attempt:
            raise ValueError(f"{self.ticket_id} has no in-flight attempt to complete")
        in_flight = self.attempts[-1]
        completed = ModelDodVerifyAttempt(
            attempt_number=in_flight.attempt_number,
            started_at=in_flight.started_at,
            completed_at=now,
            status=status,
            cause=cause,
            error_code=error_code,
            detail=detail,
        )
        return self.model_copy(update={"attempts": (*self.attempts[:-1], completed)})


class ModelDodVerifyRetryDecision(BaseModel):
    """What reconciliation decided to do with one item, and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., min_length=1)
    disposition: EnumDodVerifyRetryDisposition = Field(...)
    status: EnumDodVerifyStatus = Field(...)
    cause: EnumDodVerifyUnresolvedCause | None = Field(default=None)
    attempt_count: int = Field(..., ge=0)
    next_attempt_not_before: datetime | None = Field(
        default=None,
        description="Set iff disposition is RETRY_SCHEDULED.",
    )
    reason: str = Field(..., min_length=1)

    @model_validator(mode="after")
    def _schedule_only_on_a_scheduled_retry(self) -> Self:
        scheduled = self.disposition is EnumDodVerifyRetryDisposition.RETRY_SCHEDULED
        if scheduled != (self.next_attempt_not_before is not None):
            raise ValueError(
                "next_attempt_not_before is set exactly on RETRY_SCHEDULED; got "
                f"disposition={self.disposition.value}, "
                f"next_attempt_not_before={self.next_attempt_not_before!r}"
            )
        return self


def backoff_deadline(
    *, last_attempt_at: datetime, attempt_number: int, policy: ModelDodVerifyRetryPolicy
) -> datetime:
    """The earliest instant the next attempt may run."""
    return last_attempt_at + timedelta(seconds=policy.delay_for_attempt(attempt_number))


# ---------------------------------------------------------------------------
# Reconciliation policy (pure)
# ---------------------------------------------------------------------------
#
# Deterministic and I/O-free: every input is a recorded state, a policy and an
# explicit ``now``. The durable read/write is the ledger's job
# (``omnimarket.protocols.protocol_dod_verify_retry_ledger``), which is the
# effect boundary.
#
# Two policies, not one. The nine items the 2026-08-29 closeout held under
# ``RUN_ERROR_OR_TIMEOUT`` describe a run that faulted, so another bounded
# attempt can legitimately land somewhere else. OMN-14993 held under
# ``PR_LOOKUP_FAILED`` describes the verifier's own configuration, which a
# retry reproduces byte-for-byte; it is refused on the first observation
# rather than after spending the budget to learn nothing.


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


__all__: list[str] = [
    "CANONICAL_DOD_VERIFY_RETRY_POLICY",
    "EnumDodVerifyRetryDisposition",
    "ModelDodVerifyAttempt",
    "ModelDodVerifyRetryDecision",
    "ModelDodVerifyRetryPolicy",
    "ModelDodVerifyRetryState",
    "backoff_deadline",
    "plan_next_attempt",
    "reconcile_abandoned_attempt",
]
