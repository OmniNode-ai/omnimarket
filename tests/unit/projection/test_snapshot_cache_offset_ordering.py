# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15800 busread-corrective round 3 -- CodeRabbit discussion_r3745850632
(runner.py:586 / SnapshotCache.apply_message): ``time.time_ns()`` is not a
safe cross-process ordering token.

``SnapshotCache.apply_message`` used to drop a delta whose process-local
``ingest_sequence = time.time_ns()`` was <= the cached value. Two conditions
broke that silently: a wall-clock step backward (NTP), and a consumer-group
rebalance that hands a source partition to a replica with a lagging clock.
Both cases discarded a genuinely newer row forever -- it stayed correct in
Postgres and never reached the bus-backed serving layer.

The fix: ordering authority is the SOURCE Kafka message's own coordinates
(``source_topic``, ``source_partition``, ``source_offset``) -- broker-
assigned, monotonic per (topic, partition), and independent of any
replica's wall clock. ``observed_at`` remains display-only metadata; it is
never consulted for the staleness decision.

RED before the fix (recorded 2026-08-10, pre-implementation on this round):
``ModelProjectionSnapshotDelta`` had no ``source_topic``/``source_partition``/
``source_offset`` fields (``extra="forbid"``, so the payload below fails
validation with an ``ValidationError: Extra inputs are not permitted``), and
``SnapshotCache``/``CachedRow`` compared a process-local ``ingest_sequence``
instead. Every test in this module failed against pre-fix code.
"""

from __future__ import annotations

import json

from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.snapshot_cache import SnapshotCache

_TOPIC = "onex.snapshot.projection.test-offset-ordering.v1"
_SOURCE_TOPIC = "onex.evt.platform.node-heartbeat.v1"


def _make_cache() -> SnapshotCache:
    exposure = ProjectionTableConfig(
        topic=_TOPIC,
        table="test_table",
        columns=("id", "value"),
        bus_backed=True,
        key_columns=("id",),
        limit=100,
    )
    return SnapshotCache(
        {_TOPIC: exposure},
        bootstrap_servers="unused:9092",
        # Explicit override (OMN-15840): this test exercises offset ordering,
        # not the default group-id derivation, which requires ONEX_ENVIRONMENT.
        group_id="test-offset-ordering-group",
    )


def _delta_bytes(
    *,
    row_id: str,
    value: int,
    observed_at: str,
    source_partition: int,
    source_offset: int,
    source_topic: str = _SOURCE_TOPIC,
) -> bytes:
    payload = {
        "topic": _TOPIC,
        "key": [row_id],
        "op": "upsert",
        "row": {"id": row_id, "value": value},
        "observed_at": observed_at,
        "source_event_id": f"evt-{row_id}-{source_offset}",
        "source_topic": source_topic,
        "source_partition": source_partition,
        "source_offset": source_offset,
        "projection_version": "projection_snapshot.v1",
    }
    return json.dumps(payload).encode("utf-8")


def test_later_offset_wins_despite_earlier_wall_clock() -> None:
    """NTP backward step: the genuinely newer event carries a LATER Kafka
    offset but an EARLIER wall-clock ``observed_at`` (its publishing
    replica's clock stepped back). The cache must keep the later-offset
    row -- offset, not time.time_ns(), is the ordering authority."""
    cache = _make_cache()

    cache.apply_message(
        _TOPIC,
        key=b"row-1",
        value=_delta_bytes(
            row_id="row-1",
            value=1,
            observed_at="2026-08-10T12:00:00+00:00",  # later wall clock
            source_partition=0,
            source_offset=100,  # earlier offset -- the actually-older event
        ),
        headers=[("tenant_id", b"omninode")],
    )
    cache.apply_message(
        _TOPIC,
        key=b"row-1",
        value=_delta_bytes(
            row_id="row-1",
            value=2,
            observed_at="2026-08-10T11:00:00+00:00",  # EARLIER wall clock (NTP step back)
            source_partition=0,
            source_offset=101,  # LATER offset -- the genuinely newer event
        ),
        headers=[("tenant_id", b"omninode")],
    )

    rows = cache.get_rows(_TOPIC)
    assert len(rows) == 1
    assert rows[0]["value"] == 2, (
        "the later-offset delta must win regardless of its earlier "
        "wall-clock observed_at"
    )


def test_genuinely_older_offset_loses_even_if_applied_later() -> None:
    """A retried/delayed publish of an already-superseded source offset
    (e.g. reprocessing after a crash-restart) must never regress the
    cache, however much wall-clock time has passed since the newer delta
    landed."""
    cache = _make_cache()

    cache.apply_message(
        _TOPIC,
        key=b"row-2",
        value=_delta_bytes(
            row_id="row-2",
            value=20,
            observed_at="2026-08-10T09:00:00+00:00",
            source_partition=0,
            source_offset=50,
        ),
        headers=[("tenant_id", b"omninode")],
    )
    # A stale replay of an OLDER offset arrives much later in wall-clock time.
    cache.apply_message(
        _TOPIC,
        key=b"row-2",
        value=_delta_bytes(
            row_id="row-2",
            value=10,
            observed_at="2026-08-10T23:00:00+00:00",  # much later wall clock
            source_partition=0,
            source_offset=49,  # but an OLDER offset
        ),
        headers=[("tenant_id", b"omninode")],
    )

    rows = cache.get_rows(_TOPIC)
    assert len(rows) == 1
    assert rows[0]["value"] == 20, (
        "a genuinely older offset must lose even when it is applied after "
        "the newer delta in wall-clock time"
    )


def test_equal_offset_is_idempotent_replay_and_is_dropped() -> None:
    """Re-applying the identical (source_partition, source_offset) -- e.g.
    a replay of an already-applied message after a rebalance -- is a
    no-op, matching the pre-fix ``<=`` idempotence semantics."""
    cache = _make_cache()

    cache.apply_message(
        _TOPIC,
        key=b"row-3",
        value=_delta_bytes(
            row_id="row-3",
            value=1,
            observed_at="2026-08-10T09:00:00+00:00",
            source_partition=0,
            source_offset=7,
        ),
        headers=[("tenant_id", b"omninode")],
    )
    cache.apply_message(
        _TOPIC,
        key=b"row-3",
        value=_delta_bytes(
            row_id="row-3",
            value=999,  # different row content -- must NOT overwrite
            observed_at="2026-08-10T09:05:00+00:00",
            source_partition=0,
            source_offset=7,  # identical offset -- replay
        ),
        headers=[("tenant_id", b"omninode")],
    )

    rows = cache.get_rows(_TOPIC)
    assert rows[0]["value"] == 1


def test_different_source_topic_is_never_compared_by_offset() -> None:
    """Two different source topics feeding the same cache key (e.g.
    ``node-introspection.v1`` and ``node-heartbeat.v1`` both updating the
    same ``service_name`` row) have independent offset spaces -- a low
    offset from one topic must never be treated as stale relative to a
    high offset already cached from a different topic."""
    cache = _make_cache()

    cache.apply_message(
        _TOPIC,
        key=b"row-4",
        value=_delta_bytes(
            row_id="row-4",
            value=1,
            observed_at="2026-08-10T09:00:00+00:00",
            source_partition=0,
            source_offset=500000,
            source_topic="onex.evt.platform.node-introspection.v1",
        ),
        headers=[("tenant_id", b"omninode")],
    )
    cache.apply_message(
        _TOPIC,
        key=b"row-4",
        value=_delta_bytes(
            row_id="row-4",
            value=2,
            observed_at="2026-08-10T09:00:01+00:00",
            source_partition=0,
            source_offset=3,  # tiny offset -- but a DIFFERENT source topic's own stream
            source_topic="onex.evt.platform.node-heartbeat.v1",
        ),
        headers=[("tenant_id", b"omninode")],
    )

    rows = cache.get_rows(_TOPIC)
    assert rows[0]["value"] == 2, (
        "a delta from a different source topic must always apply -- its "
        "offset is not comparable to a different topic's offset space"
    )
