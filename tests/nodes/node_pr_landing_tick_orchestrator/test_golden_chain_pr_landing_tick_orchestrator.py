# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain and error chain of the landing tick orchestrator (OMN-22921).

Golden: a recorded tick enters, the compared event ends the chain on the contract topic.
Error: a tick that cannot be compared ends the chain with the failed event on the failure topic.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path
from typing import get_type_hints
from uuid import uuid4

import pytest
import yaml
from pydantic import BaseModel

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.landing_decision import ModelLandingDecision, ModelLandingFacts
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.handler_pr_landing_tick_orchestrator import (
    HandlerPrLandingTickOrchestrator,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_ports import (
    InMemoryTickPublisher,
    ProtocolTickDecider,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    EnumTickFailureReason,
    EnumTickMode,
    EnumTickStatus,
    ModelTickComparedEvent,
    ModelTickFailedEvent,
    ModelTickRequest,
    ModelTickSnapshot,
)

pytestmark = pytest.mark.unit

NODE = "node_pr_landing_tick_orchestrator"
PACKAGE = f"omnimarket.nodes.{NODE}"
REPO = Path(__file__).resolve().parents[3]
COMPARED_TOPIC = "onex.evt.omnimarket.pr-landing-tick-compared.v1"
FAILED_TOPIC = "onex.evt.omnimarket.pr-landing-tick-compare-failed.v1"
COMMAND_TOPIC = "onex.cmd.omnimarket.pr-landing-tick-compare-requested.v1"
SNAPSHOT = (
    REPO / "tests" / "fixtures" / "pr_landing_tick_replay" / "tick_725_omnimarket.json"
)


def _snapshot() -> ModelTickSnapshot:
    return ModelTickSnapshot.model_validate_json(SNAPSHOT.read_text(encoding="utf-8"))


def _request(
    mode: EnumTickMode = EnumTickMode.OBSERVE, **over: object
) -> ModelTickRequest:
    snap = _snapshot()
    base = {
        "correlation_id": uuid4(),
        "mode": mode,
        "facts": snap.facts,
        "recorded_decision": snap.decision,
        "fair_share": snap.fair_share,
    }
    return ModelTickRequest.model_validate({**base, **over})


def test_node_is_registered_and_its_contract_binds_the_handler() -> None:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["entry-points"]["onex.nodes"][NODE] == PACKAGE
    package = importlib.import_module(PACKAGE)
    assert package.NodePrLandingTickOrchestrator.contract_path.is_file()
    root = Path(package.__file__ or "").parent
    contract = yaml.safe_load((root / "contract.yaml").read_text())
    metadata = yaml.safe_load((root / "metadata.yaml").read_text())
    assert contract["name"] == metadata["name"] == NODE
    assert contract["node_type"] == "ORCHESTRATOR_GENERIC"
    assert contract["metadata"]["related_tickets"] == ["OMN-22921"]
    binding = contract["handler"]
    handler_class = getattr(
        importlib.import_module(binding["module"]), binding["class"]
    )

    def model(ref: str) -> type[BaseModel]:
        module, _, name = ref.rpartition(".")
        result: object = getattr(importlib.import_module(module), name)
        assert isinstance(result, type)
        assert issubclass(result, BaseModel)
        assert result.model_config == {"frozen": True, "extra": "forbid"}
        return result

    hints = get_type_hints(handler_class.handle)
    assert hints["request"] is model(binding["input_model"])
    assert hints["return"] is model(binding["output_model"])
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [
        COMPARED_TOPIC,
        FAILED_TOPIC,
        "onex.evt.omnimarket.pr-landing-rerun-completed.v1",
        "onex.evt.omnimarket.pr-landing-rerun-failed.v1",
        "onex.cmd.omnimarket.pr-landing-rerun-requested.v1",
    ]
    assert contract["runtime_dispatch"]["terminal_events"] == {
        "success": COMPARED_TOPIC,
        "failure": FAILED_TOPIC,
    }
    assert contract["overlay"]["id"] == NODE


def test_golden_chain_a_recorded_tick_ends_in_the_compared_event() -> None:
    publisher = InMemoryTickPublisher()
    result = HandlerPrLandingTickOrchestrator(publisher=publisher).handle(_request())
    assert result.status is EnumTickStatus.COMPARED
    assert result.equal is True
    [(topic, event)] = publisher.published
    assert topic == COMPARED_TOPIC
    assert isinstance(event, ModelTickComparedEvent)
    assert event.correlation_id == result.correlation_id
    assert event.divergent_subjects == ()
    assert event.writes_performed == 0


def test_golden_chain_a_divergent_tick_names_the_pull_request_in_the_event() -> None:
    publisher = InMemoryTickPublisher()
    snap = _snapshot()
    target = snap.decision.gates[0].pr
    planted = snap.decision.model_copy(
        update={"gates": tuple(g for g in snap.decision.gates if g.pr != target)}
    )
    HandlerPrLandingTickOrchestrator(publisher=publisher).handle(
        _request(recorded_decision=planted)
    )
    [(topic, event)] = publisher.published
    assert topic == COMPARED_TOPIC
    assert isinstance(event, ModelTickComparedEvent)
    assert event.equal is False
    assert event.divergent_subjects == (target,)


def test_error_chain_act_mode_without_the_overlay_ends_in_the_failed_event() -> None:
    publisher = InMemoryTickPublisher()
    result = HandlerPrLandingTickOrchestrator(publisher=publisher).handle(
        _request(EnumTickMode.ACT)
    )
    assert result.status is EnumTickStatus.FAILED
    [(topic, event)] = publisher.published
    assert topic == FAILED_TOPIC
    assert isinstance(event, ModelTickFailedEvent)
    assert event.reason is EnumTickFailureReason.ACT_NOT_ENABLED


def test_error_chain_a_tick_with_no_recorded_decision_ends_in_the_failed_event() -> (
    None
):
    publisher = InMemoryTickPublisher()
    result = HandlerPrLandingTickOrchestrator(publisher=publisher).handle(
        _request(EnumTickMode.REPLAY, recorded_decision=None)
    )
    assert result.failure_reason is EnumTickFailureReason.RECORDED_DECISION_MISSING
    assert [t for t, _ in publisher.published] == [FAILED_TOPIC]
    # observe mode names the cause as the controller's missing tick (OMN-22921)
    observed = HandlerPrLandingTickOrchestrator(
        publisher=InMemoryTickPublisher()
    ).handle(_request(EnumTickMode.OBSERVE, recorded_decision=None))
    assert observed.failure_reason is EnumTickFailureReason.CONTROLLER_TICK_MISSING


class _RaisingDecider:
    def decide(
        self, facts: ModelLandingFacts, fair_share: bool
    ) -> ModelLandingDecision:
        raise RuntimeError("decision node down")


def test_error_chain_a_decider_that_raises_ends_in_the_failed_event_not_an_exception() -> (
    None
):
    publisher = InMemoryTickPublisher()
    result = HandlerPrLandingTickOrchestrator(
        decider=_RaisingDecider(), publisher=publisher
    ).handle(_request())
    assert result.status is EnumTickStatus.FAILED
    assert result.failure_reason is EnumTickFailureReason.DECISION_RAISED
    assert [t for t, _ in publisher.published] == [FAILED_TOPIC]
    assert result.writes_performed == 0


# --- the floor's emission chains: a start, then a success with counters or a typed failure ---

FLOOR_PIECE = 9


def _emitting(
    sink: InMemoryEmissionSink,
    *,
    drill: bool = False,
    decider: ProtocolTickDecider | None = None,
) -> HandlerPrLandingTickOrchestrator:
    return HandlerPrLandingTickOrchestrator(
        emission_sink=sink, floor_piece=FLOOR_PIECE, drill=drill, decider=decider
    )


def test_golden_floor_chain_is_a_start_then_a_success_with_counters() -> None:
    sink = InMemoryEmissionSink()
    request = _request()
    _emitting(sink).handle(request)
    assert [e.phase for e in sink.events] == ["start", "success"]
    assert {e.correlation_id for e in sink.events} == {request.correlation_id.hex}
    assert {e.piece for e in sink.events} == {FLOOR_PIECE}
    assert {e.node for e in sink.events} == {NODE}
    assert sink.events[1].counters["writes_performed"] == 0
    assert sink.events[1].counters["subjects"] > 1


def test_error_floor_chain_is_a_start_then_a_typed_failure() -> None:
    sink = InMemoryEmissionSink()
    _emitting(sink, drill=True).handle(_request(EnumTickMode.ACT))
    assert [e.phase for e in sink.events] == ["start", "failure"]
    assert sink.events[1].failure_code == EnumTickFailureReason.ACT_NOT_ENABLED.value
    assert all(e.drill for e in sink.events)


def test_a_decider_that_raises_still_closes_the_floor_chain_with_a_failure() -> None:
    sink = InMemoryEmissionSink()
    _emitting(sink, decider=_RaisingDecider()).handle(_request())
    assert [e.phase for e in sink.events] == ["start", "failure"]
    assert sink.events[1].failure_code == EnumTickFailureReason.DECISION_RAISED.value


def test_the_floor_receipt_fold_counts_the_golden_and_the_error_chain() -> None:
    from datetime import UTC, datetime, timedelta

    from omnimarket.models.floor_17a import (
        ModelFloorReceiptInput,
        ModelPieceDef,
    )
    from omnimarket.nodes.node_floor_emission_compute.handlers.handler_floor_receipt import (
        HandlerFloorReceipt,
    )

    sink = InMemoryEmissionSink()
    _emitting(sink).handle(_request())
    _emitting(sink, drill=True).handle(_request(EnumTickMode.ACT))
    receipts = HandlerFloorReceipt().handle(
        ModelFloorReceiptInput(
            now=datetime.now(UTC) + timedelta(seconds=1),
            emissions=tuple(sink.events),
            pieces=(
                ModelPieceDef(
                    piece=FLOOR_PIECE,
                    name="Landing and merge queue",
                    reading_max_age_s=3600,
                ),
            ),
            joins=(),
            freshness_max_age_s=3600,
            error_max_age_s=3600,
            chain_deadline_s=600,
        )
    )
    piece = next(p for p in receipts.pieces if p.piece == FLOOR_PIECE)
    assert piece.golden_at is not None
    assert piece.error_at is not None
    assert receipts.findings == ()


# --- named mutations: each chain test goes red when its mutation is injected ---


class _SwallowingPublisher(InMemoryTickPublisher):
    """Mutation drop_compared_event: the publisher takes the event and sends nothing."""

    def publish(self, topic: str, event: BaseModel) -> None:
        return None


class _WrongTopicPublisher(InMemoryTickPublisher):
    """Mutation failure_on_success_topic: a failed tick is announced as compared."""

    def publish(self, topic: str, event: BaseModel) -> None:
        super().publish(COMPARED_TOPIC, event)


def _golden_chain(publisher: InMemoryTickPublisher) -> None:
    HandlerPrLandingTickOrchestrator(publisher=publisher).handle(_request())
    assert [t for t, _ in publisher.published] == [COMPARED_TOPIC]


def _error_chain(publisher: InMemoryTickPublisher) -> None:
    HandlerPrLandingTickOrchestrator(publisher=publisher).handle(
        _request(EnumTickMode.ACT)
    )
    assert [t for t, _ in publisher.published] == [FAILED_TOPIC]


NAMED_MUTATIONS = {
    "drop_compared_event": (_golden_chain, _SwallowingPublisher),
    "failure_on_success_topic": (_error_chain, _WrongTopicPublisher),
}


@pytest.mark.parametrize("name", sorted(NAMED_MUTATIONS))
def test_chain_test_goes_red_under_its_named_mutation(name: str) -> None:
    check, mutated = NAMED_MUTATIONS[name]
    with pytest.raises(AssertionError):
        check(mutated())


def test_chain_tests_are_green_without_a_mutation() -> None:
    _golden_chain(InMemoryTickPublisher())
    _error_chain(InMemoryTickPublisher())


class _RaisingPublisher:
    def publish(self, topic: str, event: BaseModel) -> None:
        raise RuntimeError("bus down")


def test_a_publisher_that_raises_still_closes_the_floor_chain_with_a_failure() -> None:
    sink = InMemoryEmissionSink()
    result = HandlerPrLandingTickOrchestrator(
        publisher=_RaisingPublisher(), emission_sink=sink, floor_piece=FLOOR_PIECE
    ).handle(_request())
    assert [e.phase for e in sink.events] == ["start", "failure"]
    assert sink.events[1].failure_code == EnumTickFailureReason.TICK_STAGE_RAISED.value
    assert result.status is EnumTickStatus.FAILED
