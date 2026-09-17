# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17215 cause 2: the consumer-flow cache must be bounded by its consumers.

AC1: every consumer group present in ``consumer_flow_windows`` for the current
window is reachable through ``GET /projection/onex.snapshot.projection.
consumer-flow.v1``, by paging if necessary.

The exposure used to key its snapshot on ``(consumer_group, topic,
window_start)``. Every heartbeat window therefore minted a NEW cache key, so the
cache accumulated window history it never serves (5,020 keys per ten minutes
live against a 2,000-row retention cap) and its recency trim evicted whole
pairs: a consumer whose latest window was older than the newest 2,000 rows of
everyone else's traffic disappeared from the served set while its group was
still live. Keyed on ``(consumer_group, topic)``, a new window REPLACES the
pair's previous row, so the cache holds one row per pair -- bounded by the
consumer population rather than by traffic -- and the retention trim cannot
reach a live pair while the pair population stays under the cap. The table
keeps the full window history either way.

Everything here runs through the shipped path: the exposure is parsed from the
real ``contract.yaml``, every delta is encoded by ``encode_snapshot_delta`` (the
function the writer's ``publish_snapshot_delta`` calls, which derives the Kafka
key from ``key_columns``), and every delta is applied by the real
``SnapshotCache.apply_message`` at the contract's own ``limit``.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from omnimarket.projection.api_server import app, get_snapshot_cache, get_topic_map
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.snapshot_cache import SnapshotCache
from omnimarket.projection.snapshot_publisher import encode_snapshot_delta

pytestmark = pytest.mark.unit

_TOPIC = "onex.snapshot.projection.consumer-flow.v1"
_SOURCE_TOPIC = "onex.evt.platform.node-heartbeat.v1"
_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_consumer_flow"
    / "contract.yaml"
)

# A busy runtime heartbeating every pair on every window, for long enough that
# the per-window rows cross the cache's trim threshold (limit * 4 * 2 = 4,000
# at the contract's limit of 500). 12 pairs x 400 windows = 4,800 rows.
_BUSY_PAIRS: tuple[tuple[str, str], ...] = tuple(
    (f"group-{group}", f"topic-{topic}") for group in range(4) for topic in range(3)
)
_BUSY_WINDOWS = 400
# A live consumer that published its current window before the busy traffic
# arrived and has not needed to republish since.
_QUIET_PAIR = ("group-quiet", "topic-quiet")
_ALL_PAIRS = frozenset((*_BUSY_PAIRS, _QUIET_PAIR))

_BASE = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
_WINDOW = timedelta(seconds=30)


def _consumer_flow_cfg() -> ProjectionTableConfig:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    exposures = load_projection_exposures_from_contract(
        contract, "projection_consumer_flow", _CONTRACT_PATH
    )
    assert len(exposures) == 1, exposures
    cfg = exposures[0]
    assert cfg.topic == _TOPIC
    assert cfg.bus_backed
    return cfg


def _row(
    pair: tuple[str, str], *, window: int, cursor: int, node_id: str
) -> dict[str, Any]:
    window_start = _BASE + _WINDOW * window
    window_end = window_start + _WINDOW
    return {
        "projection_cursor": cursor,
        "consumer_group": pair[0],
        "topic": pair[1],
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "node_id": node_id,
        "ingest_sequence": window,
        "messages_in": 3,
        "messages_out": 3,
        "messages_dlq": 0,
        "handler_errors": 0,
        "upstream_produced": 3,
        "upstream_evidence": "OBSERVED",
        "flow_state": "FLOWING",
        "evaluated_at": window_end.isoformat(),
    }


class _Seeded:
    """The cache after the traffic above, plus what the database would hold as
    each pair's newest window."""

    def __init__(self) -> None:
        self.cfg = _consumer_flow_cfg()
        self.cache = SnapshotCache(
            {_TOPIC: self.cfg},
            bootstrap_servers="unused:9092",
            group_id="test-omn17215-consumer-flow-one-row-per-pair",
        )
        self.newest: dict[tuple[str, str], dict[str, Any]] = {}
        self._cursor = 0
        self._observed = 0

    def publish(
        self,
        pair: tuple[str, str],
        *,
        window: int,
        node_id: str,
        partition: int,
        offset: int,
    ) -> None:
        # projection_cursor is a BIGSERIAL: every newly inserted window row
        # takes the next value, so a pair's newest window has its highest.
        self._cursor += 1
        self._observed += 1
        row = _row(pair, window=window, cursor=self._cursor, node_id=node_id)
        message = encode_snapshot_delta(
            self.cfg,
            op="upsert",
            row=row,
            source_event_id=f"hb-{node_id}-{offset}",
            source_topic=_SOURCE_TOPIC,
            source_partition=partition,
            source_offset=offset,
            observed_at=(_BASE + timedelta(milliseconds=self._observed)).isoformat(),
        )
        assert message is not None
        self.cache.apply_message(
            message.topic, message.key, message.value, list(message.headers)
        )
        self.newest[pair] = row


@pytest.fixture(scope="module")
def seeded() -> _Seeded:
    state = _Seeded()
    state.publish(_QUIET_PAIR, window=0, node_id="node-quiet", partition=1, offset=1)
    for window in range(_BUSY_WINDOWS):
        # One heartbeat per window carries every busy pair's delta.
        for pair in _BUSY_PAIRS:
            state.publish(
                pair, window=window, node_id="node-busy", partition=0, offset=window + 1
            )
    # No live consumer: mark bootstrap complete as a caught-up assignment would.
    state.cache._state[_TOPIC].bootstrap_complete = True
    return state


@contextmanager
def _client(seeded: _Seeded) -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_snapshot_cache] = lambda: seeded.cache
    app.dependency_overrides[get_topic_map] = lambda: {_TOPIC: seeded.cfg}
    try:
        yield TestClient(app, raise_server_exceptions=True)
    finally:
        app.dependency_overrides.clear()


def _pair(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row["consumer_group"]), str(row["topic"]))


def test_cache_holds_exactly_one_row_per_pair_and_it_is_the_newest_window(
    seeded: _Seeded,
) -> None:
    rows = seeded.cache.get_rows(_TOPIC, unbounded=True)
    served_pairs = [_pair(row) for row in rows]
    assert _QUIET_PAIR in served_pairs, (
        "a live consumer's current window was evicted by other pairs' history"
    )
    assert seeded.cache.row_count(_TOPIC) == len(_ALL_PAIRS), (
        f"cache holds {seeded.cache.row_count(_TOPIC)} rows for "
        f"{len(_ALL_PAIRS)} (consumer_group, topic) pairs"
    )
    assert sorted(served_pairs) == sorted(_ALL_PAIRS)
    for row in rows:
        newest = seeded.newest[_pair(row)]
        assert row["window_end"] == newest["window_end"]
        assert row["projection_cursor"] == newest["projection_cursor"]


@pytest.mark.parametrize("page_size", [None, 5])
def test_cursor_walk_reaches_every_pair_exactly_once_at_its_newest_window(
    seeded: _Seeded, page_size: int | None
) -> None:
    """``None`` is the contract limit (no ``limit`` parameter)."""
    walked: list[dict[str, Any]] = []
    since: str | None = None
    with _client(seeded) as client:
        # Bound: one page per pair at worst, plus the terminating page.
        for _ in range(len(_ALL_PAIRS) + 2):
            params: dict[str, Any] = {}
            if page_size is not None:
                params["limit"] = page_size
            if since is not None:
                params["since"] = since
            resp = client.get(f"/projection/{_TOPIC}", params=params)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            walked.extend(body["rows"])
            since = body["next_cursor"]
            if since is None:
                break
    assert since is None, (
        f"walk had not terminated after {len(_ALL_PAIRS) + 2} pages "
        f"({len(walked)} rows) for {len(_ALL_PAIRS)} pairs"
    )
    walked_pairs = [_pair(row) for row in walked]
    assert len(walked_pairs) == len(set(walked_pairs)), "a pair was served twice"
    assert set(walked_pairs) == _ALL_PAIRS
    for row in walked:
        newest = seeded.newest[_pair(row)]
        assert row["window_end"] == newest["window_end"]
        assert row["projection_cursor"] == newest["projection_cursor"]
