# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing snapshot step: each tick's facts filled and recorded on the bus (OMN-23409).

Golden: two consecutive ticks each end in one record on the overlay's record topic, carrying the
PR node ids and the failed run ids the write-facts source gave at the facts' heads; after the
second tick the first tick's facts are still recoverable, field for field.
Error: an unread tick, a tick the source returns under another number and a tick already recorded
each end in the failed event on the overlay's failed topic, and no earlier record is replaced.
Failing control: a recorder that keeps only the last tick (the script's one overwritten facts file)
loses the first tick's facts after the second tick.
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import BaseModel, ValidationError

from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.handler_pr_landing_snapshot import (
    HandlerPrLandingSnapshot,
    fill_write_facts,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.snapshot_ports import (
    InMemoryLandingFactsSource,
    InMemoryLandingSnapshotPublisher,
    InMemoryLandingTickRecorder,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_landing_snapshot import (
    EnumSnapshotFailureReason,
    EnumSnapshotStatus,
    ModelLandingSnapshotFailedEvent,
    ModelLandingSnapshotRequest,
    ModelLandingTickRecord,
    ModelPrWriteFacts,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_snapshot_overlay import (
    ModelSnapshotOverlay,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.protocols.protocol_landing_snapshot import (
    ProtocolLandingTickRecorder,
)
from tests.nodes.node_pr_landing_snapshot_effect.snapshot_inputs import (
    FIRST,
    FIXED_ID,
    NODE,
    SECOND,
    facts_source,
    overlay,
    scenario_facts,
    write_facts,
    write_source,
)

pytestmark = pytest.mark.unit

RECORDED_TOPIC = "onex.evt.omnimarket.pr-landing-tick-facts-recorded.v1"
FAILED_TOPIC = "onex.evt.omnimarket.pr-landing-snapshot-failed.v1"
COMMAND_TOPIC = "onex.cmd.omnimarket.pr-landing-snapshot-requested.v1"


class LastOnlyTickRecorder:
    """The named injected break: keeps one record and overwrites it every tick, as the script's facts file."""

    def __init__(self) -> None:
        self._last: ModelLandingTickRecord | None = None

    def record(self, record: ModelLandingTickRecord) -> None:
        self._last = record

    def recorded(self) -> tuple[ModelLandingTickRecord, ...]:
        return () if self._last is None else (self._last,)


def _handler(
    recorder: ProtocolLandingTickRecorder,
    publisher: InMemoryLandingSnapshotPublisher,
    source: InMemoryLandingFactsSource | None = None,
) -> HandlerPrLandingSnapshot:
    return HandlerPrLandingSnapshot(
        overlay=overlay(),
        facts_source=source or facts_source(),
        write_source=write_source(),
        recorder=recorder,
        publisher=publisher,
    )


def _two_ticks(
    recorder: ProtocolLandingTickRecorder,
) -> InMemoryLandingSnapshotPublisher:
    publisher = InMemoryLandingSnapshotPublisher()
    handler = _handler(recorder, publisher)
    for tick in (FIRST, SECOND):
        result = handler.handle(
            ModelLandingSnapshotRequest(correlation_id=FIXED_ID, tick=tick)
        )
        assert result.status is EnumSnapshotStatus.RECORDED, result.failure_detail
    return publisher


def _facts_after(
    recorder: ProtocolLandingTickRecorder, tick: int
) -> ModelLandingTickRecord | None:
    return next((r for r in recorder.recorded() if r.tick == tick), None)


def _expected_filled(tick: int) -> dict[str, tuple[str | None, tuple[int, ...]]]:
    return {pr: (w.pr_node_id, w.failed_run_ids) for pr, w in write_facts(tick).items()}


def test_golden_two_consecutive_ticks_each_recorded_with_node_and_run_ids() -> None:
    recorder = InMemoryLandingTickRecorder()
    publisher = _two_ticks(recorder)

    topic = overlay().record_topic
    assert [t for t, _ in publisher.published] == [topic, topic]
    events = [e for _, e in publisher.published]
    assert all(isinstance(e, ModelLandingTickRecord) for e in events)
    assert [r.tick for r in recorder.recorded()] == [FIRST, SECOND]
    for tick in (FIRST, SECOND):
        record = _facts_after(recorder, tick)
        assert record is not None
        filled = {p.pr: (p.pr_node_id, p.failed_run_ids) for p in record.facts.prs}
        assert filled == _expected_filled(tick)
        assert record.provenance == "inmemory-facts+inmemory-write-facts"


def test_first_ticks_facts_are_recoverable_after_the_next_tick() -> None:
    recorder = InMemoryLandingTickRecorder()
    _two_ticks(recorder)
    first = _facts_after(recorder, FIRST)
    assert first is not None
    expected, _, _ = fill_write_facts(scenario_facts(FIRST), write_facts(FIRST))
    assert first.facts == expected


def test_failing_control_a_last_only_recorder_loses_the_first_tick() -> None:
    recorder = LastOnlyTickRecorder()
    _two_ticks(recorder)
    assert _facts_after(recorder, SECOND) is not None
    assert _facts_after(recorder, FIRST) is None


def test_the_record_round_trips_through_its_bus_payload() -> None:
    recorder = InMemoryLandingTickRecorder()
    publisher = _two_ticks(recorder)
    for _, event in publisher.published:
        assert isinstance(event, BaseModel)
        again = ModelLandingTickRecord.model_validate_json(event.model_dump_json())
        assert again == event


def test_a_write_fact_read_at_another_head_fills_nothing() -> None:
    facts = scenario_facts(FIRST)
    pr = facts.prs[0]
    moved = {
        pr.pr: ModelPrWriteFacts(
            pr=pr.pr, head_sha="f" * 40, pr_node_id="PR_moved", failed_run_ids=(1,)
        )
    }
    filled, node_ids, run_ids = fill_write_facts(facts, moved)
    assert (node_ids, run_ids) == (0, 0)
    assert filled.prs[0].pr_node_id is None
    assert filled.prs[0].failed_run_ids == ()


def test_counts_of_filled_ids_are_reported() -> None:
    publisher = InMemoryLandingSnapshotPublisher()
    result = _handler(InMemoryLandingTickRecorder(), publisher).handle(
        ModelLandingSnapshotRequest(correlation_id=FIXED_ID, tick=FIRST)
    )
    assert (result.node_ids_filled, result.run_ids_filled) == (2, 1)


def _failed(
    publisher: InMemoryLandingSnapshotPublisher,
) -> list[ModelLandingSnapshotFailedEvent]:
    assert all(t == overlay().failed_topic for t, _ in publisher.published)
    return [
        e
        for _, e in publisher.published
        if isinstance(e, ModelLandingSnapshotFailedEvent)
    ]


def test_error_an_unread_tick_ends_in_the_failed_event() -> None:
    publisher = InMemoryLandingSnapshotPublisher()
    recorder = InMemoryLandingTickRecorder()
    result = _handler(recorder, publisher).handle(
        ModelLandingSnapshotRequest(correlation_id=FIXED_ID, tick=SECOND + 1)
    )
    assert result.status is EnumSnapshotStatus.FAILED
    assert result.failure_reason is EnumSnapshotFailureReason.FACTS_UNREAD
    assert [e.reason for e in _failed(publisher)] == [
        EnumSnapshotFailureReason.FACTS_UNREAD
    ]
    assert recorder.recorded() == ()


def test_error_a_tick_returned_under_another_number_is_refused() -> None:
    publisher = InMemoryLandingSnapshotPublisher()
    source = InMemoryLandingFactsSource({SECOND + 5: scenario_facts(FIRST)})
    result = _handler(InMemoryLandingTickRecorder(), publisher, source).handle(
        ModelLandingSnapshotRequest(correlation_id=FIXED_ID, tick=SECOND + 5)
    )
    assert result.failure_reason is EnumSnapshotFailureReason.TICK_MISMATCH
    assert [e.reason for e in _failed(publisher)] == [
        EnumSnapshotFailureReason.TICK_MISMATCH
    ]


def test_error_a_recorded_tick_is_never_replaced() -> None:
    recorder = InMemoryLandingTickRecorder()
    _two_ticks(recorder)
    before = recorder.recorded()
    publisher = InMemoryLandingSnapshotPublisher()
    result = _handler(recorder, publisher).handle(
        ModelLandingSnapshotRequest(correlation_id=FIXED_ID, tick=FIRST)
    )
    assert result.failure_reason is EnumSnapshotFailureReason.TICK_ALREADY_RECORDED
    assert [e.reason for e in _failed(publisher)] == [
        EnumSnapshotFailureReason.TICK_ALREADY_RECORDED
    ]
    assert recorder.recorded() == before


def test_overlay_redpanda_needs_servers_and_the_example_is_in_memory() -> None:
    with pytest.raises(ValidationError):
        ModelSnapshotOverlay(
            backend="redpanda",
            record_topic="t",
            failed_topic="f",
            floor_piece=9,
        )
    assert overlay().backend.value == "inmemory"
    assert overlay().floor_piece == 9


def test_contract_declares_the_overlay_topics() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["event_bus"]["publish_topics"] == [RECORDED_TOPIC, FAILED_TOPIC]
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["terminal_event"] == RECORDED_TOPIC
    assert overlay().record_topic == RECORDED_TOPIC
    assert overlay().failed_topic == FAILED_TOPIC
    assert contract["runtime_dispatch"]["terminal_events"] == {
        "success": RECORDED_TOPIC,
        "failure": FAILED_TOPIC,
    }
