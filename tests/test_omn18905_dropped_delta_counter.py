# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18905: the failure the lag guard cannot see.

The per-exposure lag guard that landed in omnimarket#2721 measures how far
behind the end of its topic a cache is. It catches a consumer that has
stopped fetching. It does NOT catch the failure actually frozen on the board,
because that cache reads every record and **discards** it: lag is zero while
the rows stand still.

Measured on the `.201` dev lane 2026-09-20: a trace subscribed to
`onex.snapshot.projection.runner-fleet.v1` alone replayed to position 16,698,
the end of the topic, and still served rows stamped `06:06:13Z`. Every delta
carried `source_offset 0`, so `0 <= 0` refused each one after the first for a
given key -- first writer wins forever, at lag zero, with a green readiness
endpoint.

Consecutive refusals with no apply in between is the only signal that
separates that from a genuinely caught-up exposure. Both look identical by
offset.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from omnimarket.projection.api_server import app, get_snapshot_cache, get_topic_map
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.snapshot_cache import (
    DEFAULT_STALE_DROP_STREAK,
    SnapshotCache,
)

pytestmark = pytest.mark.unit

_TOPIC = "onex.snapshot.projection.test-omn18905-drops.v1"
_SOURCE = "onex.evt.omnibase-infra.runner-fleet.v1"

_CFG = ProjectionTableConfig(
    topic=_TOPIC,
    table="test_omn18905_drops",
    columns=("runner_name", "status"),
    order_by="runner_name ASC",
    order_by_spec=(("runner_name", "ASC", None),),
    bus_backed=True,
    key_columns=("runner_name",),
    limit=100,
)


def _delta(*, runner: str, status: str, observed_at: str, source_offset: int) -> bytes:
    """One snapshot delta, shaped exactly as the live writers publish them."""
    return json.dumps(
        {
            "topic": _TOPIC,
            "key": [runner],
            "op": "upsert",
            "row": {"runner_name": runner, "status": status},
            "observed_at": observed_at,
            "source_event_id": f"evt-{runner}-{observed_at}",
            "source_topic": _SOURCE,
            "source_partition": 0,
            "source_offset": source_offset,
            "projection_version": "projection_snapshot.v1",
        }
    ).encode("utf-8")


def _cache() -> SnapshotCache:
    return SnapshotCache(
        {_TOPIC: _CFG},
        bootstrap_servers="unused:9092",
        group_id="test-omn18905-drops-group",
    )


def _apply(cache: SnapshotCache, payload: bytes) -> None:
    cache.apply_message(_TOPIC, None, payload, [])


def _mark_caught_up(cache: SnapshotCache, *, applied: int = 50) -> None:
    """Zero lag, so every verdict below is the DROP signal and not the lag one."""
    state = cache._state[_TOPIC]
    state.assigned_partitions.add(0)
    state.next_position[0] = applied
    state.end_offsets[0] = applied
    state.bootstrap_complete = True
    state.eof_seen.add(0)


@contextmanager
def _client_for(cache: SnapshotCache) -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_snapshot_cache] = lambda: cache
    app.dependency_overrides[get_topic_map] = lambda: {_TOPIC: _CFG}
    client = TestClient(app, raise_server_exceptions=True)
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


def test_the_live_failure_reproduced_a_constant_source_offset() -> None:
    """Every delta at offset 0: the first lands, the rest are refused."""
    cache = _cache()
    for hour in range(6, 16):
        _apply(
            cache,
            _delta(
                runner="omninode-runner-1",
                status="online" if hour == 6 else "busy",
                observed_at=f"2026-09-20T{hour:02d}:06:13+00:00",
                source_offset=0,
            ),
        )

    report = cache.lag_report(_TOPIC)
    assert report is not None
    # One applied, nine refused -- and the row still carries the FIRST status.
    assert report["dropped_since_apply"] == 9
    assert report["dropped_total"] == 9
    rows = cache.get_rows(_TOPIC)
    assert [r["status"] for r in rows] == ["online"]


def test_a_drop_streak_reads_stale_even_at_zero_lag() -> None:
    """The whole point: zero lag, rows frozen, and the verdict still fires."""
    cache = _cache()
    _mark_caught_up(cache)
    for _ in range(DEFAULT_STALE_DROP_STREAK + 2):
        _apply(
            cache,
            _delta(
                runner="omninode-runner-1",
                status="busy",
                observed_at="2026-09-20T15:00:00+00:00",
                source_offset=0,
            ),
        )

    report = cache.lag_report(_TOPIC)
    assert report is not None
    assert report["lag"] == 0, "the lag guard alone would call this healthy"
    assert cache.is_stale(_TOPIC) is True


def test_an_ordinary_redelivery_does_not_read_stale() -> None:
    """The positive control. A healthy cache refuses redeliveries all day.

    Without this the guard could be passing by calling every exposure stale,
    which is the whole-board-dark outcome the per-exposure design exists to
    avoid.
    """
    cache = _cache()
    _mark_caught_up(cache)
    _apply(
        cache,
        _delta(
            runner="omninode-runner-1",
            status="online",
            observed_at="2026-09-20T15:00:00+00:00",
            source_offset=7,
        ),
    )
    # A couple of genuine redeliveries of the same record.
    for _ in range(2):
        _apply(
            cache,
            _delta(
                runner="omninode-runner-1",
                status="online",
                observed_at="2026-09-20T15:00:00+00:00",
                source_offset=7,
            ),
        )

    assert cache.lag_report(_TOPIC)["dropped_since_apply"] == 2  # type: ignore[index]
    assert cache.is_stale(_TOPIC) is False


def test_a_real_apply_clears_the_streak() -> None:
    """The counter asks "since it last moved", not "ever".

    A cache that is applying is healthy however many redeliveries it has
    declined over its life, which is why the lifetime figure is kept
    separately rather than driving the verdict.
    """
    cache = _cache()
    _mark_caught_up(cache)
    _apply(
        cache,
        _delta(
            runner="omninode-runner-1",
            status="online",
            observed_at="2026-09-20T15:00:00+00:00",
            source_offset=1,
        ),
    )
    for _ in range(DEFAULT_STALE_DROP_STREAK + 2):
        _apply(
            cache,
            _delta(
                runner="omninode-runner-1",
                status="busy",
                observed_at="2026-09-20T15:00:00+00:00",
                source_offset=1,
            ),
        )
    assert cache.is_stale(_TOPIC) is True

    # One delta with a real, advancing offset -- the fix's effect.
    _apply(
        cache,
        _delta(
            runner="omninode-runner-1",
            status="busy",
            observed_at="2026-09-20T15:30:00+00:00",
            source_offset=2,
        ),
    )

    report = cache.lag_report(_TOPIC)
    assert report is not None
    assert report["dropped_since_apply"] == 0
    assert report["dropped_total"] == DEFAULT_STALE_DROP_STREAK + 2
    assert cache.is_stale(_TOPIC) is False
    assert [r["status"] for r in cache.get_rows(_TOPIC)] == ["busy"]


def test_the_served_exposure_reports_its_drops() -> None:
    """A client can tell "reading and discarding" from "not reading"."""
    cache = _cache()
    _mark_caught_up(cache)
    # The first delta for a new key APPLIES, so N sends leave N-1 drops;
    # two over the bound is what puts the streak strictly past it.
    for _ in range(DEFAULT_STALE_DROP_STREAK + 2):
        _apply(
            cache,
            _delta(
                runner="omninode-runner-1",
                status="busy",
                observed_at="2026-09-20T15:00:00+00:00",
                source_offset=0,
            ),
        )

    with _client_for(cache) as client:
        staleness = client.get(f"/projection/{_TOPIC}").json()["staleness"]

    assert staleness["stale"] is True
    assert staleness["lag_records"] == 0
    assert staleness["dropped_since_apply"] == DEFAULT_STALE_DROP_STREAK + 1
    assert staleness["last_dropped_event_at"] == "2026-09-20T15:00:00+00:00"


def test_ready_refuses_on_a_drop_streak_too() -> None:
    """Readiness has to see this class, not only the lag class."""
    cache = _cache()
    _mark_caught_up(cache)
    # The first delta for a new key APPLIES, so N sends leave N-1 drops;
    # two over the bound is what puts the streak strictly past it.
    for _ in range(DEFAULT_STALE_DROP_STREAK + 2):
        _apply(
            cache,
            _delta(
                runner="omninode-runner-1",
                status="busy",
                observed_at="2026-09-20T15:00:00+00:00",
                source_offset=0,
            ),
        )

    with _client_for(cache) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    body = response.json()
    assert _TOPIC in body["lagging_topics"]
    assert body["lagging_topics"][_TOPIC]["lag"] == 0
    assert body["lagging_topics"][_TOPIC]["dropped_since_apply"] == (
        DEFAULT_STALE_DROP_STREAK + 1
    )
