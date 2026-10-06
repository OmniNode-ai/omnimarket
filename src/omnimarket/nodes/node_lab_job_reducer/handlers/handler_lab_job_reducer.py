# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure lab-job reduction using only the event timestamp as time.

Submission queues and dispatches the original brief; adoption starts running.
Duplicate submissions and stale attempts drop before state-specific handling.
Terminal episodes only reopen through an alerted job's explicit resolution.
Queued jobs can be cancelled or declined with backoff; one runtime wins each
take, and competing runtimes receive refusal intents. Claims start running.
Ticks enforce dispatch, claim and stop deadlines and resend unacknowledged
alerts. Liveness checks record verdicts, stall dropped jobs after their grace
period and recover live stalled jobs. Ended attempts enter completion checking.
Completion checks finish jobs, schedule bounded retries, or stop stalled
attempts before retrying. Time-box continuations append to the original brief
once and are cleared after dispatch. Jobs without a recorded brief cannot
dispatch. Failures record FAILED then ALERTING and emit a stable alert dedup
key; acknowledgements close the alert episode. Each transition increments seq
and takes its entry timestamp from the event; in-place updates retain both.
Unhandled events drop with a reason. No clock reads, I/O or logging occur.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from omnimarket.models.lab_job import (
    TERMINAL_LAB_JOB_STATES,
    EnumLabJobAttemptOutcome,
    EnumLabJobEventKind,
    EnumLabJobIntentKind,
    EnumLabJobKind,
    EnumLabJobLiveness,
    EnumLabJobOnTimeBox,
    EnumLabJobResolution,
    EnumLabJobState,
    ModelLabJobEvent,
    ModelLabJobIntent,
    ModelLabJobReduceInput,
    ModelLabJobReduceOutput,
    ModelLabJobRow,
    ModelLabJobTransition,
)

_S = EnumLabJobState
_E = EnumLabJobEventKind
_I = EnumLabJobIntentKind
_V = EnumLabJobLiveness


class HandlerLabJobReducer:
    """Reduce one immutable row and event to a row, transitions and intents."""

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["REDUCER"]:
        return "REDUCER"

    def handle(self, request: ModelLabJobReduceInput) -> ModelLabJobReduceOutput:
        row, ev = request.row, request.event
        if row is None:
            return _initial(ev)
        if ev.kind in (_E.SUBMITTED, _E.ADOPTED):
            return _drop(row, "duplicate_submission")
        if ev.attempt is not None and ev.attempt != row.attempt:
            return _drop(row, "stale_attempt")
        if row.state in TERMINAL_LAB_JOB_STATES:
            return _resolve(row, ev)
        if ev.kind is _E.CANCELLED:
            if row.state in (_S.QUEUED, _S.RETRYING):
                return _move(row, ev, _S.DONE, "cancelled")
            return _drop(row, "not_cancellable")
        if ev.kind is _E.TICK:
            return _tick(row, ev)
        if ev.kind is _E.TAKEN:
            return _taken(row, ev)
        if ev.kind is _E.DECLINED:
            if row.state is not _S.QUEUED:
                return _drop(row, "not_queued")
            return _applied(
                row.model_copy(update={"next_dispatch_at": ev.at + _backoff(row)})
            )
        if ev.kind is _E.CLAIMED:
            if row.state is not _S.DISPATCHED:
                return _drop(row, "not_dispatched")
            return _move(
                row,
                ev,
                _S.RUNNING,
                "claimed",
                changes={"claimed_at": ev.at, "run_id": ev.run_id},
            )
        if ev.kind is _E.ATTEMPT_ENDED:
            return _attempt_ended(row, ev)
        if ev.kind is _E.CHECKED:
            return _checked(row, ev)
        if ev.kind is _E.DONE_EVALUATED:
            return _done_evaluated(row, ev)
        if ev.kind is _E.STOP_CONFIRMED:
            if row.state is not _S.STOPPING:
                return _drop(row, "not_stopping")
            return _move(
                row,
                ev,
                _S.RETRYING,
                "stop_confirmed",
                changes={"next_dispatch_at": ev.at + _backoff(row)},
            )
        if ev.kind is _E.ALERT_ACKNOWLEDGED:
            if row.state is not _S.ALERTING:
                return _drop(row, "not_alerting")
            return _move(row, ev, _S.ALERTED, "alert_acknowledged")
        return _drop(row, "unhandled_event")


def _initial(ev: ModelLabJobEvent) -> ModelLabJobReduceOutput:
    spec = ev.spec
    intents: tuple[ModelLabJobIntent, ...]
    if ev.kind is _E.SUBMITTED:
        if spec is None:
            return _drop(None, "no_recorded_brief")
        row = ModelLabJobRow(
            job_id=ev.job_id,
            kind=spec.kind,
            state=_S.QUEUED,
            spec=spec,
            attempt=1,
            seq=0,
            entered_state_at=ev.at,
            work_unit_id=f"{ev.job_id}-a1",
            parent_lane=spec.parent_lane,
            ticket=spec.ticket,
        )
        intents = (_dispatch(row),)
    elif ev.kind is _E.ADOPTED:
        row = ModelLabJobRow(
            job_id=ev.job_id,
            kind=EnumLabJobKind.ADOPTED,
            state=_S.RUNNING,
            spec=spec,
            attempt=1,
            seq=0,
            entered_state_at=ev.at,
            claimed_at=ev.at,
            run_id=ev.run_id,
            parent_lane=ev.parent_lane
            if ev.parent_lane is not None
            else (spec.parent_lane if spec is not None else None),
            ticket=ev.ticket
            if ev.ticket is not None
            else (spec.ticket if spec is not None else None),
        )
        intents = ()
    else:
        return _drop(None, "unknown_job")
    transition = ModelLabJobTransition(
        job_id=row.job_id,
        seq=1,
        from_state=None,
        to_state=row.state,
        attempt=row.attempt,
        at=ev.at,
        reason=ev.kind.value,
    )
    return ModelLabJobReduceOutput(
        row=row.model_copy(update={"seq": 1}),
        applied=True,
        transitions=(transition,),
        intents=intents,
    )


def _resolve(row: ModelLabJobRow, ev: ModelLabJobEvent) -> ModelLabJobReduceOutput:
    if row.state is not _S.ALERTED or ev.kind is not _E.RESOLVED:
        return _drop(row, "terminal")
    if ev.resolution is EnumLabJobResolution.CLOSE:
        return _move(row, ev, _S.DONE, "resolved_close")
    if ev.resolution is not EnumLabJobResolution.RETRY:
        return _drop(row, "terminal")
    if row.spec is None:
        return _drop(row, "no_recorded_brief")
    fresh = _new_attempt(row).model_copy(
        update={
            "episode": row.episode + 1,
            "continuation": None,
            "failure_reason": None,
            "alert_sent_at": None,
        }
    )
    return _move(fresh, ev, _S.QUEUED, "resolved_retry", intents=(_dispatch(fresh),))


def _tick(row: ModelLabJobRow, ev: ModelLabJobEvent) -> ModelLabJobReduceOutput:
    elapsed = ev.at - row.entered_state_at
    if row.state is _S.QUEUED:
        if elapsed >= timedelta(minutes=30):
            return _fail(row, ev, "dispatch_deadline")
        if row.next_dispatch_at is not None and ev.at >= row.next_dispatch_at:
            if row.spec is None:
                return _drop(row, "no_recorded_brief")
            return _applied(
                row.model_copy(update={"next_dispatch_at": None}),
                intents=(_dispatch(row),),
            )
    elif row.state is _S.DISPATCHED:
        if elapsed >= timedelta(minutes=10):
            return _move(row, ev, _S.STALLED, "claim_deadline")
    elif row.state is _S.STOPPING:
        if elapsed >= timedelta(minutes=10):
            return _fail(row, ev, "stop_unconfirmed")
    elif (
        row.state is _S.ALERTING
        and row.alert_sent_at is not None
        and ev.at - row.alert_sent_at >= timedelta(minutes=15)
    ):
        return _applied(
            row.model_copy(update={"alert_sent_at": ev.at}),
            intents=(_alert(row),),
        )
    return _drop(row, "nothing_due")


def _taken(row: ModelLabJobRow, ev: ModelLabJobEvent) -> ModelLabJobReduceOutput:
    if row.state is _S.QUEUED:
        return _move(
            row,
            ev,
            _S.DISPATCHED,
            "taken",
            changes={"owner_runtime": ev.runtime_id, "next_dispatch_at": None},
        )
    if row.state in (
        _S.DISPATCHED,
        _S.RUNNING,
        _S.CHECKING,
        _S.STALLED,
        _S.STOPPING,
        _S.RETRYING,
        _S.FAILED,
        _S.ALERTING,
    ):
        if row.owner_runtime == ev.runtime_id:
            return _drop(row, "duplicate_take")
        return ModelLabJobReduceOutput(
            row=row,
            applied=False,
            drop_reason="taken_by_other_runtime",
            intents=(
                ModelLabJobIntent(
                    kind=_I.TAKE_REFUSED,
                    job_id=row.job_id,
                    attempt=row.attempt,
                    work_unit_id=row.work_unit_id,
                    runtime_id=ev.runtime_id,
                ),
            ),
        )
    return _drop(row, "not_queued")


def _attempt_ended(
    row: ModelLabJobRow,
    ev: ModelLabJobEvent,
) -> ModelLabJobReduceOutput:
    if row.state not in (_S.DISPATCHED, _S.RUNNING):
        return _drop(row, "not_running")
    time_box_hit = ev.outcome is EnumLabJobAttemptOutcome.TIMED_OUT
    changes: dict[str, object] = {
        "last_outcome": ev.outcome,
        "time_box_hit": time_box_hit,
    }
    if (
        time_box_hit
        and row.spec is not None
        and row.spec.retry_policy.on_time_box is EnumLabJobOnTimeBox.CONTINUE
    ):
        changes["continuation"] = (
            f"\n\n---\nContinuation (attempt {row.attempt + 1}): continue from here; "
            "the previous attempt hit its time box.\n"
            f"Last STATUS: {ev.last_status or 'none recorded'}\n"
            f"Pushed head: {ev.head or 'none recorded'}\n"
        )
    return _move(row, ev, _S.CHECKING, "attempt_ended", changes=changes)


def _checked(row: ModelLabJobRow, ev: ModelLabJobEvent) -> ModelLabJobReduceOutput:
    if row.state not in (_S.RUNNING, _S.STALLED):
        return _drop(row, "not_watched")
    recorded = row.model_copy(update={"last_verdict": ev.verdict})
    if row.state is _S.STALLED:
        if ev.verdict is _V.ALIVE:
            return _move(recorded, ev, _S.RUNNING, "recovered")
        return _applied(recorded)
    if ev.verdict is _V.TERMINATED:
        return _move(recorded, ev, _S.CHECKING, "terminated")
    stall_after = row.spec.stall_after_min if row.spec is not None else 20
    if (
        ev.verdict is _V.DROPPED
        and row.claimed_at is not None
        and ev.at - row.claimed_at >= timedelta(minutes=stall_after)
    ):
        return _move(recorded, ev, _S.STALLED, "dropped")
    return _applied(recorded)


def _done_evaluated(
    row: ModelLabJobRow,
    ev: ModelLabJobEvent,
) -> ModelLabJobReduceOutput:
    if row.state not in (_S.CHECKING, _S.STALLED, _S.RETRYING):
        return _drop(row, "not_checking")
    if ev.done_rule_met:
        return _move(row, ev, _S.DONE, "done_rule_met")
    spec = row.spec
    attempts_left = spec is not None and row.attempt < spec.retry_policy.max_attempts
    if row.state is _S.CHECKING:
        if (
            row.time_box_hit
            and spec is not None
            and spec.retry_policy.on_time_box is EnumLabJobOnTimeBox.FAIL
        ):
            return _fail(row, ev, "time_box")
        if not attempts_left:
            return _fail(row, ev, "attempts_exhausted")
        return _move(
            row,
            ev,
            _S.RETRYING,
            "retry",
            changes={"next_dispatch_at": ev.at + _backoff(row)},
        )
    if row.state is _S.STALLED:
        if not attempts_left:
            return _fail(row, ev, "stalled")
        kill = ModelLabJobIntent(
            kind=_I.KILL_ATTEMPT,
            job_id=row.job_id,
            attempt=row.attempt,
            work_unit_id=row.work_unit_id,
            runtime_id=row.owner_runtime,
        )
        return _move(row, ev, _S.STOPPING, "stop_attempt", intents=(kill,))
    if row.next_dispatch_at is None or ev.at < row.next_dispatch_at:
        return _drop(row, "backoff_pending")
    if spec is None:
        return _drop(row, "no_recorded_brief")
    fresh = _new_attempt(row)
    dispatch = _dispatch(fresh)
    fresh = fresh.model_copy(update={"continuation": None})
    return _move(fresh, ev, _S.QUEUED, "retry_dispatch", intents=(dispatch,))


def _new_attempt(row: ModelLabJobRow) -> ModelLabJobRow:
    """Reset attempt observations, retaining state until the transition is recorded."""
    attempt = row.attempt + 1
    return row.model_copy(
        update={
            "attempt": attempt,
            "work_unit_id": f"{row.job_id}-a{attempt}",
            "owner_runtime": None,
            "run_id": None,
            "claimed_at": None,
            "last_verdict": None,
            "last_outcome": None,
            "time_box_hit": False,
            "next_dispatch_at": None,
        }
    )


def _brief(row: ModelLabJobRow) -> str | None:
    if row.spec is None:
        return None
    return row.spec.brief + (row.continuation or "")


def _backoff(row: ModelLabJobRow) -> timedelta:
    return timedelta(seconds=row.spec.retry_policy.backoff_s if row.spec else 60)


def _dispatch(row: ModelLabJobRow) -> ModelLabJobIntent:
    return ModelLabJobIntent(
        kind=_I.DISPATCH,
        job_id=row.job_id,
        attempt=row.attempt,
        work_unit_id=f"{row.job_id}-a{row.attempt}",
        brief=_brief(row),
    )


def _alert(row: ModelLabJobRow) -> ModelLabJobIntent:
    return ModelLabJobIntent(
        kind=_I.ALERT,
        job_id=row.job_id,
        attempt=row.attempt,
        work_unit_id=row.work_unit_id,
        dedup_key=f"lab-job:{row.job_id}:{row.attempt}:failed",
        reason=row.failure_reason,
    )


def _fail(
    row: ModelLabJobRow,
    ev: ModelLabJobEvent,
    reason: str,
) -> ModelLabJobReduceOutput:
    recorded = row.model_copy(update={"failure_reason": reason, "alert_sent_at": ev.at})
    failed, failure_transition = _advance(recorded, ev, _S.FAILED, reason)
    alerting, alert_transition = _advance(failed, ev, _S.ALERTING, "alert")
    return ModelLabJobReduceOutput(
        row=alerting,
        applied=True,
        transitions=(failure_transition, alert_transition),
        intents=(_alert(alerting),),
    )


def _move(
    row: ModelLabJobRow,
    ev: ModelLabJobEvent,
    state: EnumLabJobState,
    reason: str,
    *,
    changes: dict[str, object] | None = None,
    intents: tuple[ModelLabJobIntent, ...] = (),
) -> ModelLabJobReduceOutput:
    updated = row.model_copy(update=changes or {})
    updated, transition = _advance(updated, ev, state, reason)
    return ModelLabJobReduceOutput(
        row=updated,
        applied=True,
        transitions=(transition,),
        intents=intents,
    )


def _advance(
    row: ModelLabJobRow,
    ev: ModelLabJobEvent,
    state: EnumLabJobState,
    reason: str,
) -> tuple[ModelLabJobRow, ModelLabJobTransition]:
    transition = ModelLabJobTransition(
        job_id=row.job_id,
        seq=row.seq + 1,
        from_state=row.state,
        to_state=state,
        attempt=row.attempt,
        at=ev.at,
        reason=reason,
    )
    updated = row.model_copy(
        update={
            "state": state,
            "seq": transition.seq,
            "entered_state_at": ev.at,
        }
    )
    return updated, transition


def _applied(
    row: ModelLabJobRow,
    *,
    intents: tuple[ModelLabJobIntent, ...] = (),
) -> ModelLabJobReduceOutput:
    return ModelLabJobReduceOutput(row=row, applied=True, intents=intents)


def _drop(row: ModelLabJobRow | None, reason: str) -> ModelLabJobReduceOutput:
    return ModelLabJobReduceOutput(row=row, applied=False, drop_reason=reason)


__all__: list[str] = ["HandlerLabJobReducer"]
