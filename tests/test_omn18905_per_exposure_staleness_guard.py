# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18905: a frozen exposure must say so instead of reading as live.

The defect, measured on the `.201` dev lane 2026-09-20T15:04-15:07Z: the
projection-api served `runner-fleet` rows stamped `06:06:13Z` from a process
started at `14:39:01Z`, with `GET /ready` reporting all sixteen topics
bootstrapped and `consumer_failure: null`. Three reads fifty seconds apart
returned byte-identical `latest_event_at` while the snapshot topic advanced,
so the cache was frozen rather than slow.

Why nothing caught it: `bootstrap_complete` is a ONE-WAY LATCH and `eof_seen`
is a sticky set, so a topic that caught up once is never re-evaluated, and
`latest_event_at` freezes with the cache so it cannot report its own
staleness. Only the offsets can tell a live exposure from a frozen one.

The guard is deliberately PER EXPOSURE. A whole-API refusal would take every
panel dark to report one frozen topic, and an idle producer would be
indistinguishable from a broken one. Both properties are asserted below.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from omnimarket.projection.api_server import app, get_snapshot_cache, get_topic_map
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.snapshot_cache import (
    DEFAULT_STALE_LAG_RECORDS,
    SnapshotCache,
)

pytestmark = pytest.mark.unit

_BUSY = "onex.snapshot.projection.test-omn18905-busy.v1"
_IDLE = "onex.snapshot.projection.test-omn18905-idle.v1"


def _cfg(topic: str) -> ProjectionTableConfig:
    return ProjectionTableConfig(
        topic=topic,
        table="test_omn18905_table",
        columns=("id",),
        order_by="id ASC",
        order_by_spec=(("id", "ASC", None),),
        bus_backed=True,
        key_columns=("id",),
        limit=100,
    )


def _cache() -> SnapshotCache:
    return SnapshotCache(
        {_BUSY: _cfg(_BUSY), _IDLE: _cfg(_IDLE)},
        bootstrap_servers="unused:9092",
        group_id="test-omn18905-staleness-group",
    )


def _seed(
    cache: SnapshotCache, topic: str, *, applied: int, end: int, complete: bool = True
) -> None:
    """Put one partition's offsets where a real replay would have left them.

    ``complete=True`` by default because the latch is the precondition of the
    defect: every topic in the live incident reported bootstrapped.
    """
    state = cache._state[topic]
    state.assigned_partitions.add(0)
    state.next_position[0] = applied
    state.end_offsets[0] = end
    state.bootstrap_complete = complete
    if complete:
        state.eof_seen.add(0)


@contextmanager
def _client_for(cache: SnapshotCache) -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_snapshot_cache] = lambda: cache
    app.dependency_overrides[get_topic_map] = lambda: {
        _BUSY: _cfg(_BUSY),
        _IDLE: _cfg(_IDLE),
    }
    client = TestClient(app, raise_server_exceptions=True)
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


def test_an_exposure_behind_the_bound_is_stale() -> None:
    """The live shape: caught up once, latched, then left far behind."""
    cache = _cache()
    _seed(cache, _BUSY, applied=1000, end=1000 + DEFAULT_STALE_LAG_RECORDS + 1)

    assert cache.is_stale(_BUSY) is True
    report = cache.lag_report(_BUSY)
    assert report is not None
    assert report["lag"] == DEFAULT_STALE_LAG_RECORDS + 1
    assert report["applied_offset"] == 1000
    assert report["partitions"] == 1


def test_a_caught_up_exposure_is_not_stale() -> None:
    """The positive control AC1 asks for.

    Without this the guard could be passing by refusing unconditionally,
    which would be the same outage as a whole-API refusal wearing a
    per-exposure name.
    """
    cache = _cache()
    _seed(cache, _BUSY, applied=1000, end=1000)

    assert cache.is_stale(_BUSY) is False
    report = cache.lag_report(_BUSY)
    assert report is not None
    assert report["lag"] == 0


def test_an_idle_producer_stays_fresh_while_a_frozen_peer_goes_stale() -> None:
    """This is the property that dissolves the whole-board-dark tradeoff.

    `runtime-error-fingerprints` on the live lane has published exactly one
    delta ever; its snapshot topic high watermark has not moved. It is not
    broken and must not be reported as though it were, in the same response
    cycle in which a genuinely frozen peer is.
    """
    cache = _cache()
    _seed(cache, _IDLE, applied=1, end=1)
    _seed(cache, _BUSY, applied=10, end=10_000)

    assert cache.is_stale(_IDLE) is False
    assert cache.is_stale(_BUSY) is True


def test_an_unmeasured_exposure_is_refused_rather_than_assumed_fresh() -> None:
    """No readable end offset is not evidence of freshness.

    GATE-DIRECTION LAW, the same direction the bootstrap checks already take:
    unknown refuses.
    """
    cache = _cache()
    state = cache._state[_BUSY]
    state.assigned_partitions.add(0)
    state.bootstrap_complete = True

    report = cache.lag_report(_BUSY)
    assert report is not None
    assert report["partitions"] == 0
    assert cache.is_stale(_BUSY) is True


def test_a_missing_end_offset_no_longer_marks_a_partition_caught_up() -> None:
    """The fail-open this fix closes, asserted directly.

    `_record_partition_progress` was called with `end_offsets.get(tp, 0)`, and
    `position >= 0` holds for every position including zero, so a partition
    the broker did not answer for was marked caught up and its topic
    bootstrap-complete.
    """
    from aiokafka import TopicPartition

    cache = _cache()
    tp = TopicPartition(_BUSY, 0)

    cache._record_partition_progress(tp, position=0, end_offset=None)

    state = cache._state[_BUSY]
    assert state.bootstrap_complete is False
    assert 0 not in state.eof_seen


def test_ready_refuses_and_names_the_lagging_topic_with_its_numbers() -> None:
    """A frozen cache cannot report ready (AC1), and says which one and by how much."""
    cache = _cache()
    _seed(cache, _IDLE, applied=5, end=5)
    _seed(cache, _BUSY, applied=10, end=9_000)

    with _client_for(cache) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    # The latch still reads true -- which is exactly why readiness may not be
    # derived from it alone.
    assert body["bus_backed_topics"][_BUSY] is True
    assert body["consumer_failure"] is None
    assert _BUSY in body["lagging_topics"]
    assert _IDLE not in body["lagging_topics"]
    assert body["lagging_topics"][_BUSY]["lag"] == 8_990


def test_ready_is_ready_when_every_exposure_is_caught_up() -> None:
    """The positive control for the endpoint, not just for the predicate."""
    cache = _cache()
    _seed(cache, _IDLE, applied=5, end=5)
    _seed(cache, _BUSY, applied=10, end=10)

    with _client_for(cache) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["lagging_topics"] == {}


def test_a_served_exposure_carries_its_own_staleness_verdict() -> None:
    """AC2: a client can mechanically tell frozen rows from live ones.

    The rows are still served: an exposure the cache stopped following holds
    the last state it saw, which is more useful to a panel than nothing. What
    it may not do is look live.
    """
    cache = _cache()
    _seed(cache, _BUSY, applied=10, end=9_000)

    with _client_for(cache) as client:
        body = client.get(f"/projection/{_BUSY}").json()

    assert body["backing"] == "bus"
    staleness = body["staleness"]
    assert staleness["stale"] is True
    assert staleness["lag_records"] == 8_990
    assert staleness["applied_offset"] == 10
    assert staleness["end_offset"] == 9_000
    assert staleness["partitions_measured"] == 1


def test_a_fresh_exposure_is_served_without_a_stale_verdict() -> None:
    cache = _cache()
    _seed(cache, _IDLE, applied=7, end=7)

    with _client_for(cache) as client:
        body = client.get(f"/projection/{_IDLE}").json()

    assert body["staleness"]["stale"] is False
    assert body["staleness"]["lag_records"] == 0
