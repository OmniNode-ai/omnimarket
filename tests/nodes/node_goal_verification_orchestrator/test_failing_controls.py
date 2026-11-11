# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20033: each named injected break must turn the reading red (the model's mutants)."""

from __future__ import annotations

from datetime import datetime

import pytest

from omnimarket.nodes.node_goal_verification_orchestrator.handlers.handler_goal_verification import (
    HandlerGoalClosureResult,
    HandlerGoalDisposition,
    HandlerGoalVerification,
    HandlerGoalVerificationSweep,
)
from omnimarket.nodes.node_goal_verification_orchestrator.handlers.store_goal_verification_inmemory import (
    StoreGoalVerificationInMemory,
)
from omnimarket.nodes.node_goal_verification_orchestrator.models.model_goal_verification import (
    ModelGoalClosure,
    ModelGoalDispositionRecorded,
    ModelGoalVerificationDecision,
    ModelGoalVerificationRun,
    ModelGoalVerificationSweepCommand,
)
from tests.nodes.node_goal_verification_orchestrator.support import (
    GOAL,
    REV1,
    T0,
    RecordingPublisher,
    build_engine,
    event_id,
    result_event,
)

pytestmark = pytest.mark.unit


def _duplicate_close_reading(
    engine: HandlerGoalVerification, publisher: RecordingPublisher
) -> int:
    """The reading: published start commands after two distinct closes of one revision."""
    handler = HandlerGoalClosureResult(engine)
    handler.handle(result_event(1))
    handler.handle(result_event(2))
    return len(publisher.published)


def test_reading_passes_on_the_real_orchestrator() -> None:
    engine, _, publisher, _ = build_engine()
    assert _duplicate_close_reading(engine, publisher) == 1


def test_control_a_close_that_ignores_the_recorded_run_starts_two() -> None:
    """Break `no_dedupe`: every close starts; the reading must see two."""

    class NoDedupe(HandlerGoalVerification):
        def close(self, closure: ModelGoalClosure) -> ModelGoalVerificationDecision:
            self.store.record_close(closure)
            return self._start(closure.goal_id, closure.contract_revision, created=True)

    real, store, publisher, clock = build_engine()
    broken = NoDedupe(
        rules=real.rules,
        store=store,
        contract_reader=real.contract_reader,
        publisher=publisher,
        clock=clock,
    )
    assert _duplicate_close_reading(broken, publisher) == 2


def test_control_a_disposition_that_starts_a_run_is_seen() -> None:
    """Break `late_disposition_starts`: the reading counts publications, not a marker."""

    class DispositionStarts(HandlerGoalVerification):
        def disposition(
            self, event: ModelGoalDispositionRecorded
        ) -> ModelGoalVerificationDecision:
            decision = super().disposition(event)
            self._start(event.goal_id, event.contract_revision, created=False)
            return decision

    real, store, publisher, clock = build_engine()
    broken = DispositionStarts(
        rules=real.rules,
        store=store,
        contract_reader=real.contract_reader,
        publisher=publisher,
        clock=clock,
    )
    HandlerGoalClosureResult(broken).handle(result_event(1))
    before = len(publisher.published)
    HandlerGoalDisposition(broken).handle(
        ModelGoalDispositionRecorded(
            event_id=event_id(50),
            goal_id=GOAL,
            contract_revision=REV1,
            disposition="accepted",
            emitted_at=T0,
        )
    )
    assert len(publisher.published) == before + 1


def test_control_an_unbounded_retry_exceeds_the_contract_bound() -> None:
    """Break `unbounded_retry`: the sweep never writes the obligation, attempts pass the bound."""

    class Unbounded(HandlerGoalVerification):
        def sweep(self, now: datetime) -> tuple[ModelGoalVerificationDecision, ...]:
            return tuple(
                self._start(run.goal_id, run.contract_revision, created=False)
                for run in self.store.list_unverified()
            )

    real, store, publisher, clock = build_engine(rules_override={"retry_bound": 2})
    broken = Unbounded(
        rules=real.rules,
        store=store,
        contract_reader=real.contract_reader,
        publisher=publisher,
        clock=clock,
    )
    HandlerGoalClosureResult(broken).handle(result_event(1))
    for _ in range(4):
        HandlerGoalVerificationSweep(broken).handle(
            ModelGoalVerificationSweepCommand(
                now=clock.advance(broken.rules.attempt_timeout_s)
            )
        )
    run = store.get_run(GOAL, REV1)
    assert run is not None
    assert run.attempts > broken.rules.retry_bound


def test_control_a_run_dropped_instead_of_listed_is_seen() -> None:
    """Break `drop_obligation`: an exhausted run vanishes from the listing."""

    class HidesOpenRuns(StoreGoalVerificationInMemory):
        hide = False

        def list_unverified(self) -> tuple[ModelGoalVerificationRun, ...]:
            return () if self.hide else super().list_unverified()

    store = HidesOpenRuns()
    engine, _, _, clock = build_engine(rules_override={"retry_bound": 1}, store=store)
    HandlerGoalClosureResult(engine).handle(result_event(1))
    HandlerGoalVerificationSweep(engine).handle(
        ModelGoalVerificationSweepCommand(
            now=clock.advance(engine.rules.attempt_timeout_s)
        )
    )
    assert len(engine.unverified().runs) == 1
    store.hide = True
    assert engine.unverified().runs == ()
