# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20033 (GC.10): closing a goal starts one verification run per goal and revision."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

import pytest

from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_terminal_v2 import (
    ModelDelegationTerminalCompletedV2,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_completed_event import (
    ModelDodVerifyCompletedEvent,
)
from omnimarket.nodes.node_goal_verification_orchestrator.handlers.handler_goal_verification import (
    HandlerGoalClosureDelegationTerminal,
    HandlerGoalClosureResult,
    HandlerGoalDisposition,
    HandlerGoalVerdictObserved,
    HandlerGoalVerificationSweep,
    HandlerUnverifiedCloses,
)
from omnimarket.nodes.node_goal_verification_orchestrator.handlers.store_goal_verification_inmemory import (
    StoreGoalVerificationInMemory,
)
from omnimarket.nodes.node_goal_verification_orchestrator.models.model_goal_verification import (
    EnumGoalVerificationAction as Action,
)
from omnimarket.nodes.node_goal_verification_orchestrator.models.model_goal_verification import (
    EnumGoalVerificationState as State,
)
from omnimarket.nodes.node_goal_verification_orchestrator.models.model_goal_verification import (
    ModelGoalDispositionRecorded,
    ModelGoalVerificationRun,
    ModelGoalVerificationSweepCommand,
    ModelUnverifiedClosesQuery,
)
from tests.nodes.node_goal_verification_orchestrator.support import (
    DELEGATION_RUN,
    GOAL,
    REV1,
    REV2,
    T0,
    DictContractReader,
    build_engine,
    event_id,
    goal_contract,
    result_event,
)

pytestmark = pytest.mark.unit


def _completed(
    goal: UUID = GOAL, revision: UUID = REV1
) -> ModelDodVerifyCompletedEvent:
    return ModelDodVerifyCompletedEvent.model_construct(
        correlation_id=event_id(900),
        ticket_id="OMN-20033",
        goal_id=goal,
        contract_revision=revision,
    )


def test_closing_a_goal_starts_one_verifier_run_with_the_goal_contract() -> None:
    engine, store, publisher, _ = build_engine()
    outcome = HandlerGoalClosureResult(engine).handle(result_event(1))
    [decision] = outcome.decisions
    assert decision.action is Action.STARTED
    assert decision.state is State.STARTED
    [command] = publisher.published
    assert command.goal_id == GOAL
    assert command.contract_revision == REV1
    assert command.ticket_id == "OMN-20033"
    assert command.dod_evidence == goal_contract().dod_evidence
    assert command.correlation_id == engine.run_correlation_id(GOAL, REV1)
    run = store.get_run(GOAL, REV1)
    assert run is not None
    assert run.attempts == 1
    assert run.closing_event_ids == (event_id(1),)


def test_duplicate_close_one_run_same_event_delivered_twice() -> None:
    engine, store, publisher, _ = build_engine()
    handler = HandlerGoalClosureResult(engine)
    first = handler.handle(result_event(1))
    second = handler.handle(result_event(1))
    assert first.decisions[0].action is Action.STARTED
    assert second.decisions[0].action is Action.DUPLICATE_CLOSE
    assert len(publisher.published) == 1
    run = store.get_run(GOAL, REV1)
    assert run is not None
    assert run.attempts == 1


def test_duplicate_close_one_run_two_distinct_closing_events_for_one_revision() -> None:
    # Finding F1: the key is goal id and revision. A key that included the closing event id
    # would start two runs here.
    engine, store, publisher, _ = build_engine()
    handler = HandlerGoalClosureResult(engine)
    handler.handle(result_event(1))
    second = handler.handle(result_event(2))
    assert second.decisions[0].action is Action.DUPLICATE_CLOSE
    assert publisher.runs_started == 1
    assert len(publisher.published) == 1
    run = store.get_run(GOAL, REV1)
    assert run is not None
    assert run.closing_event_ids == (event_id(1), event_id(2))


def test_duplicate_close_one_run_redelivery_after_publish_crash_starts_the_same_run() -> (
    None
):
    # Publish first, record second: the attempt record is lost to a crash, the close is
    # redelivered, the command is published again under the same verifier run key.
    class CrashesOnceBeforeRecording(StoreGoalVerificationInMemory):
        crashed = False

        def mark_attempt(
            self, goal_id: UUID, contract_revision: UUID, at: datetime
        ) -> ModelGoalVerificationRun:
            if not self.crashed:
                self.crashed = True
                raise RuntimeError("orchestrator crashed between publish and record")
            return super().mark_attempt(goal_id, contract_revision, at)

    store = CrashesOnceBeforeRecording()
    engine, _, publisher, _ = build_engine(store=store)
    handler = HandlerGoalClosureResult(engine)
    with pytest.raises(RuntimeError):
        handler.handle(result_event(1))
    pending = store.get_run(GOAL, REV1)
    assert pending is not None
    assert pending.state is State.PENDING
    handler.handle(result_event(1))
    assert len(publisher.published) == 2
    assert publisher.runs_started == 1


def test_a_new_contract_revision_is_a_new_run() -> None:
    reader = DictContractReader()
    reader.add(goal_contract(REV1))
    reader.add(goal_contract(REV2))
    engine, _, publisher, _ = build_engine(reader=reader)
    handler = HandlerGoalClosureResult(engine)
    handler.handle(result_event(1, revision=REV1))
    handler.handle(result_event(2, revision=REV2))
    assert publisher.runs_started == 2


def test_a_late_disposition_is_recorded_and_starts_nothing() -> None:
    engine, store, publisher, _ = build_engine()
    HandlerGoalClosureResult(engine).handle(result_event(1))
    before = len(publisher.published)
    disposition = ModelGoalDispositionRecorded(
        event_id=event_id(50),
        goal_id=GOAL,
        contract_revision=REV1,
        disposition="accepted",
        emitted_at=T0,
    )
    decision = HandlerGoalDisposition(engine).handle(disposition).decisions[0]
    assert decision.action is Action.DISPOSITION_RECORDED
    assert len(publisher.published) == before
    run = store.get_run(GOAL, REV1)
    assert run is not None
    assert run.disposition_event_ids == (event_id(50),)


def test_a_disposition_before_any_close_creates_no_run() -> None:
    engine, store, publisher, _ = build_engine()
    HandlerGoalDisposition(engine).handle(
        ModelGoalDispositionRecorded(
            event_id=event_id(50),
            goal_id=GOAL,
            contract_revision=REV1,
            disposition="accepted",
            emitted_at=T0,
        )
    )
    assert publisher.published == []
    assert store.get_run(GOAL, REV1) is None
    assert store.list_unverified() == ()


def test_a_delegation_terminal_for_a_call_goal_starts_its_run() -> None:
    reader = DictContractReader()
    call_goal = goal_contract().model_copy(update={"level": "delegate_call"})
    reader.by_delegation[DELEGATION_RUN] = call_goal
    reader.add(call_goal)
    engine, _, publisher, _ = build_engine(reader=reader)
    terminal = ModelDelegationTerminalCompletedV2.model_construct(
        correlation_id=DELEGATION_RUN
    )
    decision = (
        HandlerGoalClosureDelegationTerminal(engine).handle(terminal).decisions[0]
    )
    assert decision.action is Action.STARTED
    [command] = publisher.published
    assert command.level == "delegate_call"
    assert command.delegation_correlation_id == DELEGATION_RUN


def test_a_delegation_terminal_with_no_goal_starts_nothing() -> None:
    engine, store, publisher, _ = build_engine()
    terminal = ModelDelegationTerminalCompletedV2.model_construct(
        correlation_id=DELEGATION_RUN
    )
    decision = (
        HandlerGoalClosureDelegationTerminal(engine).handle(terminal).decisions[0]
    )
    assert decision.action is Action.NO_GOAL
    assert publisher.published == []
    assert store.list_unverified() == ()


def test_a_result_that_closes_no_goal_starts_nothing() -> None:
    engine, store, publisher, _ = build_engine()
    event = result_event(1).model_copy(update={"closes_claims": frozenset()})
    decision = HandlerGoalClosureResult(engine).handle(event).decisions[0]
    assert decision.action is Action.NO_GOAL
    assert publisher.published == []
    assert store.list_unverified() == ()


def test_verifier_failure_is_retried_up_to_the_bound_then_an_obligation_is_listed() -> (
    None
):
    engine, store, publisher, clock = build_engine(rules_override={"retry_bound": 2})
    HandlerGoalClosureResult(engine).handle(result_event(1))
    sweep = HandlerGoalVerificationSweep(engine)
    timeout = engine.rules.attempt_timeout_s
    inside = sweep.handle(
        ModelGoalVerificationSweepCommand(now=clock.advance(timeout - 1))
    )
    assert inside.decisions[0].action is Action.WAITING
    retried = sweep.handle(ModelGoalVerificationSweepCommand(now=clock.advance(1)))
    assert retried.decisions[0].action is Action.RETRIED
    assert retried.decisions[0].attempts == 2
    exhausted = sweep.handle(
        ModelGoalVerificationSweepCommand(now=clock.advance(timeout))
    )
    assert exhausted.decisions[0].action is Action.EXHAUSTED
    run = store.get_run(GOAL, REV1)
    assert run is not None
    assert run.state is State.EXHAUSTED
    assert run.attempts == 2
    assert run.obligation_reason == "verifier_attempts_exhausted"
    assert publisher.runs_started == 1
    assert len(publisher.published) == 2
    # The bound is never exceeded: a further sweep does nothing.
    again = sweep.handle(
        ModelGoalVerificationSweepCommand(now=clock.advance(timeout * 5))
    )
    assert again.decisions == ()
    assert len(publisher.published) == 2


def test_a_late_verdict_clears_the_obligation() -> None:
    engine, store, _, clock = build_engine(rules_override={"retry_bound": 1})
    HandlerGoalClosureResult(engine).handle(result_event(1))
    HandlerGoalVerificationSweep(engine).handle(
        ModelGoalVerificationSweepCommand(
            now=clock.advance(engine.rules.attempt_timeout_s)
        )
    )
    exhausted = store.get_run(GOAL, REV1)
    assert exhausted is not None
    assert exhausted.state is State.EXHAUSTED
    bound = HandlerGoalVerdictObserved(engine).handle(_completed()).decisions[0]
    assert bound.action is Action.VERDICT_BOUND
    run = store.get_run(GOAL, REV1)
    assert run is not None
    assert run.state is State.VERDICT
    assert run.obligation_reason is None
    assert store.list_unverified() == ()


def test_a_verdict_for_a_close_never_delivered_is_unmatched() -> None:
    engine, store, _, _ = build_engine()
    decision = HandlerGoalVerdictObserved(engine).handle(_completed()).decisions[0]
    assert decision.action is Action.VERDICT_UNMATCHED
    assert store.get_run(GOAL, REV1) is None


def test_unverified_close_listed_from_the_moment_it_is_delivered() -> None:
    engine, _, publisher, _ = build_engine()
    publisher.fail_next = True
    with pytest.raises(ConnectionError):
        HandlerGoalClosureResult(engine).handle(result_event(1))
    listed = HandlerUnverifiedCloses(engine).handle(ModelUnverifiedClosesQuery())
    [run] = listed.runs
    assert (run.goal_id, run.contract_revision, run.state) == (
        GOAL,
        REV1,
        State.PENDING,
    )


def test_unverified_close_listed_when_the_contract_cannot_be_read() -> None:
    engine, _, publisher, clock = build_engine(reader=DictContractReader())
    decision = HandlerGoalClosureResult(engine).handle(result_event(1)).decisions[0]
    assert decision.action is Action.CONTRACT_UNREAD
    assert publisher.published == []
    listed = HandlerUnverifiedCloses(engine).handle(ModelUnverifiedClosesQuery())
    assert [run.state for run in listed.runs] == [State.PENDING]
    sweep = HandlerGoalVerificationSweep(engine)
    for _ in range(engine.rules.retry_bound + 1):
        sweep.handle(
            ModelGoalVerificationSweepCommand(
                now=clock.advance(engine.rules.attempt_timeout_s)
            )
        )
    [run] = HandlerUnverifiedCloses(engine).handle(ModelUnverifiedClosesQuery()).runs
    assert run.state is State.EXHAUSTED
    assert run.obligation_reason == "contract_unread"


def test_unverified_close_listed_after_the_verifier_is_lost_and_dropped_once_a_verdict_binds() -> (
    None
):
    engine, _, _, clock = build_engine(rules_override={"retry_bound": 1})
    HandlerGoalClosureResult(engine).handle(result_event(1))
    HandlerGoalVerificationSweep(engine).handle(
        ModelGoalVerificationSweepCommand(
            now=clock.advance(engine.rules.attempt_timeout_s)
        )
    )
    listed = HandlerUnverifiedCloses(engine).handle(ModelUnverifiedClosesQuery())
    assert [(r.state, r.obligation_reason) for r in listed.runs] == [
        (State.EXHAUSTED, "verifier_attempts_exhausted")
    ]
    HandlerGoalVerdictObserved(engine).handle(_completed())
    assert (
        HandlerUnverifiedCloses(engine).handle(ModelUnverifiedClosesQuery()).runs == ()
    )


def test_the_listing_filters_on_a_goal() -> None:
    engine, _, _, _ = build_engine()
    HandlerGoalClosureResult(engine).handle(result_event(1))
    other = UUID("00000000-0000-4000-8000-0000000000aa")
    assert (
        HandlerUnverifiedCloses(engine)
        .handle(ModelUnverifiedClosesQuery(goal_id=other))
        .runs
        == ()
    )
    assert (
        len(
            HandlerUnverifiedCloses(engine)
            .handle(ModelUnverifiedClosesQuery(goal_id=GOAL))
            .runs
        )
        == 1
    )


def test_a_node_wired_without_a_publisher_cannot_drop_a_close() -> None:
    from omnimarket.nodes.node_goal_verification_orchestrator.handlers.handler_goal_verification import (
        HandlerGoalVerification,
    )

    reader = DictContractReader()
    reader.add(goal_contract())
    engine = HandlerGoalVerification(contract_reader=reader)
    with pytest.raises(RuntimeError, match="no verifier start publisher"):
        HandlerGoalClosureResult(engine).handle(result_event(1))
    [run] = engine.unverified().runs
    assert run.state is State.PENDING
