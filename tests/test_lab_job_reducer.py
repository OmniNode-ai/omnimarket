# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Table-driven tests of node_lab_job_reducer (lab job supervisor plan, M1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omnimarket.models.lab_job import (
    TERMINAL_LAB_JOB_STATES,
    EnumLabJobAttemptOutcome,
    EnumLabJobDoneCriterionKind,
    EnumLabJobEngine,
    EnumLabJobEventKind,
    EnumLabJobIntentKind,
    EnumLabJobKind,
    EnumLabJobLiveness,
    EnumLabJobOnTimeBox,
    EnumLabJobResolution,
    EnumLabJobState,
    ModelLabJobDoneCriterion,
    ModelLabJobEvent,
    ModelLabJobReduceInput,
    ModelLabJobReduceOutput,
    ModelLabJobRetryPolicy,
    ModelLabJobRow,
    ModelLabJobSpec,
)
from omnimarket.nodes.node_lab_job_reducer.handlers.handler_lab_job_reducer import (
    HandlerLabJobReducer,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)
JOB = "lj-00000000000000aa"
S = EnumLabJobState
E = EnumLabJobEventKind
IK = EnumLabJobIntentKind
V = EnumLabJobLiveness
OC = EnumLabJobAttemptOutcome


def _spec(**overrides: Any) -> ModelLabJobSpec:
    fields: dict[str, Any] = {
        "job_id": JOB,
        "kind": EnumLabJobKind.LANE,
        "brief": "ORIGINAL BRIEF: build the thing",
        "repo": "OmniNode-ai/omnimarket",
        "ref": "b" * 40,
        "engine": EnumLabJobEngine.OPUS,
        "time_box_min": 30,
        "retry_policy": ModelLabJobRetryPolicy(max_attempts=2, backoff_s=60),
        "done_criteria": (
            ModelLabJobDoneCriterion(kind=EnumLabJobDoneCriterionKind.TERMINAL_ROW),
        ),
        "parent_lane": "parent-lane",
        "ticket": "OMN-20604",
    }
    fields.update(overrides)
    return ModelLabJobSpec(**fields)


def _row(state: EnumLabJobState, **overrides: Any) -> ModelLabJobRow:
    fields: dict[str, Any] = {
        "job_id": JOB,
        "kind": EnumLabJobKind.LANE,
        "state": state,
        "spec": _spec(),
        "attempt": 1,
        "seq": 3,
        "entered_state_at": T0,
        "work_unit_id": f"{JOB}-a1",
        "parent_lane": "parent-lane",
        "ticket": "OMN-20604",
    }
    fields.update(overrides)
    return ModelLabJobRow(**fields)


def _ev(
    kind: EnumLabJobEventKind, minutes: float = 0, **fields: Any
) -> ModelLabJobEvent:
    return ModelLabJobEvent(
        kind=kind, job_id=JOB, at=T0 + timedelta(minutes=minutes), **fields
    )


def _reduce(
    row: ModelLabJobRow | None, event: ModelLabJobEvent
) -> ModelLabJobReduceOutput:
    return HandlerLabJobReducer().handle(ModelLabJobReduceInput(row=row, event=event))


def _kinds(out: ModelLabJobReduceOutput) -> list[EnumLabJobIntentKind]:
    return [i.kind for i in out.intents]


# (name, row, event, expected state, expected intent kinds)
TABLE: list[
    tuple[
        str,
        ModelLabJobRow | None,
        ModelLabJobEvent,
        EnumLabJobState,
        list[EnumLabJobIntentKind],
    ]
] = [
    (
        "submit queues and dispatches a1",
        None,
        _ev(E.SUBMITTED, spec=_spec()),
        S.QUEUED,
        [IK.DISPATCH],
    ),
    (
        "take moves queued to dispatched",
        _row(S.QUEUED),
        _ev(E.TAKEN, attempt=1, runtime_id="rt-a"),
        S.DISPATCHED,
        [],
    ),
    (
        "decline keeps queued",
        _row(S.QUEUED),
        _ev(E.DECLINED, attempt=1, runtime_id="rt-a"),
        S.QUEUED,
        [],
    ),
    (
        "queued 30 min fails and alerts",
        _row(S.QUEUED),
        _ev(E.TICK, 30),
        S.ALERTING,
        [IK.ALERT],
    ),
    ("cancel a queued job", _row(S.QUEUED), _ev(E.CANCELLED), S.DONE, []),
    (
        "claim moves dispatched to running",
        _row(S.DISPATCHED, owner_runtime="rt-a"),
        _ev(E.CLAIMED, 1, attempt=1, run_id="r1"),
        S.RUNNING,
        [],
    ),
    (
        "no claim in 10 min stalls",
        _row(S.DISPATCHED, owner_runtime="rt-a"),
        _ev(E.TICK, 10),
        S.STALLED,
        [],
    ),
    (
        "unit ends before claim goes checking",
        _row(S.DISPATCHED, owner_runtime="rt-a"),
        _ev(E.ATTEMPT_ENDED, 2, attempt=1, outcome=OC.EXITED_ZERO),
        S.CHECKING,
        [],
    ),
    (
        "alive stays running",
        _row(S.RUNNING, claimed_at=T0),
        _ev(E.CHECKED, 30, attempt=1, verdict=V.ALIVE),
        S.RUNNING,
        [],
    ),
    (
        "unobservable stays running",
        _row(S.RUNNING, claimed_at=T0),
        _ev(E.CHECKED, 30, attempt=1, verdict=V.UNOBSERVABLE),
        S.RUNNING,
        [],
    ),
    (
        "dropped past grace stalls",
        _row(S.RUNNING, claimed_at=T0),
        _ev(E.CHECKED, 25, attempt=1, verdict=V.DROPPED),
        S.STALLED,
        [],
    ),
    (
        "dropped inside grace ignored",
        _row(S.RUNNING, claimed_at=T0),
        _ev(E.CHECKED, 5, attempt=1, verdict=V.DROPPED),
        S.RUNNING,
        [],
    ),
    (
        "terminated goes checking",
        _row(S.RUNNING, claimed_at=T0),
        _ev(E.CHECKED, 9, attempt=1, verdict=V.TERMINATED),
        S.CHECKING,
        [],
    ),
    (
        "attempt end goes checking",
        _row(S.RUNNING, claimed_at=T0),
        _ev(E.ATTEMPT_ENDED, 9, attempt=1, outcome=OC.EXITED_NONZERO),
        S.CHECKING,
        [],
    ),
    (
        "done rule holds",
        _row(S.CHECKING),
        _ev(E.DONE_EVALUATED, 1, attempt=1, done_rule_met=True),
        S.DONE,
        [],
    ),
    (
        "not done with attempts left retries",
        _row(S.CHECKING),
        _ev(E.DONE_EVALUATED, 1, attempt=1, done_rule_met=False),
        S.RETRYING,
        [],
    ),
    (
        "not done on last attempt alerts",
        _row(S.CHECKING, attempt=2, work_unit_id=f"{JOB}-a2"),
        _ev(E.DONE_EVALUATED, 1, attempt=2, done_rule_met=False),
        S.ALERTING,
        [IK.ALERT],
    ),
    (
        "time box with on_time_box=fail alerts",
        _row(
            S.CHECKING,
            time_box_hit=True,
            spec=_spec(
                retry_policy=ModelLabJobRetryPolicy(
                    on_time_box=EnumLabJobOnTimeBox.FAIL
                )
            ),
        ),
        _ev(E.DONE_EVALUATED, 1, attempt=1, done_rule_met=False),
        S.ALERTING,
        [IK.ALERT],
    ),
    (
        "stalled alive recovers",
        _row(S.STALLED, claimed_at=T0),
        _ev(E.CHECKED, 30, attempt=1, verdict=V.ALIVE),
        S.RUNNING,
        [],
    ),
    (
        "stalled not done stops the attempt",
        _row(S.STALLED),
        _ev(E.DONE_EVALUATED, 1, attempt=1, done_rule_met=False),
        S.STOPPING,
        [IK.KILL_ATTEMPT],
    ),
    (
        "stalled done rule holds",
        _row(S.STALLED),
        _ev(E.DONE_EVALUATED, 1, attempt=1, done_rule_met=True),
        S.DONE,
        [],
    ),
    (
        "stalled adopted with no brief alerts",
        _row(S.STALLED, kind=EnumLabJobKind.ADOPTED, spec=None, job_id=JOB),
        _ev(E.DONE_EVALUATED, 1, attempt=1, done_rule_met=False),
        S.ALERTING,
        [IK.ALERT],
    ),
    (
        "stop confirmed retries",
        _row(S.STOPPING),
        _ev(E.STOP_CONFIRMED, 2, attempt=1),
        S.RETRYING,
        [],
    ),
    (
        "stop unconfirmed in 10 min alerts",
        _row(S.STOPPING),
        _ev(E.TICK, 10),
        S.ALERTING,
        [IK.ALERT],
    ),
    (
        "retry after backoff requeues a2",
        _row(S.RETRYING, next_dispatch_at=T0 + timedelta(minutes=1)),
        _ev(E.DONE_EVALUATED, 2, attempt=1, done_rule_met=False),
        S.QUEUED,
        [IK.DISPATCH],
    ),
    (
        "retrying done rule holds",
        _row(S.RETRYING, next_dispatch_at=T0 + timedelta(minutes=5)),
        _ev(E.DONE_EVALUATED, 2, attempt=1, done_rule_met=True),
        S.DONE,
        [],
    ),
    (
        "cancel a retrying job",
        _row(S.RETRYING, next_dispatch_at=T0),
        _ev(E.CANCELLED),
        S.DONE,
        [],
    ),
    (
        "alert acknowledged",
        _row(S.ALERTING, alert_sent_at=T0),
        _ev(E.ALERT_ACKNOWLEDGED, 1, attempt=1),
        S.ALERTED,
        [],
    ),
    (
        "alert unacknowledged 15 min resends",
        _row(S.ALERTING, alert_sent_at=T0),
        _ev(E.TICK, 15),
        S.ALERTING,
        [IK.ALERT],
    ),
    (
        "resolve retry reopens",
        _row(S.ALERTED, attempt=2),
        _ev(E.RESOLVED, resolution=EnumLabJobResolution.RETRY),
        S.QUEUED,
        [IK.DISPATCH],
    ),
    (
        "resolve close ends",
        _row(S.ALERTED, attempt=2),
        _ev(E.RESOLVED, resolution=EnumLabJobResolution.CLOSE),
        S.DONE,
        [],
    ),
]


@pytest.mark.parametrize(
    ("row", "event", "state", "intents"),
    [pytest.param(r, e, s, i, id=n) for n, r, e, s, i in TABLE],
)
def test_transition_table(
    row: ModelLabJobRow | None,
    event: ModelLabJobEvent,
    state: EnumLabJobState,
    intents: list[EnumLabJobIntentKind],
) -> None:
    out = _reduce(row, event)
    assert out.applied, out.drop_reason
    assert out.row is not None
    assert out.row.state is state
    assert _kinds(out) == intents


def test_every_state_has_an_exit_or_is_terminal() -> None:
    exits: dict[EnumLabJobState, set[EnumLabJobState]] = {}
    for _n, row, _event, state, _i in TABLE:
        if row is not None and state is not row.state:
            exits.setdefault(row.state, set()).add(state)
    # failed is transient: the reducer records it and moves on to alerting.
    for st in EnumLabJobState:
        if st in TERMINAL_LAB_JOB_STATES or st is S.FAILED:
            continue
        assert exits.get(st), f"state {st} has no exit in the table"
    # the terminal states only reopen through resolved
    assert S.ALERTED in exits


def test_failed_is_recorded_before_alerting() -> None:
    out = _reduce(_row(S.QUEUED), _ev(E.TICK, 30))
    assert [(t.from_state, t.to_state) for t in out.transitions] == [
        (S.QUEUED, S.FAILED),
        (S.FAILED, S.ALERTING),
    ]
    assert out.row is not None
    assert out.row.failure_reason


def test_seq_advances_once_per_transition() -> None:
    out = _reduce(_row(S.QUEUED, seq=3), _ev(E.TICK, 30))
    assert [t.seq for t in out.transitions] == [4, 5]
    assert out.row is not None
    assert out.row.seq == 5


def test_two_runtimes_one_job_exactly_one_takes() -> None:
    """Two runtimes both receive the dispatch (a rebalance redelivery): one owns it."""
    queued = _reduce(None, _ev(E.SUBMITTED, spec=_spec())).row
    first = _reduce(queued, _ev(E.TAKEN, 1, attempt=1, runtime_id="rt-a"))
    assert first.applied
    assert first.row is not None
    assert first.row.state is S.DISPATCHED
    assert first.row.owner_runtime == "rt-a"

    second = _reduce(first.row, _ev(E.TAKEN, 1, attempt=1, runtime_id="rt-b"))
    assert not second.applied
    assert second.row == first.row
    assert [(i.kind, i.runtime_id) for i in second.intents] == [
        (IK.TAKE_REFUSED, "rt-b")
    ]

    again = _reduce(first.row, _ev(E.TAKEN, 2, attempt=1, runtime_id="rt-a"))
    assert not again.applied
    assert again.intents == ()


def test_declined_take_redispatches_after_backoff() -> None:
    declined = _reduce(_row(S.QUEUED), _ev(E.DECLINED, 0, attempt=1, runtime_id="rt-a"))
    assert declined.row is not None
    assert declined.row.next_dispatch_at == T0 + timedelta(seconds=60)
    early = _reduce(declined.row, _ev(E.TICK, 0.5))
    assert not early.applied
    due = _reduce(declined.row, _ev(E.TICK, 1))
    assert due.row is not None
    assert due.row.state is S.QUEUED
    assert [(i.kind, i.attempt, i.work_unit_id) for i in due.intents] == [
        (IK.DISPATCH, 1, f"{JOB}-a1")
    ]
    assert due.row.next_dispatch_at is None


def test_dispatch_carries_the_original_brief() -> None:
    out = _reduce(None, _ev(E.SUBMITTED, spec=_spec()))
    (intent,) = out.intents
    assert intent.brief == "ORIGINAL BRIEF: build the thing"
    assert intent.work_unit_id == f"{JOB}-a1"


def test_restart_is_built_from_the_original_brief() -> None:
    row = _row(S.RETRYING, next_dispatch_at=T0, continuation=None)
    out = _reduce(row, _ev(E.DONE_EVALUATED, 1, attempt=1, done_rule_met=False))
    assert out.row is not None
    assert out.row.attempt == 2
    (intent,) = out.intents
    assert intent.brief == "ORIGINAL BRIEF: build the thing"
    assert intent.work_unit_id == f"{JOB}-a2"
    assert out.row.owner_runtime is None


def test_time_box_split_is_a_continuation_of_the_original_brief() -> None:
    running = _row(S.RUNNING, claimed_at=T0)
    ended = _reduce(
        running,
        _ev(
            E.ATTEMPT_ENDED,
            31,
            attempt=1,
            outcome=OC.TIMED_OUT,
            last_status="STATUS step 3 of 5",
            head="c" * 40,
        ),
    ).row
    assert ended is not None
    assert ended.time_box_hit
    retrying = _reduce(
        ended, _ev(E.DONE_EVALUATED, 32, attempt=1, done_rule_met=False)
    ).row
    assert retrying is not None
    assert retrying.state is S.RETRYING
    out = _reduce(retrying, _ev(E.DONE_EVALUATED, 34, attempt=1, done_rule_met=False))
    (intent,) = out.intents
    assert intent.brief is not None
    assert intent.brief.startswith("ORIGINAL BRIEF: build the thing")
    assert "STATUS step 3 of 5" in intent.brief
    assert "c" * 40 in intent.brief
    assert "previous attempt hit its time box" in intent.brief
    # a second split starts again from the original brief, never nests
    assert out.row is not None
    assert out.row.spec is not None
    assert out.row.spec.brief == "ORIGINAL BRIEF: build the thing"


def test_alert_dedup_key_is_stable_across_resends() -> None:
    first = _reduce(_row(S.QUEUED), _ev(E.TICK, 30))
    resent = _reduce(first.row, _ev(E.TICK, 45))
    keys = {i.dedup_key for i in first.intents + resent.intents}
    assert keys == {f"lab-job:{JOB}:1:failed"}


def test_adopted_claim_without_brief_runs_and_never_restarts() -> None:
    out = _reduce(
        None,
        ModelLabJobEvent(
            kind=E.ADOPTED,
            job_id="adopted:run-123",
            at=T0,
            run_id="run-123",
            parent_lane="orchestrator",
            ticket="OMN-1",
        ),
    )
    assert out.row is not None
    assert out.row.state is S.RUNNING
    assert out.row.kind is EnumLabJobKind.ADOPTED
    assert out.row.spec is None
    assert out.row.claimed_at == T0
    assert out.intents == ()


def test_adopted_claim_with_recorded_brief_restarts_from_it() -> None:
    adopted = _reduce(
        None,
        ModelLabJobEvent(
            kind=E.ADOPTED, job_id="adopted:run-9", at=T0, run_id="run-9", spec=_spec()
        ),
    ).row
    assert adopted is not None
    assert adopted.spec is not None
    stalled = adopted.model_copy(update={"state": S.STALLED})
    out = _reduce(
        stalled,
        ModelLabJobEvent(
            kind=E.DONE_EVALUATED,
            job_id="adopted:run-9",
            at=T0 + timedelta(minutes=30),
            attempt=1,
            done_rule_met=False,
        ),
    )
    assert out.row is not None
    assert out.row.state is S.STOPPING


@pytest.mark.parametrize("state", sorted(TERMINAL_LAB_JOB_STATES))
def test_terminal_states_drop_everything_but_resolve(state: EnumLabJobState) -> None:
    out = _reduce(_row(state), _ev(E.CHECKED, 30, attempt=1, verdict=V.DROPPED))
    assert not out.applied
    assert out.drop_reason


def test_stale_attempt_events_are_dropped() -> None:
    row = _row(S.RUNNING, attempt=2, claimed_at=T0, work_unit_id=f"{JOB}-a2")
    out = _reduce(row, _ev(E.ATTEMPT_ENDED, 5, attempt=1, outcome=OC.EXITED_ZERO))
    assert not out.applied
    assert out.drop_reason == "stale_attempt"


def test_duplicate_submission_is_a_noop() -> None:
    out = _reduce(_row(S.RUNNING, claimed_at=T0), _ev(E.SUBMITTED, spec=_spec()))
    assert not out.applied
    assert out.drop_reason == "duplicate_submission"


def test_event_for_unknown_job_is_dropped() -> None:
    out = _reduce(None, _ev(E.TICK, 1))
    assert not out.applied
    assert out.row is None


def test_tick_with_nothing_due_is_dropped() -> None:
    out = _reduce(_row(S.RUNNING, claimed_at=T0), _ev(E.TICK, 1))
    assert not out.applied


def test_retry_before_backoff_waits() -> None:
    row = _row(S.RETRYING, next_dispatch_at=T0 + timedelta(minutes=5))
    out = _reduce(row, _ev(E.DONE_EVALUATED, 2, attempt=1, done_rule_met=False))
    assert not out.applied
    assert out.drop_reason == "backoff_pending"
