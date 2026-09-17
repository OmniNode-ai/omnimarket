# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17215 cause 1: a cursor walk must be able to pass the contract limit.

Both cursor-paginated read paths (``projection_query`` behind
``GET /projection/{topic}`` and ``_evidence_projection_response`` behind
``/v1/evidence-pipeline/*``) ask the cache for every row, apply the ``since`` /
``cursor`` and content filters, and only then cut the page. They made that
request as ``cache.get_rows(limit=None)``, but ``SnapshotCache.get_rows`` reads
``limit=None`` as "the exposure's contract limit". The cache therefore handed
back only the lowest-cursor ``exposure.limit`` rows BEFORE the filter ran:

* a page at the contract limit saw exactly ``limit`` rows, judged itself
  complete, and advertised ``next_cursor: null`` while the cache held more;
* a smaller page's cursor led to at most the remainder of that first window,
  so no walk could ever reach row ``limit + 1``.

Observed live on the ``.201`` dev lane on 2026-09-15: ``limit=500/501/1000``
served 500 rows with a null cursor; ``limit=499`` then ``since`` served one
row and a null cursor, while the database held thousands of newer rows.

The existing route tests use a MagicMock cache that ignores ``limit``, which is
why they stayed green. Every test here drives the REAL ``SnapshotCache``
(seeded through ``apply_message``, the path the live consumer uses) so the
cache's own ``limit`` semantics are what the routes actually meet.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from omnimarket.projection.api_server import app, get_snapshot_cache, get_topic_map
from omnimarket.projection.models import ProjectionStatus, ProjectionTableConfig
from omnimarket.projection.snapshot_cache import SnapshotCache

pytestmark = pytest.mark.unit

_CONTRACT_LIMIT = 5
# More than two full pages at the contract limit, and well under the cache's
# retention cap (limit * 4), so eviction plays no part in what is served.
_CACHED_ROWS = 12

_FLOW_TOPIC = "onex.snapshot.projection.test-omn17215-cursor-walk.v1"
_EVIDENCE_TOPIC = "onex.snapshot.projection.evidence_pipeline.stages.v1"  # onex-topic-allow: the evidence-pipeline route serves this fixed snapshot topic
_SOURCE_TOPIC = "onex.evt.platform.node-heartbeat.v1"


def _flow_cfg() -> ProjectionTableConfig:
    # Descending presentation order, as consumer-flow declares: pages must
    # still be SELECTED in ascending cursor order (OMN-18043 ruling).
    return ProjectionTableConfig(
        topic=_FLOW_TOPIC,
        table="test_omn17215_cursor_walk",
        columns=("projection_cursor", "consumer_group", "window_end"),
        order_by="projection_cursor DESC",
        order_by_spec=(("projection_cursor", "DESC", None),),
        cursor_column="projection_cursor",
        limit=_CONTRACT_LIMIT,
        bus_backed=True,
        key_columns=("projection_cursor",),
    )


def _evidence_cfg() -> ProjectionTableConfig:
    return ProjectionTableConfig(
        topic=_EVIDENCE_TOPIC,
        table="evidence_dashboard_projection",
        schema_name="public",
        columns=(
            "projection_cursor",
            "correlation_id",
            "last_ingest_sequence",
            "observed_at",
        ),
        order_by="last_ingest_sequence ASC",
        order_by_spec=(("last_ingest_sequence", "ASC", None),),
        freshness_column="observed_at",
        cursor_column="projection_cursor",
        last_ingest_sequence_column="last_ingest_sequence",
        observed_at_column="observed_at",
        limit=_CONTRACT_LIMIT,
        status=ProjectionStatus.OK,
        bus_backed=True,
        key_columns=("projection_cursor",),
    )


def _row(topic: str, cursor: int) -> dict[str, Any]:
    if topic == _EVIDENCE_TOPIC:
        return {
            "projection_cursor": cursor,
            "correlation_id": f"corr-{cursor}",
            "last_ingest_sequence": cursor,
            "observed_at": "2026-09-15T14:13:59+00:00",
        }
    return {
        "projection_cursor": cursor,
        "consumer_group": f"group-{cursor}",
        "window_end": f"2026-09-15T14:13:{cursor:02d}+00:00",
    }


def _seeded_cache(cfg: ProjectionTableConfig) -> SnapshotCache:
    """A real SnapshotCache holding ``_CACHED_ROWS`` rows for ``cfg.topic``.

    Rows are applied out of cursor order so an observed ascending walk is the
    cache's sort, not insertion order echoed back.
    """
    cache = SnapshotCache(
        {cfg.topic: cfg},
        bootstrap_servers="unused:9092",
        # Explicit group id: default derivation needs ONEX_ENVIRONMENT and is
        # not what this test exercises.
        group_id="test-omn17215-cursor-walk-group",
    )
    order = [7, 1, 12, 4, 9, 2, 11, 5, 8, 3, 10, 6]
    assert sorted(order) == list(range(1, _CACHED_ROWS + 1))
    for offset, cursor in enumerate(order, start=1):
        payload = {
            "topic": cfg.topic,
            "key": [str(cursor)],
            "op": "upsert",
            "row": _row(cfg.topic, cursor),
            "observed_at": f"2026-09-15T14:14:{offset:02d}+00:00",
            "source_event_id": f"evt-{cursor}",
            "source_topic": _SOURCE_TOPIC,
            "source_partition": 0,
            "source_offset": offset,
            "projection_version": "projection_snapshot.v1",
        }
        cache.apply_message(
            cfg.topic,
            key=str(cursor).encode("utf-8"),
            value=json.dumps(payload).encode("utf-8"),
            headers=[("tenant_id", b"omninode")],
        )
    # No live consumer: mark bootstrap complete directly, as a caught-up
    # partition assignment would (same pattern as the OMN-16290 test).
    cache._state[cfg.topic].bootstrap_complete = True
    assert cache.row_count(cfg.topic) == _CACHED_ROWS
    return cache


@contextmanager
def _client(
    cfg: ProjectionTableConfig, cache: SnapshotCache
) -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_snapshot_cache] = lambda: cache
    app.dependency_overrides[get_topic_map] = lambda: {cfg.topic: cfg}
    try:
        yield TestClient(app, raise_server_exceptions=True)
    finally:
        app.dependency_overrides.clear()


def _cursors(body: dict[str, Any]) -> list[int]:
    return [int(row["projection_cursor"]) for row in body["rows"]]


# ---------------------------------------------------------------------------
# GET /projection/{topic}
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("limit", [None, _CONTRACT_LIMIT, _CONTRACT_LIMIT + 1, 1000])
def test_projection_page_at_contract_limit_advertises_a_cursor(
    limit: int | None,
) -> None:
    """The live ``limit=500/501/1000`` readback: a full page must not claim to
    be the complete set while the cache holds more rows."""
    cfg = _flow_cfg()
    params = {} if limit is None else {"limit": limit}
    with _client(cfg, _seeded_cache(cfg)) as client:
        resp = client.get(f"/projection/{_FLOW_TOPIC}", params=params)
    assert resp.status_code == 200
    body = resp.json()
    assert body["row_limit"] == _CONTRACT_LIMIT
    assert sorted(_cursors(body)) == [1, 2, 3, 4, 5]
    assert body["next_cursor"] == "5"


@pytest.mark.parametrize("page_size", [_CONTRACT_LIMIT, _CONTRACT_LIMIT - 1, 1])
def test_projection_since_walk_reaches_every_cached_row_exactly_once(
    page_size: int,
) -> None:
    """Following ``next_cursor`` to null visits the whole cached set: no row
    served twice, none skipped, and the walk passes the contract limit."""
    cfg = _flow_cfg()
    cache = _seeded_cache(cfg)
    walked: list[int] = []
    since: str | None = None
    with _client(cfg, cache) as client:
        for _ in range(_CACHED_ROWS + 2):  # bound: one row per page at worst
            params: dict[str, Any] = {"limit": page_size}
            if since is not None:
                params["since"] = since
            resp = client.get(f"/projection/{_FLOW_TOPIC}", params=params)
            assert resp.status_code == 200
            body = resp.json()
            walked.extend(_cursors(body))
            since = body["next_cursor"]
            if since is None:
                break
    assert since is None, "walk did not terminate with a null cursor"
    assert len(walked) == len(set(walked)), f"overlap in walk: {walked}"
    cached = cache.get_rows(_FLOW_TOPIC, limit=_CACHED_ROWS)
    assert len(cached) == cache.row_count(_FLOW_TOPIC)
    expected = sorted(int(r["projection_cursor"]) for r in cached)
    assert sorted(walked) == expected == list(range(1, _CACHED_ROWS + 1))


# ---------------------------------------------------------------------------
# /v1/evidence-pipeline/* (the sibling seam, same read shape)
# ---------------------------------------------------------------------------


def test_evidence_page_at_contract_limit_advertises_a_cursor() -> None:
    cfg = _evidence_cfg()
    with _client(cfg, _seeded_cache(cfg)) as client:
        resp = client.get(
            "/v1/evidence-pipeline/stages", params={"limit": _CONTRACT_LIMIT}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert _cursors(body) == [1, 2, 3, 4, 5]
    assert body["next_cursor"] == "5"


def test_evidence_cursor_walk_reaches_every_cached_row_exactly_once() -> None:
    cfg = _evidence_cfg()
    walked: list[int] = []
    cursor: str | None = None
    with _client(cfg, _seeded_cache(cfg)) as client:
        for _ in range(_CACHED_ROWS + 2):
            params: dict[str, Any] = {"limit": _CONTRACT_LIMIT}
            if cursor is not None:
                params["cursor"] = cursor
            resp = client.get("/v1/evidence-pipeline/stages", params=params)
            assert resp.status_code == 200
            body = resp.json()
            walked.extend(_cursors(body))
            cursor = body["next_cursor"]
            if cursor is None:
                break
    assert cursor is None, "walk did not terminate with a null cursor"
    assert walked == list(range(1, _CACHED_ROWS + 1))


def test_evidence_content_filter_sees_rows_beyond_the_contract_limit() -> None:
    """The same cap ran before the correlation/ticket/repo filters, so a row
    past position ``limit`` in cursor order could never be found by filter."""
    cfg = _evidence_cfg()
    with _client(cfg, _seeded_cache(cfg)) as client:
        resp = client.get(
            "/v1/evidence-pipeline/stages", params={"correlation_id": "corr-11"}
        )
    assert resp.status_code == 200
    body = resp.json()
    assert _cursors(body) == [11]
    assert body["next_cursor"] is None


# ---------------------------------------------------------------------------
# The cache's default for other callers is unchanged
# ---------------------------------------------------------------------------


def test_get_rows_default_and_explicit_limits_are_unchanged() -> None:
    """Callers that rely on ``limit=None`` meaning the contract limit (and on an
    explicit ``limit``) keep that behaviour."""
    cfg = _flow_cfg()
    cache = _seeded_cache(cfg)
    assert len(cache.get_rows(_FLOW_TOPIC)) == _CONTRACT_LIMIT
    assert len(cache.get_rows(_FLOW_TOPIC, limit=None)) == _CONTRACT_LIMIT
    assert len(cache.get_rows(_FLOW_TOPIC, limit=3)) == 3


def test_get_rows_unbounded_returns_the_whole_ordered_set() -> None:
    cfg = _flow_cfg()
    cache = _seeded_cache(cfg)
    rows = cache.get_rows(
        _FLOW_TOPIC,
        unbounded=True,
        order_by_override=(("projection_cursor", "ASC", None),),
    )
    assert [r["projection_cursor"] for r in rows] == list(range(1, _CACHED_ROWS + 1))


def test_get_rows_refuses_unbounded_with_an_explicit_limit() -> None:
    cache = _seeded_cache(_flow_cfg())
    with pytest.raises(ValueError, match="unbounded=True"):
        cache.get_rows(_FLOW_TOPIC, unbounded=True, limit=3)
