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
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aiokafka import AIOKafkaConsumer, TopicPartition
from omnibase_infra.enums import EnumConsumerGroupPurpose
from omnibase_infra.event_bus.kafka_auth import build_aiokafka_auth_kwargs_from_env
from omnibase_infra.models import ModelNodeIdentity
from omnibase_infra.utils import apply_instance_discriminator, compute_consumer_group_id

from omnimarket.projection.models import (
    ModelProjectionSnapshotDelta,
    ProjectionTableConfig,
)

logger = logging.getLogger(__name__)

# A per-process-unique group, never a shared literal (CodeRabbit, OMN-15800):
# SnapshotCache is a full-topic STATE CACHE, not a work queue -- every
# replica must see every partition. A single static group_id would let Kafka
# split partitions across replicas (each caching only a subset while both
# report bootstrap_complete=True). group_id defaults to a canonically-derived
# base (OMN-15840) plus a fresh uuid4 instance discriminator per process, so
# each process is still its own consumer group and always gets the complete
# compacted topic -- while landing inside the MSK-IAM-pinned "onex-dev.*"
# pattern. The pre-OMN-15840 literal prefix (f"{prefix}-{uuid4()}") matched
# none of the six patterns pinned in
# omninode_infra/tests/test_msk_group_pattern_pin.py and died
# GroupAuthorizationFailedError before the consumer could join -- same defect
# class as OMN-15700 (omnibase_infra#2681), whose ModelNodeIdentity +
# compute_consumer_group_id mechanism this reuses rather than a parallel one.
_GROUP_SERVICE_NAME = "omnimarket-projection-api"
_GROUP_NODE_NAME = "snapshot-cache"
_GROUP_VERSION = "v1"
DEFAULT_CLIENT_ID = "omnimarket-projection-api-snapshot-cache"
_BOOTSTRAP_POLL_INTERVAL_SECONDS = 0.5
_BOOTSTRAP_POLL_MAX_ATTEMPTS = 40  # ~20s to observe a partition assignment
# OMN-15876: batch size for the post-bootstrap-poll consume loop's
# getmany() calls. The catch-up check runs at most once per batch (not once
# per message), so this bounds RPC overhead to roughly
# (backlog_size / this) end_offsets()/position() round trips during a fresh
# pod's bootstrap replay, independent of how large the topic's retained
# backlog is.
_CONSUME_BATCH_MAX_RECORDS = 500


def _default_group_id() -> str:
    """Derive this instance's default consumer group id canonically.

    Reuses the same mechanism as omnibase_infra#2681 (OMN-15700): a typed
    ``ModelNodeIdentity`` fed through ``compute_consumer_group_id()``, rather
    than a bespoke f-string literal, so the result is authorized by the
    MSK-IAM-pinned ``onex-dev.*`` pattern in the deployed environment.

    The environment component MUST come from ``ONEX_ENVIRONMENT`` -- the
    deployment env var the onex-dev ConfigMap already sets for every other
    runtime process. This reads it via ``os.environ[...]`` (fail-fast) rather
    than ``os.getenv(..., "local")``: a silent "local" default is the exact
    fail-open class flagged on OMN-15835 for the sibling savings-estimator
    group, and it would derive a group id that is authorized nowhere.
    """
    environment = os.environ["ONEX_ENVIRONMENT"]
    identity = ModelNodeIdentity(
        env=environment,
        service=_GROUP_SERVICE_NAME,
        node_name=_GROUP_NODE_NAME,
        version=_GROUP_VERSION,
    )
    base_group_id = compute_consumer_group_id(
        identity, EnumConsumerGroupPurpose.CONSUME
    )
    # Per-instance uniqueness (see module comment above): every replica of
    # this full-topic state cache must be its own consumer group.
    return apply_instance_discriminator(base_group_id, str(uuid.uuid4()))  # type: ignore[no-any-return]


@dataclass(frozen=True)
class CachedRow:
    """One cached row: the serialized column values plus cache metadata.

    ``observed_at`` is display-only (never consulted for staleness -- it is
    wall-clock time and can move backward across replicas/NTP steps).
    ``source_topic``/``source_partition``/``source_offset`` are the
    authoritative ordering token: broker-assigned Kafka coordinates of the
    SOURCE message that produced this row (CodeRabbit, OMN-15800 round 3,
    discussion r3745850632).
    """

    row: dict[str, Any]
    observed_at: datetime
    source_topic: str
    source_partition: int
    source_offset: int
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

    ``None`` sorts last by default, independent of ASC/DESC direction
    (matches every pre-OMN-15800-defect-A order_by, none of which declared
    NULLS placement explicitly). A contract that explicitly declares ``NULLS
    FIRST`` flips this per column via ``nulls_last=False`` (OMN-15800 defect
    A) -- direction and NULLS placement are independent SQL knobs.
    """

    __slots__ = ("nulls_last", "reverse", "value")

    def __init__(self, value: Any, reverse: bool, nulls_last: bool = True) -> None:
        self.value = value
        self.reverse = reverse
        # OMN-15800 defect A: explicit NULLS FIRST|LAST placement, independent
        # of ASC/DESC direction (SQL semantics — an explicit NULLS clause
        # overrides whatever the direction's implicit default would be).
        # Defaults True to preserve this cache's pre-existing behavior for
        # every order_by that does not declare NULLS explicitly (nulls always
        # sorted last, unconditionally).
        self.nulls_last = nulls_last

    def __lt__(self, other: _SortWrapper) -> bool:
        if self.value is None and other.value is None:
            return False
        if self.value is None:
            return not self.nulls_last
        if other.value is None:
            return self.nulls_last
        if self.reverse:
            return bool(other.value < self.value)
        return bool(self.value < other.value)


def _sort_rows(
    items: list[tuple[tuple[str, ...], CachedRow]],
    order_by_spec: tuple[tuple[str, str, str | None], ...],
) -> list[tuple[tuple[str, ...], CachedRow]]:
    if not order_by_spec:
        return items

    def _sort_key(
        item: tuple[tuple[str, ...], CachedRow],
    ) -> tuple[_SortWrapper, ...]:
        _key, cached = item
        return tuple(
            _SortWrapper(
                cached.row.get(column),
                reverse=(direction == "DESC"),
                nulls_last=(nulls != "FIRST"),
            )
            for column, direction, nulls in order_by_spec
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
        group_id: str | None = None,
        client_id: str = DEFAULT_CLIENT_ID,
    ) -> None:
        self._exposures: dict[str, ProjectionTableConfig] = {
            topic: cfg for topic, cfg in exposures.items() if cfg.bus_backed
        }
        self._bootstrap_servers = bootstrap_servers
        # Per-process-unique unless a caller explicitly pins one (tests).
        self._group_id = group_id or _default_group_id()
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
            # source offset to compare (see publish_snapshot_delta).
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

        # Staleness authority (CodeRabbit, OMN-15800 round 3, discussion
        # r3745850632): compare the SOURCE message's own (partition, offset)
        # -- never wall-clock time. A delta from a DIFFERENT source_topic
        # than what's cached is never comparable (independent offset
        # spaces -- e.g. node-introspection.v1 and node-heartbeat.v1 both
        # update the same registry row) and always applies; only a replay
        # of an offset <= the cached one from the SAME source_topic+
        # source_partition is dropped as stale/idempotent-replay.
        existing = state.rows.get(delta.key)
        if (
            existing is not None
            and delta.source_topic == existing.source_topic
            and delta.source_partition == existing.source_partition
            and delta.source_offset <= existing.source_offset
        ):
            return  # stale/replayed delta relative to cache state -- idempotent

        observed_at = _parse_observed_at(delta.observed_at)
        state.rows[delta.key] = CachedRow(
            row=dict(delta.row or {}),
            observed_at=observed_at,
            source_topic=delta.source_topic,
            source_partition=delta.source_partition,
            source_offset=delta.source_offset,
            tenant_id=tenant_id,
        )
        if state.latest_event_at is None or observed_at > state.latest_event_at:
            state.latest_event_at = observed_at

        exposure = self._exposures[topic]
        max_rows = exposure.limit * 4
        if len(state.rows) > max_rows:
            # Evict by RECENCY (lowest observed_at first), never by the
            # exposure's display order_by_spec (CodeRabbit, OMN-15800): for an
            # ASC-ordered exposure, sorting-then-truncating-to-head would keep
            # the OLDEST rows forever and evict the row this call just wrote.
            # Retention and display ordering are separate concerns.
            # observed_at (wall-clock, display-only metadata) is a globally
            # comparable soft-LRU signal here -- capacity eviction is not the
            # correctness-critical authority path (that's the offset-keyed
            # staleness check above); a rare mis-ordered eviction under an
            # NTP step is a capacity heuristic miss, not a silently dropped
            # newer row.
            newest_first = sorted(
                state.rows.items(),
                key=lambda item: item[1].observed_at,
                reverse=True,
            )
            state.rows = dict(newest_first[:max_rows])

    def get_rows(
        self,
        topic: str,
        *,
        limit: int | None = None,
        order_by_override: tuple[tuple[str, str, str | None], ...] | None = None,
    ) -> list[dict[str, Any]]:
        """Return cached rows for ``topic``, ordered per the exposure's order_by_spec.

        ``order_by_override`` lets a caller apply a caller-requested direction
        flip (the ``?order=asc|desc`` query param) to the ACTUAL returned rows
        -- not just to the response envelope's ``ordering`` string, which
        would otherwise silently diverge from the real row order.
        """
        state = self._state.get(topic)
        if state is None:
            return []
        exposure = self._exposures[topic]
        spec = (
            order_by_override
            if order_by_override is not None
            else exposure.order_by_spec
        )
        ordered = _sort_rows(list(state.rows.items()), spec)
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
        self._consumer = AIOKafkaConsumer(  # no-contract-check: projection-api runtime owns the snapshot-cache consumer lifecycle (OMN-15800), same runtime-boundary pattern as BaseProjectionRunner.run()
            *self._exposures.keys(),
            bootstrap_servers=self._bootstrap_servers,
            group_id=self._group_id,
            client_id=self._client_id,
            auto_offset_reset="earliest",
            enable_auto_commit=False,
            value_deserializer=None,
            # OMN-15816: onex-dev's managed Kafka listener is SASL_SSL/
            # AWS_MSK_IAM-only -- a client built without these kwargs defaults
            # to PLAINTEXT and the broker closes the connection immediately.
            # Same idiom as omnibase_infra's own consumers, e.g.
            # AgentActionsConsumer (services/observability/agent_actions/consumer.py).
            **build_aiokafka_auth_kwargs_from_env(),
        )
        await self._consumer.start()
        self._running = True
        self._consume_task = asyncio.ensure_future(self._consume_loop())

    async def _consume_loop(self) -> None:
        assert self._consumer is not None
        # Poll (not a one-shot check) so a topic with ZERO messages still
        # reaches bootstrap_complete=True: aiokafka assigns partitions lazily
        # after the group join, so the assignment is frequently empty on the
        # very first call, and an idle/empty compacted topic never delivers a
        # message to re-trigger the check from inside the loop below
        # (CodeRabbit, OMN-15800). Bounded retries, not an infinite poll.
        for _attempt in range(_BOOTSTRAP_POLL_MAX_ATTEMPTS):
            await self._mark_bootstrap_complete_when_caught_up()
            if all(state.bootstrap_complete for state in self._state.values()):
                break
            await asyncio.sleep(_BOOTSTRAP_POLL_INTERVAL_SECONDS)
        else:
            logger.warning(
                "SnapshotCache: bootstrap did not complete for all topics "
                "within %d attempts; still-incomplete topics keep serving "
                "503 snapshot_bootstrap_incomplete until a future poll "
                "inside the consume loop catches them up",
                _BOOTSTRAP_POLL_MAX_ATTEMPTS,
            )
        # OMN-15876: consume in BATCHES and run the RPC-heavy catch-up check
        # at most once per batch, never once per message. The prior
        # `async for msg in self._consumer: ... if not bootstrapped: await
        # _mark_bootstrap_complete_when_caught_up()` shape fired an
        # end_offsets() broker round trip (across every assigned partition)
        # plus a position() round trip per assigned partition on EVERY
        # message from a not-yet-bootstrapped topic. Against a topic with a
        # large, actively-growing retained backlog (onex-dev's
        # live-events.v1 exposure: 60,724+ messages) that is tens of
        # thousands of unbatched broker round trips before a single fresh
        # pod's bootstrap replay can complete -- observed live as a pod that
        # ran 47+ minutes with zero restarts and never left /ready 503
        # (readinessProbe only marks NotReady; it never restarts the pod, so
        # this was a genuine non-convergent-in-practice algorithm, not a
        # crash). Batching bounds RPC overhead by (batch count), not
        # (message count), independent of backlog size.
        while self._running:
            batches = await self._consumer.getmany(
                timeout_ms=int(_BOOTSTRAP_POLL_INTERVAL_SECONDS * 1000),
                max_records=_CONSUME_BATCH_MAX_RECORDS,
            )
            if not self._running:
                break
            if not batches:
                continue
            any_unbootstrapped = False
            for _tp, messages in batches.items():
                for msg in messages:
                    headers = list(msg.headers or [])
                    self.apply_message(msg.topic, msg.key, msg.value, headers)
                    if not self.is_bootstrapped(msg.topic):
                        any_unbootstrapped = True
            if any_unbootstrapped:
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
