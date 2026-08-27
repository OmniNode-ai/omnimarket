# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16777 — materialization: what actually lands in the read model.

These drive ``HandlerProjectionConsumerFlow`` through the same
``handle(input_data)`` entrypoint the runtime's projection arm invokes, against
the in-memory adapter whose UPSERT semantics are pinned to the real Postgres and
SQLite adapters (OMN-15598).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_projection_consumer_flow.handlers.handler_projection_consumer_flow import (
    TABLE_FLOW,
    HandlerProjectionConsumerFlow,
)
from omnimarket.nodes.node_projection_consumer_flow.models import (
    EnumConsumerFlowState,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

_T0 = datetime(2026, 8, 27, 12, 0, 0, tzinfo=UTC)
_IN_TOPIC = "onex.evt.platform.node-heartbeat.v1"  # onex-topic-allow: real topic from the OMN-16755 incident
_OUT_TOPIC = "onex.cmd.omnibase-infra.gateway-link-health-upsert.v1"  # onex-topic-allow: real topic from the OMN-16755 incident
_STALLED_GROUP = "onex-dev.omnimarket.gateway-link-health-projection-compute.consume"


def _delta(
    *,
    group: str,
    topic: str = _IN_TOPIC,
    node_id: str,
    sequence: int,
    start: datetime,
    end: datetime,
    messages_in: int = 0,
    messages_out: int = 0,
    messages_dlq: int = 0,
    handler_errors: int = 0,
) -> dict[str, Any]:
    return {
        "consumer_group": group,
        "topic": topic,
        "node_id": node_id,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "window_sequence": sequence,
        "messages_in": messages_in,
        "messages_out": messages_out,
        "messages_dlq": messages_dlq,
        "handler_errors": handler_errors,
    }


def _heartbeat(
    *,
    node_id: str,
    sequence: int,
    start: datetime,
    end: datetime,
    consumer_deltas: list[dict[str, Any]],
    produce_deltas: list[dict[str, Any]] | None = None,
) -> dict[str, object]:
    """A heartbeat payload exactly as the projection arm receives it."""
    return {
        "_event_type": "heartbeat",
        "_topic": _IN_TOPIC,
        "node_id": node_id,
        "flow_window": {
            "node_id": node_id,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "window_sequence": sequence,
            "consumer_deltas": consumer_deltas,
            "produce_deltas": produce_deltas or [],
        },
    }


def _rows(db: InmemoryDatabaseAdapter) -> list[dict[str, object]]:
    return db.query(TABLE_FLOW)


@pytest.mark.unit
def test_stalled_row_is_materialized_with_its_counters() -> None:
    """AC2 end to end: 15,750 in / 0 out lands as a STALLED row."""
    db = InmemoryDatabaseAdapter()
    node_id = str(uuid4())
    payload = _heartbeat(
        node_id=node_id,
        sequence=1,
        start=_T0,
        end=_T0 + timedelta(seconds=60),
        consumer_deltas=[
            _delta(
                group=_STALLED_GROUP,
                node_id=node_id,
                sequence=1,
                start=_T0,
                end=_T0 + timedelta(seconds=60),
                messages_in=15750,
                messages_out=0,
            )
        ],
    )
    payload["_db"] = db

    result = HandlerProjectionConsumerFlow().handle(payload)

    assert result["rows_upserted"] == 1
    (row,) = _rows(db)
    assert row["flow_state"] == EnumConsumerFlowState.STALLED.value
    assert row["messages_in"] == 15750
    assert row["messages_out"] == 0


@pytest.mark.unit
def test_two_legs_are_separate_rows() -> None:
    """AC3's actual content: the OMN-16754 defect was ONE verdict spanning two
    legs, so a live outbound leg vouched for a dead inbound one. Two
    subscriptions must land as two rows with independent verdicts."""
    db = InmemoryDatabaseAdapter()
    node_id = str(uuid4())
    end = _T0 + timedelta(seconds=60)
    payload = _heartbeat(
        node_id=node_id,
        sequence=1,
        start=_T0,
        end=end,
        consumer_deltas=[
            _delta(
                group="onex-dev.gateway-forwarder.inbound.consume",
                node_id=node_id,
                sequence=1,
                start=_T0,
                end=end,
                messages_in=0,
                messages_out=0,
            ),
            _delta(
                group="onex-dev.gateway-forwarder.outbound.consume",
                node_id=node_id,
                sequence=1,
                start=_T0,
                end=end,
                messages_in=5575,
                messages_out=5575,
            ),
        ],
    )
    payload["_db"] = db

    HandlerProjectionConsumerFlow().handle(payload)

    by_group = {row["consumer_group"]: row for row in _rows(db)}
    assert len(by_group) == 2, "the two legs collapsed into one row"
    assert (
        by_group["onex-dev.gateway-forwarder.outbound.consume"]["flow_state"]
        == EnumConsumerFlowState.FLOWING.value
    )
    inbound = by_group["onex-dev.gateway-forwarder.inbound.consume"]
    # The inbound leg is fed from OUTSIDE this runtime, so nothing here produces
    # to its topic and there is no honest STARVED evidence. It reports IDLE and
    # records WHY — never a fabricated STARVED, which would fire on every quiet
    # externally-fed topic in the platform.
    assert inbound["flow_state"] == EnumConsumerFlowState.IDLE.value
    assert inbound["upstream_evidence"] == "NONE"
    outbound_state = by_group["onex-dev.gateway-forwarder.outbound.consume"][
        "flow_state"
    ]
    assert inbound["flow_state"] != outbound_state, (
        "one leg's health was allowed to speak for the other"
    )


@pytest.mark.unit
def test_upstream_production_in_the_same_window_yields_starved() -> None:
    """A consumer that took nothing on a topic this runtime WAS publishing to."""
    db = InmemoryDatabaseAdapter()
    node_id = str(uuid4())
    end = _T0 + timedelta(seconds=60)
    payload = _heartbeat(
        node_id=node_id,
        sequence=1,
        start=_T0,
        end=end,
        consumer_deltas=[
            _delta(
                group="onex-dev.omnimarket.link-health-writer.consume",
                topic=_OUT_TOPIC,
                node_id=node_id,
                sequence=1,
                start=_T0,
                end=end,
            )
        ],
        produce_deltas=[
            {
                "topic": _OUT_TOPIC,
                "node_id": node_id,
                "window_start": _T0.isoformat(),
                "window_end": end.isoformat(),
                "window_sequence": 1,
                "messages_produced": 12,
            }
        ],
    )
    payload["_db"] = db

    HandlerProjectionConsumerFlow().handle(payload)

    (row,) = _rows(db)
    assert row["flow_state"] == EnumConsumerFlowState.STARVED.value
    assert row["upstream_produced"] == 12


@pytest.mark.unit
def test_missed_window_materializes_unknown_with_null_counters() -> None:
    """AC5: ``UNKNOWN != 0 messages``.

    Window 1 arrives, window 2 is lost in transit, window 3 arrives. The gap
    must materialize as an UNKNOWN row whose counters are NULL. If it landed as
    a zero row it would read as "this consumer was alive and idle" — a runtime
    that has stopped heartbeating entirely would then report permanently
    healthy, which is the defect this whole epic exists to close.
    """
    db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionConsumerFlow()
    node_id = str(uuid4())
    w1_end = _T0 + timedelta(seconds=60)
    w3_start = _T0 + timedelta(seconds=120)
    w3_end = _T0 + timedelta(seconds=180)

    first = _heartbeat(
        node_id=node_id,
        sequence=1,
        start=_T0,
        end=w1_end,
        consumer_deltas=[
            _delta(
                group=_STALLED_GROUP,
                node_id=node_id,
                sequence=1,
                start=_T0,
                end=w1_end,
                messages_in=10,
                messages_out=10,
            )
        ],
    )
    first["_db"] = db
    handler.handle(first)

    # window_sequence 2 never arrives.
    third = _heartbeat(
        node_id=node_id,
        sequence=3,
        start=w3_start,
        end=w3_end,
        consumer_deltas=[
            _delta(
                group=_STALLED_GROUP,
                node_id=node_id,
                sequence=3,
                start=w3_start,
                end=w3_end,
                messages_in=10,
                messages_out=10,
            )
        ],
    )
    third["_db"] = db
    handler.handle(third)

    unknown = [
        row
        for row in _rows(db)
        if row["flow_state"] == EnumConsumerFlowState.UNKNOWN.value
    ]
    assert len(unknown) == 1, (
        "the lost window produced no UNKNOWN row — a dropped heartbeat is "
        "indistinguishable from a quiet one"
    )
    gap = unknown[0]
    assert gap["messages_in"] is None, (
        "the missed window materialized as 0 messages; UNKNOWN must never be "
        "readable as observed-idle (AC5)"
    )
    assert gap["messages_out"] is None
    assert gap["messages_dlq"] is None
    assert gap["handler_errors"] is None
    assert gap["window_start"] == w1_end.isoformat()
    assert gap["window_end"] == w3_start.isoformat()
    assert gap["messages_in"] != 0


@pytest.mark.unit
def test_a_late_window_is_never_downgraded_to_unknown() -> None:
    """A gap row only ever writes into an empty slot: a real observation that
    arrives late outranks the placeholder that stood in for it."""
    db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionConsumerFlow()
    node_id = str(uuid4())
    w1_end = _T0 + timedelta(seconds=60)
    w2_end = _T0 + timedelta(seconds=120)

    for sequence, start, end in ((1, _T0, w1_end), (2, w1_end, w2_end)):
        payload = _heartbeat(
            node_id=node_id,
            sequence=sequence,
            start=start,
            end=end,
            consumer_deltas=[
                _delta(
                    group=_STALLED_GROUP,
                    node_id=node_id,
                    sequence=sequence,
                    start=start,
                    end=end,
                    messages_in=7,
                    messages_out=7,
                )
            ],
        )
        payload["_db"] = db
        handler.handle(payload)

    # A much later window arrives; the slot for window 2 is already filled.
    late_start = _T0 + timedelta(seconds=300)
    payload = _heartbeat(
        node_id=node_id,
        sequence=6,
        start=late_start,
        end=late_start + timedelta(seconds=60),
        consumer_deltas=[
            _delta(
                group=_STALLED_GROUP,
                node_id=node_id,
                sequence=6,
                start=late_start,
                end=late_start + timedelta(seconds=60),
                messages_in=1,
                messages_out=1,
            )
        ],
    )
    payload["_db"] = db
    handler.handle(payload)

    observed = {row["window_start"]: row for row in _rows(db)}
    assert (
        observed[w1_end.isoformat()]["flow_state"]
        == EnumConsumerFlowState.FLOWING.value
    ), "an observed window was overwritten by an UNKNOWN placeholder"


@pytest.mark.unit
def test_a_replayed_older_window_does_not_overwrite_a_newer_one() -> None:
    """At-least-once delivery means redelivery is normal, not exceptional.

    Ordering is by producer-assigned window_start with ingest_sequence as the
    tie-breaker — never by an ingest clock, which would let a replay of an older
    window silently overwrite state that has already moved on.
    """
    db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionConsumerFlow()
    node_id = str(uuid4())
    end = _T0 + timedelta(seconds=60)

    newer = _heartbeat(
        node_id=node_id,
        sequence=9,
        start=_T0,
        end=end,
        consumer_deltas=[
            _delta(
                group=_STALLED_GROUP,
                node_id=node_id,
                sequence=9,
                start=_T0,
                end=end,
                messages_in=100,
                messages_out=100,
            )
        ],
    )
    newer["_db"] = db
    handler.handle(newer)

    older = _heartbeat(
        node_id=node_id,
        sequence=8,
        start=_T0,
        end=end,
        consumer_deltas=[
            _delta(
                group=_STALLED_GROUP,
                node_id=node_id,
                sequence=8,
                start=_T0,
                end=end,
                messages_in=1,
                messages_out=0,
            )
        ],
    )
    older["_db"] = db
    handler.handle(older)

    (row,) = [r for r in _rows(db) if r["window_start"] == _T0.isoformat()]
    assert row["messages_in"] == 100, (
        "a replayed older window overwrote a newer one — the projection now "
        "reports state that has already been superseded"
    )
    assert row["flow_state"] == EnumConsumerFlowState.FLOWING.value


@pytest.mark.unit
def test_replaying_the_same_window_reproduces_identical_rows() -> None:
    """AC6: replay determinism. No wall clock anywhere in the row."""
    node_id = str(uuid4())
    end = _T0 + timedelta(seconds=60)

    def _run() -> list[dict[str, object]]:
        db = InmemoryDatabaseAdapter()
        payload = _heartbeat(
            node_id=node_id,
            sequence=1,
            start=_T0,
            end=end,
            consumer_deltas=[
                _delta(
                    group=_STALLED_GROUP,
                    node_id=node_id,
                    sequence=1,
                    start=_T0,
                    end=end,
                    messages_in=15750,
                    messages_out=0,
                    messages_dlq=3,
                    handler_errors=3,
                )
            ],
        )
        payload["_db"] = db
        HandlerProjectionConsumerFlow().handle(payload)
        return _rows(db)

    assert _run() == _run()


@pytest.mark.unit
def test_a_heartbeat_without_a_window_writes_nothing() -> None:
    """The priming tick and the non-carrier node both send a heartbeat with no
    window. Absence is not zero traffic, so it must not create a row."""
    db = InmemoryDatabaseAdapter()
    payload: dict[str, object] = {
        "_db": db,
        "_event_type": "heartbeat",
        "_topic": _IN_TOPIC,
        "node_id": str(uuid4()),
    }

    result = HandlerProjectionConsumerFlow().handle(payload)

    assert result["rows_upserted"] == 0
    assert _rows(db) == []
