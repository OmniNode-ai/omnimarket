# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bus-fed, in-memory serving cache for bus_backed projection exposures.

OMN-15800 Seam B (2026-08-09 operator ruling: "nothing should be connecting
to a database other than the runtime"). ``SnapshotCache`` is the ONLY state
backing ``GET /projection/{topic}`` for any exposure whose contract declares
``projection_api.bus_backed: true`` — the projection-api process holds zero
DB driver and zero DSN. One ``AIOKafkaConsumer`` subscribes to every
bus_backed exposure's compacted ``onex.snapshot.projection.*`` topic, replays
each to end-of-partition (bootstrap), and then keeps consuming live. HTTP
reads never touch Kafka directly — they read this in-memory dict.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aiokafka import AIOKafkaConsumer, TopicPartition

from omnimarket.projection.models import (
    ModelProjectionSnapshotDelta,
    ProjectionTableConfig,
)

logger = logging.getLogger(__name__)

DEFAULT_GROUP_ID = "omnimarket-projection-api-snapshot-cache-v1"
DEFAULT_CLIENT_ID = "omnimarket-projection-api-snapshot-cache"


@dataclass(frozen=True)
class CachedRow:
    """One cached row: the serialized column values plus cache metadata."""

    row: dict[str, Any]
    observed_at: datetime
    ingest_sequence: int
    tenant_id: str


@dataclass
class _TopicCacheState:
    rows: dict[tuple[str, ...], CachedRow] = field(default_factory=dict)
    bootstrap_complete: bool = False
    latest_event_at: datetime | None = None
    assigned_partitions: set[int] = field(default_factory=set)
    eof_seen: set[int] = field(default_factory=set)


class _SortWrapper:
    """Comparable wrapper enabling per-column ASC/DESC in a single sort key.

    ``None`` sorts last regardless of direction (both directions want a
    missing value at the bottom of the page, not interleaved by accident).
    """

    __slots__ = ("reverse", "value")

    def __init__(self, value: Any, reverse: bool) -> None:
        self.value = value
        self.reverse = reverse

    def __lt__(self, other: _SortWrapper) -> bool:
        if self.value is None:
            return False
        if other.value is None:
            return True
        if self.reverse:
            return bool(other.value < self.value)
        return bool(self.value < other.value)


def _sort_rows(
    items: list[tuple[tuple[str, ...], CachedRow]],
    order_by_spec: tuple[tuple[str, str], ...],
) -> list[tuple[tuple[str, ...], CachedRow]]:
    if not order_by_spec:
        return items

    def _sort_key(
        item: tuple[tuple[str, ...], CachedRow],
    ) -> tuple[_SortWrapper, ...]:
        _key, cached = item
        return tuple(
            _SortWrapper(cached.row.get(column), reverse=(direction == "DESC"))
            for column, direction in order_by_spec
        )

    return sorted(items, key=_sort_key)


def _parse_observed_at(value: str) -> datetime:
    try:
        ts = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(ts)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    except ValueError:
        return datetime.now(UTC)


class SnapshotCache:
    """In-memory topic -> key -> row cache fed by compacted snapshot topics."""

    def __init__(
        self,
        exposures: dict[str, ProjectionTableConfig],
        *,
        bootstrap_servers: str,
        group_id: str = DEFAULT_GROUP_ID,
        client_id: str = DEFAULT_CLIENT_ID,
    ) -> None:
        self._exposures: dict[str, ProjectionTableConfig] = {
            topic: cfg for topic, cfg in exposures.items() if cfg.bus_backed
        }
        self._bootstrap_servers = bootstrap_servers
        self._group_id = group_id
        self._client_id = client_id
        self._state: dict[str, _TopicCacheState] = {
            topic: _TopicCacheState() for topic in self._exposures
        }
        self._consumer: AIOKafkaConsumer | None = None
        self._consume_task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def bus_backed_topics(self) -> frozenset[str]:
        return frozenset(self._exposures)

    def tracks_topic(self, topic: str) -> bool:
        return topic in self._exposures

    def is_bootstrapped(self, topic: str) -> bool:
        state = self._state.get(topic)
        return state is not None and state.bootstrap_complete

    def latest_event_at(self, topic: str) -> datetime | None:
        state = self._state.get(topic)
        return state.latest_event_at if state is not None else None

    def row_count(self, topic: str) -> int:
        state = self._state.get(topic)
        return len(state.rows) if state is not None else 0

    def apply_message(
        self,
        topic: str,
        key: bytes | None,
        value: bytes | None,
        headers: list[tuple[str, bytes]],
    ) -> None:
        """Apply one raw Kafka message (upsert or tombstone) to the cache.

        Public and synchronous so both the live consumer loop and the
        cross-boundary regression test drive the exact same apply path — the
        test does not hand-roll a stand-in for cache application.
        """
        state = self._state.get(topic)
        if state is None:
            return  # not a bus_backed topic this cache tracks

        if value is None:
            # Genuine Kafka tombstone: unconditional delete, no
            # ingest_sequence to compare (see publish_snapshot_delta).
            if key is None:
                return
            key_tuple = tuple(key.decode("utf-8").split("|"))
            state.rows.pop(key_tuple, None)
            return

        try:
            delta = ModelProjectionSnapshotDelta.model_validate_json(value)
        except Exception as exc:
            logger.error(
                "SnapshotCache: malformed snapshot delta on %s: %s", topic, exc
            )
            return

        tenant_id = "omninode"
        for header_key, header_value in headers:
            if header_key == "tenant_id":
                tenant_id = header_value.decode("utf-8")
                break

        existing = state.rows.get(delta.key)
        if existing is not None and delta.ingest_sequence <= existing.ingest_sequence:
            return  # stale/replayed delta relative to cache state -- idempotent

        observed_at = _parse_observed_at(delta.observed_at)
        state.rows[delta.key] = CachedRow(
            row=dict(delta.row or {}),
            observed_at=observed_at,
            ingest_sequence=delta.ingest_sequence,
            tenant_id=tenant_id,
        )
        if state.latest_event_at is None or observed_at > state.latest_event_at:
            state.latest_event_at = observed_at

        exposure = self._exposures[topic]
        max_rows = exposure.limit * 4
        if len(state.rows) > max_rows:
            ordered = _sort_rows(list(state.rows.items()), exposure.order_by_spec)
            state.rows = dict(ordered[:max_rows])

    def get_rows(self, topic: str, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Return cached rows for ``topic``, ordered per the exposure's order_by_spec."""
        state = self._state.get(topic)
        if state is None:
            return []
        exposure = self._exposures[topic]
        ordered = _sort_rows(list(state.rows.items()), exposure.order_by_spec)
        rows = [cached.row for _key, cached in ordered]
        effective_limit = limit if limit is not None else exposure.limit
        return rows[:effective_limit]

    async def start(self) -> None:
        """Subscribe, replay every bus_backed topic to end-of-partition, then
        keep consuming live. A no-op when no exposure declares bus_backed."""
        if not self._exposures:
            logger.info(
                "SnapshotCache: no bus_backed exposures declared; not starting a consumer"
            )
            return
        self._consumer = AIOKafkaConsumer(
            *self._exposures.keys(),
            bootstrap_servers=self._bootstrap_servers,
            group_id=self._group_id,
            client_id=self._client_id,
            auto_offset_reset="earliest",
            enable_auto_commit=False,
            value_deserializer=None,
        )
        await self._consumer.start()
        self._running = True
        self._consume_task = asyncio.ensure_future(self._consume_loop())

    async def _consume_loop(self) -> None:
        assert self._consumer is not None
        await self._mark_bootstrap_complete_when_caught_up()
        async for msg in self._consumer:
            if not self._running:
                break
            headers = list(msg.headers or [])
            self.apply_message(msg.topic, msg.key, msg.value, headers)
            if not self.is_bootstrapped(msg.topic):
                await self._mark_bootstrap_complete_when_caught_up()

    async def _mark_bootstrap_complete_when_caught_up(self) -> None:
        """Mark each assigned partition bootstrap-complete once its consumer
        position has caught up to the end offset observed at that moment."""
        assert self._consumer is not None
        partitions: frozenset[TopicPartition] = self._consumer.assignment()
        if not partitions:
            return
        end_offsets = await self._consumer.end_offsets(list(partitions))
        for tp in partitions:
            position = await self._consumer.position(tp)
            state = self._state.get(tp.topic)
            if state is None:
                continue
            state.assigned_partitions.add(tp.partition)
            if position >= end_offsets.get(tp, 0):
                state.eof_seen.add(tp.partition)
            if (
                state.assigned_partitions
                and state.eof_seen == state.assigned_partitions
            ):
                state.bootstrap_complete = True

    async def stop(self) -> None:
        self._running = False
        if self._consume_task is not None:
            self._consume_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._consume_task
            self._consume_task = None
        if self._consumer is not None:
            with contextlib.suppress(Exception):
                await self._consumer.stop()
            self._consumer = None
