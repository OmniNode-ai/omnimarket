# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The republish command replays the accepted-row topic into the 22 old row topics (OMN-25630, L12, AC2).

Golden chain: every row of the accepted-row topic, bootstrap rows and their live twins included, reaches the one
old topic of its row type and version, and a consumer of any old topic sees each of its rows once however often
the command runs. Error chain: a row with no old topic is refused. Failing control: a command that wrote a row
and its twin separately is caught by the once-only check.
"""

from __future__ import annotations

from collections import defaultdict

import pytest
from omnibase_core.models.events.work.model_work_ledger_render import ROW_TYPE_BY_KIND

from omnimarket.events.enum_ledger_row_type import EnumLedgerRowType
from omnimarket.events.model_ledger_row_event import (
    work_ledger_event_id,
    work_ledger_row_id,
)
from omnimarket.models.model_work_ledger_accepted_row import (
    EnumAcceptedRowSource,
    ModelAcceptedRowEvent,
)
from omnimarket.models.model_work_ledger_projection_inbound import (
    ModelWorkLedgerProjectionInbound,
)
from omnimarket.nodes.node_projection_work_ledger.contract_topics import (
    ROLLBACK_ROW_TOPICS,
)
from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_republish_old_topics import (
    HandlerWorkLedgerRepublishOldTopics,
    RepublishRefusedError,
    old_topic_of,
)
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_accepted_row_relay import (
    ModelRepublishRequest,
)
from tests.nodes.work_ledger_fixtures import records_for_all_ledger_row_types
from tests.unit.nodes.node_projection_work_ledger.accepted_row_support import (
    LEDGER_ID,
)

pytestmark = pytest.mark.unit

_STAMP = "2026-09-28T10:{n:02d}:00Z"
RAW_BY_TYPE = {
    EnumLedgerRowType.CLAIM: "| CLAIM | lane=alpha | ticket=OMN-1 | est ~1 lane-hours; displaces x; (OMN-1) | work",
    EnumLedgerRowType.STATUS: "| STATUS | lane=alpha | ticket=OMN-1 | progress",
    EnumLedgerRowType.TERMINAL: "| TERMINAL | lane=alpha | ticket=OMN-1 | friction=none | done",
    EnumLedgerRowType.HOLD: "| HOLD | lane=beta | id=2026-09-28T10:10:00Z-beta | surface=lab-dev | until=2026-09-28T12:00:00Z | reserved",
    EnumLedgerRowType.RELEASE: "| RELEASE | lane=beta | re=2026-09-28T10:10:00Z-beta | surface=lab-dev | result=PASS | restored=yes | done",
    EnumLedgerRowType.MSG: "| MSG | from=alpha | to=beta | id=2026-09-28T10:15:00Z-alpha | look",
    EnumLedgerRowType.ACK: "| ACK | from=beta | to=alpha | id=2026-09-28T10:16:00Z-beta | re=2026-09-28T10:15:00Z-alpha",
    EnumLedgerRowType.RULING: '| RULING | lane=orch | ticket=OMN-1 | "go" | text',
    EnumLedgerRowType.OPERATOR_CONSENT: '| OPERATOR-CONSENT | lane=orch | ticket=OMN-1 | "yes" | text',
    EnumLedgerRowType.FRICTION: "| FRICTION | lane=alpha | ticket=OMN-1 | cost=1h | slow",
    EnumLedgerRowType.CORRECTION: "| CORRECTION | lane=alpha | ticket=OMN-1 | fixes the row above",
}


class FakeOldTopics:
    """The 22 old topics: a log per topic, from which a consumer reads."""

    def __init__(self, preloaded: dict[str, set[str]] | None = None) -> None:
        self.logs: dict[str, list[tuple[str, dict[str, object]]]] = defaultdict(list)
        self._preloaded = preloaded or {}

    def row_ids_on(self, topic: str) -> frozenset[str]:
        return frozenset(r for r, _ in self.logs[topic]) | frozenset(
            self._preloaded.get(topic, set())
        )

    def publish(self, topic: str, row_id: str, payload: dict[str, object]) -> None:
        self.logs[topic].append((row_id, payload))

    def consumer_sees(self, topic: str) -> list[str]:
        return [row_id for row_id, _ in self.logs[topic]]


def _raw(row_type: EnumLedgerRowType, n: int) -> str:
    return _STAMP.format(n=n) + " " + RAW_BY_TYPE[row_type]


def _event(
    row_type: EnumLedgerRowType, n: int, *, seq: int | None, bootstrap: bool
) -> ModelAcceptedRowEvent:
    raw = _raw(row_type, n)
    return ModelAcceptedRowEvent(
        ledger_id=LEDGER_ID,
        row_id=work_ledger_row_id(raw),
        raw_row=raw,
        source=EnumAcceptedRowSource.BOOTSTRAP
        if bootstrap
        else EnumAcceptedRowSource.LIVE,
        ledger_seq=seq,
        legacy_ordinal=n if bootstrap and seq is None else None,
    )


def _typed_claim() -> ModelAcceptedRowEvent:
    raw = "2026-09-28T11:00:00Z | CLAIM | lane=acceptance-lane | ticket=OMN-20001 | est ~1 lane-hours; displaces none; (OMN-20001) | paired source"
    row_id = work_ledger_row_id(raw)
    event_id = work_ledger_event_id(LEDGER_ID, row_id)
    body = records_for_all_ledger_row_types()[1].event.model_copy(
        update={"event_id": event_id}
    )
    assert EnumLedgerRowType(ROW_TYPE_BY_KIND[body.kind]) is EnumLedgerRowType.CLAIM
    inbound = ModelWorkLedgerProjectionInbound(
        ledger_id=LEDGER_ID,
        row_id=row_id,
        event_id=event_id,
        source="acceptance-test",
        raw_row=raw,
        provenance_kind="markdown",
        event=body,
    )
    return ModelAcceptedRowEvent(
        ledger_id=LEDGER_ID,
        row_id=row_id,
        raw_row=raw,
        source=EnumAcceptedRowSource.LIVE,
        ledger_seq=99,
        typed=inbound,
    )


def _topic_events() -> tuple[ModelAcceptedRowEvent, ...]:
    """Bootstrap history of every row type, live rows after it, and the live twin of one bootstrap row."""
    types = list(EnumLedgerRowType)
    history = [
        _event(t, n, seq=None, bootstrap=True) for n, t in enumerate(types[:4], start=1)
    ]
    sequenced = [
        _event(t, n, seq=n - 4, bootstrap=True)
        for n, t in enumerate(types[4:8], start=5)
    ]
    live = [
        _event(t, n, seq=n - 4, bootstrap=False)
        for n, t in enumerate(types[8:], start=9)
    ]
    twin = _event(
        types[0], 1, seq=50, bootstrap=False
    )  # the live twin of bootstrap row 1
    return (*history, *sequenced, *live, twin, _typed_claim())


def test_republish_old_topics_every_row_reaches_the_one_topic_of_its_type_once() -> (
    None
):
    topics = FakeOldTopics()
    events = _topic_events()
    result = HandlerWorkLedgerRepublishOldTopics(topics).handle(
        ModelRepublishRequest(events=events)
    )
    distinct = {e.row_id for e in events}
    assert len(distinct) == len(events) - 1  # the twin shares its bootstrap row's id
    assert result.duplicates_dropped == 1
    assert {r.row_id for r in result.published} == distinct
    assert len(result.published) == len(distinct)
    # a consumer of any old topic sees each of its rows once, and every topic it sees is one of the 22
    assert set(topics.logs) <= set(ROLLBACK_ROW_TOPICS)
    for topic in ROLLBACK_ROW_TOPICS:
        seen = topics.consumer_sees(topic)
        assert len(seen) == len(set(seen)), f"{topic} repeats a row"
    assert sorted(
        r for t in ROLLBACK_ROW_TOPICS for r in topics.consumer_sees(t)
    ) == sorted(distinct)
    # eleven v1 topics hold the raw rows, the typed claim is on claim.v2
    assert topics.consumer_sees(EnumLedgerRowType.CLAIM.typed_topic) == [
        _typed_claim().row_id
    ]
    for row_type in EnumLedgerRowType:
        assert topics.consumer_sees(row_type.topic), f"{row_type.topic} got no row"


def test_republish_old_topics_the_twin_publishes_the_sequence_old_consumers_need() -> (
    None
):
    topics = FakeOldTopics()
    HandlerWorkLedgerRepublishOldTopics(topics).handle(
        ModelRepublishRequest(events=_topic_events())
    )
    row_id = _event(EnumLedgerRowType.CLAIM, 1, seq=None, bootstrap=True).row_id
    payloads = [p for r, p in topics.logs[EnumLedgerRowType.CLAIM.topic] if r == row_id]
    assert len(payloads) == 1
    assert payloads[0]["ledger_seq"] == 50
    assert payloads[0]["raw_row"] == _raw(EnumLedgerRowType.CLAIM, 1)


def test_republish_old_topics_running_twice_adds_no_second_copy() -> None:
    topics = FakeOldTopics()
    handler = HandlerWorkLedgerRepublishOldTopics(topics)
    request = ModelRepublishRequest(events=_topic_events())
    first = handler.handle(request)
    second = handler.handle(request)
    assert len(first.published) > 0
    assert second.published == ()
    assert second.already_on_old_topics == len(first.published)
    for topic in ROLLBACK_ROW_TOPICS:
        seen = topics.consumer_sees(topic)
        assert len(seen) == len(set(seen))


def test_republish_old_topics_fills_only_the_gap_an_old_topic_has() -> None:
    events = _topic_events()
    held = {
        EnumLedgerRowType.STATUS.topic: {
            e.row_id for e in events if "| STATUS |" in e.raw_row
        }
    }
    topics = FakeOldTopics(preloaded=held)
    result = HandlerWorkLedgerRepublishOldTopics(topics).handle(
        ModelRepublishRequest(events=events)
    )
    assert result.already_on_old_topics == 1
    assert topics.consumer_sees(EnumLedgerRowType.STATUS.topic) == []


def test_republish_old_topics_failing_control_a_twin_written_separately_is_two_copies() -> (
    None
):
    topics = FakeOldTopics()
    for event in _topic_events():
        topics.publish(old_topic_of(event), event.row_id, {})
    claim_topic = topics.consumer_sees(EnumLedgerRowType.CLAIM.topic)
    assert len(claim_topic) != len(set(claim_topic))


def test_republish_old_topics_refuses_a_row_it_cannot_type() -> None:
    bad_raw = "2026-09-28T10:00:00Z | NOT-A-TYPE | lane=x | text"
    bad = ModelAcceptedRowEvent(
        ledger_id=LEDGER_ID,
        row_id=work_ledger_row_id(bad_raw),
        raw_row=bad_raw,
        source=EnumAcceptedRowSource.LIVE,
        ledger_seq=1,
    )
    topics = FakeOldTopics()
    with pytest.raises(RepublishRefusedError):
        HandlerWorkLedgerRepublishOldTopics(topics).handle(
            ModelRepublishRequest(events=(bad,))
        )
    assert dict(topics.logs) == {}
