# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The compare step reads the controller's own decision from the tick record (OMN-23665).

Before this change the compare step paired the cutover repository with a synthetic no action; now a
record that carries the controller's finished tick is compared with the decision the controller made.
The measured case: omnimemory 713 held behind omnimemory 725 reads no action, not dispatch_worker.
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from omnimarket.models.landing_controller_carry import (
    ModelLandingControllerLogged,
    decisions_of,
)
from omnimarket.models.landing_decision import (
    EnumLandingActionKind,
    ModelLandingDecision,
    ModelLandingFacts,
)
from omnimarket.models.landing_tick_record import ModelLandingTickRecord
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.handler_pr_landing_snapshot import (
    carry_controller_state,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.handler_pr_landing_tick_orchestrator import (
    HandlerPrLandingTickOrchestrator,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_ports import (
    InMemoryTickPublisher,
    InMemoryTickWriter,
    ProtocolTickDecider,
    ProtocolTickWriter,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_sources import (
    InMemorySnapshotSource,
    RedpandaSnapshotSource,
    run_snapshots,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    EnumTickFailureReason,
    EnumTickMode,
    EnumTickStatus,
    ModelTickComparedEvent,
    ModelTickFailedEvent,
    ModelTickRequest,
    ModelTickResult,
    ModelTickSnapshot,
)
from tests.nodes.node_pr_landing_snapshot_effect.controller_inputs import (
    CONTROLLER_TICK,
    REPO,
    carry,
    node_facts_bare,
    pr,
)

pytestmark = pytest.mark.unit

COMPARED_TOPIC = "onex.evt.omnimarket.pr-landing-tick-compared.v1"
FAILED_TOPIC = "onex.evt.omnimarket.pr-landing-tick-compare-failed.v1"
HELD, NEW_RED = f"{REPO}#713", f"{REPO}#714"


def _handler(
    publisher: InMemoryTickPublisher | None = None,
    *,
    decider: ProtocolTickDecider | None = None,
    writer: ProtocolTickWriter | None = None,
    script_out_of_scope: tuple[str, ...] = (),
) -> HandlerPrLandingTickOrchestrator:
    return HandlerPrLandingTickOrchestrator(
        publisher=publisher,
        cutover_repository=REPO,
        decider=decider,
        writer=writer,
        script_out_of_scope=script_out_of_scope,
    )


def _compare(
    facts: ModelLandingFacts, handler: HandlerPrLandingTickOrchestrator, **over: object
) -> ModelTickResult:
    return handler.handle(
        ModelTickRequest.model_validate(
            {
                "correlation_id": uuid4(),
                "mode": EnumTickMode.OBSERVE,
                "facts": facts,
                "controller": decisions_of(carry()),
                **over,
            }
        )
    )


def test_the_measured_713_case_reads_no_action_and_agrees_with_the_controller() -> None:
    publisher = InMemoryTickPublisher()
    facts = carry_controller_state(node_facts_bare(), carry())
    result = _compare(facts, _handler(publisher))
    assert result.status is EnumTickStatus.COMPARED
    assert result.equal
    assert result.controller_tick == CONTROLLER_TICK
    assert result.compared_prs == (HELD, NEW_RED)
    assert result.agreed_prs == (HELD, NEW_RED)
    assert result.divergences == ()
    assert result.writes_performed == 0
    assert result.recorded_written is False
    ((topic, event),) = publisher.published
    assert topic == COMPARED_TOPIC
    assert isinstance(event, ModelTickComparedEvent)
    assert (event.controller_tick, event.compared_prs, event.agreed_prs) == (5, 2, 2)


def test_failing_control_without_the_carried_state_the_node_dispatches_for_the_held_pr() -> (
    None
):
    """The old facts (no state, no gate): the node says dispatch_worker, the controller holds it: named."""
    result = _compare(node_facts_bare(), _handler())
    assert result.status is EnumTickStatus.COMPARED
    assert not result.equal
    assert [d.subject for d in result.divergences] == [HELD]
    assert result.agreed_prs == (NEW_RED,)


def test_a_genuinely_new_red_dispatches_in_the_node_and_the_controller_agrees() -> None:
    facts = carry_controller_state(node_facts_bare(), carry())
    result = _compare(facts, _handler())
    row = {r.subject: r for r in result.rows}[NEW_RED]
    assert row.action_kinds == (EnumLandingActionKind.DISPATCH_WORKER.value,)
    assert NEW_RED in result.agreed_prs


def test_the_controllers_real_decision_replaces_the_synthetic_no_action() -> None:
    """Listed out of scope or not, a record that carries the controller's tick is never paired with a made-up no action."""
    facts = carry_controller_state(node_facts_bare(), carry())
    out_of_scope = _compare(facts, _handler(script_out_of_scope=(REPO,)))
    assert out_of_scope.equal
    assert out_of_scope.recorded_written is False
    assert out_of_scope.recorded_actions == 1  # the controller's one dispatch, for 714


def test_other_repositories_are_not_in_the_compared_scope() -> None:
    other = pr(9, "e").model_copy(update={"pr": "OmniNode-ai/omnibase_core#9"})
    controller_facts = carry().facts.model_copy(
        update={"prs": (*carry().facts.prs, other)}
    )
    both = carry().model_copy(update={"facts": controller_facts})
    facts = carry_controller_state(node_facts_bare(), both)
    result = _compare(facts, _handler(), controller=decisions_of(both))
    assert result.equal
    assert all(p.startswith(f"{REPO}#") for p in result.compared_prs)


def test_a_decision_row_the_node_does_not_decide_is_a_named_divergence() -> None:
    """The controller's real decision is its logged rows: a merge it logged that the node does not decide names the pull request."""
    publisher = InMemoryTickPublisher()
    logged = ModelLandingControllerLogged(
        kind=EnumLandingActionKind.MERGE, subject=HELD, outcome="merged"
    )
    result = _compare(
        carry_controller_state(node_facts_bare(), carry((logged,))),
        _handler(publisher),
        controller=decisions_of(carry((logged,))),
    )
    assert result.status is EnumTickStatus.COMPARED
    assert not result.equal
    assert {d.subject for d in result.divergences} == {HELD, NEW_RED}
    ((topic, event),) = publisher.published
    assert topic == COMPARED_TOPIC
    assert isinstance(event, ModelTickComparedEvent)
    assert set(event.divergent_subjects) == {HELD, NEW_RED}


def test_error_chain_a_record_with_no_controller_tick_is_unread_not_assumed() -> None:
    """OMN-22921: the controller decides the cutover repository, so a record that carries none of its ticks has no decision to compare with."""
    publisher = InMemoryTickPublisher()
    result = _handler(publisher, script_out_of_scope=(REPO,)).handle(
        ModelTickRequest(
            correlation_id=uuid4(), mode=EnumTickMode.OBSERVE, facts=node_facts_bare()
        )
    )
    assert result.status is EnumTickStatus.FAILED
    assert result.failure_reason is EnumTickFailureReason.CONTROLLER_TICK_MISSING
    assert result.recorded_written is False
    assert result.equal is False
    ((topic, event),) = publisher.published
    assert topic == FAILED_TOPIC
    assert isinstance(event, ModelTickFailedEvent)
    assert event.reason is EnumTickFailureReason.CONTROLLER_TICK_MISSING


class _RaisingDecider:
    def decide(
        self, facts: ModelLandingFacts, fair_share: bool
    ) -> ModelLandingDecision:
        raise RuntimeError("boom")


def test_error_chain_a_decider_that_raises_ends_in_the_typed_failure() -> None:
    result = _compare(node_facts_bare(), _handler(decider=_RaisingDecider()))
    assert result.status is EnumTickStatus.FAILED
    assert result.failure_reason is EnumTickFailureReason.DECISION_RAISED


@pytest.mark.parametrize("mode", [EnumTickMode.OBSERVE, EnumTickMode.REPLAY])
def test_no_observe_or_replay_tick_ever_writes(mode: EnumTickMode) -> None:
    writer = InMemoryTickWriter()
    facts = carry_controller_state(node_facts_bare(), carry())
    result = _compare(facts, _handler(writer=writer), mode=mode)
    assert result.writes_performed == 0
    assert writer.performed == []


def _record_json() -> str:
    return ModelLandingTickRecord(
        correlation_id=uuid4(),
        tick=1000,
        facts=carry_controller_state(node_facts_bare(), carry()),
        provenance="test",
        controller=decisions_of(carry()),
    ).model_dump_json()


class _Reader:
    def read(self, max_records: int, timeout_s: float) -> list[str]:
        del timeout_s
        return [_record_json()][:max_records]


def test_golden_chain_a_recorded_tick_with_the_controller_carry_ends_in_the_compared_event() -> (
    None
):
    publisher = InMemoryTickPublisher()
    source = RedpandaSnapshotSource(_Reader(), max_snapshots=5, poll_timeout_s=1.0)
    (result,) = run_snapshots(_handler(publisher), source, EnumTickMode.OBSERVE)
    assert result.equal
    ((topic, event),) = publisher.published
    assert topic == COMPARED_TOPIC
    assert isinstance(event, ModelTickComparedEvent)
    assert json.loads(event.model_dump_json())["controller_tick"] == CONTROLLER_TICK


def test_the_in_memory_source_carries_the_controller_into_the_request() -> None:
    snapshot = ModelTickSnapshot(
        facts=carry_controller_state(node_facts_bare(), carry()),
        controller=decisions_of(carry()),
    )
    (result,) = run_snapshots(
        _handler(), InMemorySnapshotSource([snapshot]), EnumTickMode.OBSERVE
    )
    assert result.controller_tick == CONTROLLER_TICK
