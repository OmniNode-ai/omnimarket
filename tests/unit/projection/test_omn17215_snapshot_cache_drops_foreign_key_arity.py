# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17215 AC1: a replay of legacy-keyed records must not be served.

The consumer-flow exposure now keys its snapshot on ``(consumer_group, topic)``.
Its snapshot topic is not compacted -- it retains records by time -- so a fresh
cache's bootstrap replay still delivers every record published under the old
``consumer_group|topic|window_start`` key until retention ages it out. The cache
stores a row under the delta's OWN key, so a 3-part legacy row is never replaced
by the pair's 2-part row: without a guard the cache serves both, legacy window
history beside the current window, for the whole retention period.

Everything runs through the shipped path: the exposure is parsed from the real
``contract.yaml``, every delta (legacy ones included) is encoded by the real
``encode_snapshot_delta``, and every delta is applied by the real
``SnapshotCache.apply_message``. A legacy record is encoded against the same
exposure with the previous ``key_columns``, which is byte-for-byte what the
writer published before the key changed.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

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
_LEGACY_KEY_COLUMNS = ("consumer_group", "topic", "window_start")
_CACHE_LOGGER = "omnimarket.projection.snapshot_cache"

_BASE = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
_WINDOW = timedelta(seconds=30)
_PAIR = ("group-a", "topic-a")


def _cfg() -> ProjectionTableConfig:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    exposures = load_projection_exposures_from_contract(
        contract, "projection_consumer_flow", _CONTRACT_PATH
    )
    assert len(exposures) == 1, exposures
    cfg = exposures[0]
    assert cfg.topic == _TOPIC
    assert cfg.bus_backed
    assert cfg.key_columns == ("consumer_group", "topic"), cfg.key_columns
    return cfg


def _legacy_cfg(cfg: ProjectionTableConfig) -> ProjectionTableConfig:
    return cfg.model_copy(update={"key_columns": _LEGACY_KEY_COLUMNS})


def _cache(cfg: ProjectionTableConfig) -> SnapshotCache:
    cache = SnapshotCache(
        {_TOPIC: cfg},
        bootstrap_servers="unused:9092",
        group_id="test-omn17215-snapshot-cache-foreign-key-arity",
    )
    cache._state[_TOPIC].bootstrap_complete = True
    return cache


def _row(pair: tuple[str, str], *, window: int, messages_in: int) -> dict[str, Any]:
    window_start = _BASE + _WINDOW * window
    window_end = window_start + _WINDOW
    return {
        "projection_cursor": window + 1,
        "consumer_group": pair[0],
        "topic": pair[1],
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "node_id": "node-1",
        "ingest_sequence": window,
        "messages_in": messages_in,
        "messages_out": messages_in,
        "messages_dlq": 0,
        "handler_errors": 0,
        "upstream_produced": None,
        "upstream_evidence": "NONE",
        "flow_state": "FLOWING",
        "evaluated_at": window_end.isoformat(),
    }


def _upsert(
    cache: SnapshotCache,
    exposure: ProjectionTableConfig,
    row: dict[str, Any],
    *,
    offset: int,
) -> None:
    message = encode_snapshot_delta(
        exposure,
        op="upsert",
        row=row,
        source_event_id=f"hb-{offset}",
        source_topic=_SOURCE_TOPIC,
        source_partition=0,
        source_offset=offset,
        observed_at=(_BASE + timedelta(milliseconds=offset)).isoformat(),
    )
    assert message is not None
    cache.apply_message(
        message.topic, message.key, message.value, list(message.headers)
    )


def _tombstone(
    cache: SnapshotCache,
    exposure: ProjectionTableConfig,
    key: dict[str, Any],
    *,
    offset: int,
) -> None:
    message = encode_snapshot_delta(
        exposure,
        op="delete",
        row=None,
        key=key,
        source_event_id=f"hb-{offset}",
        source_topic=_SOURCE_TOPIC,
        source_partition=0,
        source_offset=offset,
        observed_at=(_BASE + timedelta(milliseconds=offset)).isoformat(),
    )
    assert message is not None
    assert message.value is None
    cache.apply_message(
        message.topic, message.key, message.value, list(message.headers)
    )


def test_legacy_three_part_upsert_is_neither_cached_nor_served() -> None:
    cfg = _cfg()
    cache = _cache(cfg)

    _upsert(cache, _legacy_cfg(cfg), _row(_PAIR, window=0, messages_in=7), offset=1)

    assert cache.row_count(_TOPIC) == 0, list(cache._state[_TOPIC].rows)
    assert cache.get_rows(_TOPIC, unbounded=True) == []
    assert cache.latest_event_at(_TOPIC) is None


def test_legacy_three_part_tombstone_does_not_delete_the_pairs_row(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = _cfg()
    cache = _cache(cfg)
    row = _row(_PAIR, window=1, messages_in=5)
    _upsert(cache, cfg, row, offset=1)
    assert cache.row_count(_TOPIC) == 1

    legacy_key = {
        "consumer_group": _PAIR[0],
        "topic": _PAIR[1],
        "window_start": row["window_start"],
    }
    with caplog.at_level(logging.DEBUG, logger=_CACHE_LOGGER):
        _tombstone(cache, _legacy_cfg(cfg), legacy_key, offset=2)

    assert cache.get_rows(_TOPIC, unbounded=True) == [row]
    # The row surviving is necessary but not sufficient: a 3-part tuple can
    # never equal a 2-part one, so a pop would miss either way. The drop itself
    # is what the guard owns, and it records it.
    dropped = [r for r in caplog.records if "dropped a 3-part key" in r.getMessage()]
    assert len(dropped) == 1, [r.getMessage() for r in caplog.records]


def test_current_two_part_delta_still_applies_replaces_and_deletes() -> None:
    cfg = _cfg()
    cache = _cache(cfg)

    first = _row(_PAIR, window=1, messages_in=5)
    _upsert(cache, cfg, first, offset=1)
    assert cache.get_rows(_TOPIC, unbounded=True) == [first]

    second = _row(_PAIR, window=2, messages_in=9)
    _upsert(cache, cfg, second, offset=2)
    assert cache.row_count(_TOPIC) == 1
    assert cache.get_rows(_TOPIC, unbounded=True) == [second]

    _tombstone(cache, cfg, {"consumer_group": _PAIR[0], "topic": _PAIR[1]}, offset=3)
    assert cache.row_count(_TOPIC) == 0


def test_replay_of_legacy_history_then_current_rows_serves_one_row_per_pair() -> None:
    """A bootstrap replay reads the retained topic in offset order: the legacy
    records published before the key change, then the current ones."""
    cfg = _cfg()
    legacy = _legacy_cfg(cfg)
    cache = _cache(cfg)
    pairs = tuple((f"group-{g}", f"topic-{t}") for g in range(4) for t in range(3))
    legacy_windows = 200  # 2,400 legacy records, past the 2,000-row retention cap
    offset = 0

    for window in range(legacy_windows):
        for pair in pairs:
            offset += 1
            _upsert(
                cache, legacy, _row(pair, window=window, messages_in=1), offset=offset
            )

    newest: dict[tuple[str, str], dict[str, Any]] = {}
    for window in range(legacy_windows, legacy_windows + 3):
        for pair in pairs:
            offset += 1
            row = _row(pair, window=window, messages_in=window)
            _upsert(cache, cfg, row, offset=offset)
            newest[pair] = row

    keys = list(cache._state[_TOPIC].rows)
    assert all(len(key) == 2 for key in keys), [k for k in keys if len(k) != 2][:3]
    assert sorted(keys) == sorted(pairs)
    served = cache.get_rows(_TOPIC, unbounded=True)
    assert len(served) == len(pairs), f"{len(served)} rows for {len(pairs)} pairs"
    for row in served:
        assert row == newest[(row["consumer_group"], row["topic"])]
