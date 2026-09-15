# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17215 AC6: a skipped window must not shadow an observed one.

A heartbeat that jumps ``window_sequence`` mints one UNKNOWN gap row per known
(consumer_group, topic) pair, including the pairs whose observed window that
SAME heartbeat carries. Both rows are then published as snapshot deltas stamped
with that message's own Kafka coordinates, and the snapshot is keyed on
``(consumer_group, topic)`` alone -- so the two collide on one cache key at one
source offset, and ``SnapshotCache.apply_message`` drops the second of them as a
stale replay. The exposure ends up serving UNKNOWN with null counters for a pair
whose window the invocation observed, while ``consumer_flow_windows`` holds the
observed row (the gap row is minted at the arriving window's own
``window_start``, so the flow upsert overwrites it on the same primary key).

The fix is at the publisher, not at the cache: no gap delta is published for a
pair whose observed row the same invocation writes. A pair that genuinely has
only a gap still gets its UNKNOWN delta -- an unobserved window must stay
visible, which is the whole point of the gap row.

Everything below is the shipped path: the real ``ConsumerFlowProjectionWriter``
against a table double that enforces the real statements' conflict semantics,
the real ``publish_snapshot_delta``/``encode_snapshot_delta`` captured at the
producer boundary, and the real ``SnapshotCache.apply_message`` fed every
captured delta in publish order.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.nodes.node_projection_consumer_flow.handlers.handler_consumer_flow_runner import (
    _INSERT_UNKNOWN,
    _SELECT_KEYS_AT_SEQUENCE,
    _SELECT_PRIOR_STATE,
    _SELECT_UPSTREAM,
    _UPSERT_FLOW,
    ConsumerFlowProjectionWriter,
)
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.snapshot_cache import SnapshotCache

pytestmark = pytest.mark.unit

_SNAPSHOT_TOPIC = "onex.snapshot.projection.consumer-flow.v1"
_SOURCE_TOPIC = "onex.evt.platform.node-heartbeat.v1"  # onex-topic-allow: the heartbeat topic this node subscribes to
_NODE_ID = uuid4()
_T0 = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
_WINDOW = timedelta(seconds=60)
_OBSERVED_PAIR = ("group-observed", "topic-a")
_GAP_ONLY_PAIR = ("group-quiet", "topic-b")
_LAST_SEQUENCE = 1


class _FlowWindowsTable:
    """``omninode_internal.consumer_flow_windows``, reduced to its conflict rules.

    Keyed on the real primary key ``(consumer_group, topic, window_start)``, it
    reproduces the two behaviours the published deltas depend on: the gap
    insert is ``ON CONFLICT DO NOTHING`` and the flow upsert overwrites unless
    the stored ``ingest_sequence`` is newer for the same node. ``RETURNING``
    yields the stored row, with the ``projection_cursor`` BIGSERIAL a real
    insert would stamp.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, datetime], dict[str, Any]] = {}
        self._cursor = 0
        self.statements: list[str] = []

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def execute(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.statements.append(query)
        if query == _SELECT_UPSTREAM:
            # Nothing in this runtime produces to the consumed topic, so the
            # derivation sees no upstream evidence at all.
            return [{"window_count": 0, "produced": 0}]
        if query == _SELECT_PRIOR_STATE:
            return [{"last_sequence": _LAST_SEQUENCE}]
        if query == _SELECT_KEYS_AT_SEQUENCE:
            return [
                {"consumer_group": group, "topic": topic}
                for group, topic in (_OBSERVED_PAIR, _GAP_ONLY_PAIR)
            ]
        if query == _INSERT_UNKNOWN:
            return self._insert_unknown(*params)
        if query == _UPSERT_FLOW:
            return self._upsert_flow(*params)
        raise AssertionError(f"unexpected statement: {query}")

    def _insert_unknown(self, *params: Any) -> list[dict[str, Any]]:
        group, topic, window_start, window_end, node_id, sequence = params[:6]
        evidence, state, evaluated_at = params[6:]
        pk = (group, topic, window_start)
        if pk in self.rows:
            return []  # ON CONFLICT DO NOTHING
        self._cursor += 1
        row = {
            "consumer_group": group,
            "topic": topic,
            "window_start": window_start,
            "window_end": window_end,
            "node_id": node_id,
            "ingest_sequence": sequence,
            "messages_in": None,
            "messages_out": None,
            "messages_dlq": None,
            "handler_errors": None,
            "upstream_produced": None,
            "upstream_evidence": evidence,
            "flow_state": state,
            "evaluated_at": evaluated_at,
            "projection_cursor": self._cursor,
        }
        self.rows[pk] = row
        return [dict(row)]

    def _upsert_flow(self, *params: Any) -> list[dict[str, Any]]:
        (
            group,
            topic,
            window_start,
            window_end,
            node_id,
            sequence,
            messages_in,
            messages_out,
            messages_dlq,
            handler_errors,
            upstream_produced,
            evidence,
            state,
            evaluated_at,
        ) = params
        pk = (group, topic, window_start)
        stored = self.rows.get(pk)
        if stored is not None and not (
            stored["node_id"] != node_id or stored["ingest_sequence"] <= sequence
        ):
            return []  # the ON CONFLICT ... WHERE predicate refused the update
        cursor = stored["projection_cursor"] if stored is not None else None
        if cursor is None:
            self._cursor += 1
            cursor = self._cursor
        row = {
            "consumer_group": group,
            "topic": topic,
            "window_start": window_start,
            "window_end": window_end,
            "node_id": node_id,
            "ingest_sequence": sequence,
            "messages_in": messages_in,
            "messages_out": messages_out,
            "messages_dlq": messages_dlq,
            "handler_errors": handler_errors,
            "upstream_produced": upstream_produced,
            "upstream_evidence": evidence,
            "flow_state": state,
            "evaluated_at": evaluated_at,
            "projection_cursor": cursor,
        }
        self.rows[pk] = row
        return [dict(row)]


class _RecordingProducer:
    """The producer boundary: records exactly what a broker would receive.

    ``publish_snapshot_delta`` runs for real above this, so every captured
    message carries the key bytes, headers and encoded delta the live topic
    would hold.
    """

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_and_wait(
        self,
        topic: str,
        value: bytes | None = None,
        key: bytes | None = None,
        headers: list[tuple[str, bytes]] | None = None,
    ) -> None:
        self.sent.append(
            {"topic": topic, "value": value, "key": key, "headers": headers or []}
        )

    async def stop(self) -> None:
        return None


def _heartbeat_with_gap() -> dict[str, Any]:
    """Window 3 arrives after window 1: window 2 was never delivered."""
    start = _T0 + _WINDOW * 3
    return {
        "node_id": str(_NODE_ID),
        "window_start": start.isoformat(),
        "window_end": (start + _WINDOW).isoformat(),
        "window_sequence": _LAST_SEQUENCE + 2,
        "consumer_deltas": [
            {
                "consumer_group": _OBSERVED_PAIR[0],
                "topic": _OBSERVED_PAIR[1],
                "node_id": str(_NODE_ID),
                "window_start": start.isoformat(),
                "window_end": (start + _WINDOW).isoformat(),
                "window_sequence": _LAST_SEQUENCE + 2,
                "messages_in": 512,
                "messages_out": 509,
                "messages_dlq": 1,
                "handler_errors": 2,
            }
        ],
        "produce_deltas": [],
    }


class _Projected:
    """One invocation of the writer, and the cache the published deltas built."""

    def __init__(self) -> None:
        self.table = _FlowWindowsTable()
        self.producer = _RecordingProducer()
        self.writer = ConsumerFlowProjectionWriter()
        assert self.writer._snapshot_exposure is not None, (
            "the writer must resolve its bus_backed exposure from contract.yaml"
        )
        self.exposure = self.writer._snapshot_exposure
        assert self.exposure.topic == _SNAPSHOT_TOPIC
        self.writer._db = self.table  # type: ignore[assignment]
        self.writer._producer = self.producer  # type: ignore[assignment]

        meta = MessageMeta(
            partition=0, offset=4711, fallback_id="omn17215-ac6", topic=_SOURCE_TOPIC
        )
        payload: dict[str, Any] = {
            "flow_window": _heartbeat_with_gap(),
            "correlation_id": str(uuid4()),
        }
        assert (
            asyncio.run(self.writer.project_event(_SOURCE_TOPIC, payload, meta)) is True
        )

        self.cache = SnapshotCache(
            {_SNAPSHOT_TOPIC: self.exposure},
            bootstrap_servers="unused:9092",
            group_id="test-omn17215-ac6-gap-row",
        )
        # Every published delta, in publish order, through the real apply path.
        for message in self.producer.sent:
            self.cache.apply_message(
                message["topic"], message["key"], message["value"], message["headers"]
            )
        self.cache._state[_SNAPSHOT_TOPIC].bootstrap_complete = True

    def served(self, pair: tuple[str, str]) -> dict[str, Any]:
        rows = [
            row
            for row in self.cache.get_rows(_SNAPSHOT_TOPIC, unbounded=True)
            if (row["consumer_group"], row["topic"]) == pair
        ]
        assert len(rows) == 1, rows
        return rows[0]

    def published_keys(self) -> list[bytes | None]:
        return [message["key"] for message in self.producer.sent]


@pytest.fixture(scope="module")
def projected() -> _Projected:
    return _Projected()


def test_observed_pair_is_served_as_its_observed_window_not_unknown(
    projected: _Projected,
) -> None:
    row = projected.served(_OBSERVED_PAIR)
    assert row["flow_state"] == "FLOWING"
    assert row["messages_in"] == 512
    assert row["messages_out"] == 509
    assert row["messages_dlq"] == 1
    assert row["handler_errors"] == 2
    assert row["ingest_sequence"] == _LAST_SEQUENCE + 2
    assert row["window_end"] == (_T0 + _WINDOW * 4).isoformat()


def test_a_pair_with_only_a_gap_still_gets_its_unknown_delta(
    projected: _Projected,
) -> None:
    """Suppression is per pair: an unobserved window stays visible."""
    row = projected.served(_GAP_ONLY_PAIR)
    assert row["flow_state"] == "UNKNOWN"
    assert row["messages_in"] is None
    assert row["messages_out"] is None
    assert row["ingest_sequence"] == _LAST_SEQUENCE + 1


def test_no_gap_delta_is_published_for_the_observed_pair(
    projected: _Projected,
) -> None:
    """The shadowing delta is never published, rather than published and lost.

    Both rows carry this message's own coordinates, so a published gap delta
    would be dropped by the cache's staleness rule whichever of the two arrived
    second -- the defect is that it is published at all.
    """
    observed_key = "|".join(_OBSERVED_PAIR).encode("utf-8")
    gap_key = "|".join(_GAP_ONLY_PAIR).encode("utf-8")
    keys = projected.published_keys()
    assert keys.count(observed_key) == 1, keys
    assert keys.count(gap_key) == 1, keys
    assert len(keys) == 2


def test_the_table_still_holds_the_gap_row_for_the_unobserved_pair(
    projected: _Projected,
) -> None:
    """Nothing about the write path changed: the gap row is still inserted, and
    the observed pair's gap row is still overwritten by its observed window."""
    gap_start = _T0 + _WINDOW * 3
    stored = projected.table.rows[(*_GAP_ONLY_PAIR, gap_start)]
    assert stored["flow_state"] == "UNKNOWN"
    observed_stored = projected.table.rows[(*_OBSERVED_PAIR, gap_start)]
    assert observed_stored["flow_state"] == "FLOWING"
    assert observed_stored["messages_in"] == 512
