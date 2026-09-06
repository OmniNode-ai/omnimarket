# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15876 -- the row-cap eviction must be amortized, not once per record.

MEASURED LIVE on onex-dev (dev-system ``i-06169517a92b45f86``), 2026-09-06,
read-only in-pod broker offsets from
``omnimarket-projection-live-events-writer-7748cbfbb5-vr74h``:

===============================================  =========  ==========  =========
topic                                            log_start         hwm    records
===============================================  =========  ==========  =========
onex.snapshot.projection.consumer-flow.v1                0   1,293,082  1,293,082
onex.snapshot.projection.live-events.v1            179,145     268,911     89,766
onex.snapshot.projection.registration.v1            39,876      73,369     33,493
onex.snapshot.projection.delegation.* (x4)               0           2          2
onex.snapshot.projection.session.replay.v1               0           0          0
onex.snapshot.projection.tenant-credentials.v1           0           0          0
onex.snapshot.projection.work.events.v1                  0           0          0
===============================================  =========  ==========  =========

The ``/ready`` bus_backed map splits on exactly that ordering: the seven
topics holding 0 or 2 records are ``true`` within seconds, ``registration``
(33,493) flips within minutes, and ``consumer-flow`` (1,293,082) was still
``false`` on a pod 72 minutes into its replay while the container sat pegged
at 497m of its 500m CPU limit. The two topics the deploy gate reports as
never bootstrapping are simply the two largest backlogs -- the topics exist,
their producers are deployed, and their partitions are assigned. Nothing is
missing; the replay is too expensive.

WHY it is too expensive, and the correction to the prior measurement:
``apply_message`` caps a topic's cached rows at ``exposure.limit * 4`` and,
once over that cap, re-``sorted()`` the ENTIRE cache and rebuilt the dict on
EVERY subsequent record. ``consumer-flow`` puts ``window_start`` inside its
compaction key (recorded on OMN-17345; see
``node_projection_delegation/handlers/handler_delegation.py`` and
``node_projection_session_replay/contract.yaml``), so every record mints a
NEW key, the cap is exceeded almost immediately and never falls back, and the
sort fires ~1.29M times over 2,000 rows each.

The prior benchmark (ledger ``2026-09-06T14:48:00Z``) concluded "the apply
path is NOT the cost" from 60,724 records at ``limit=100``. That is the
``live-events`` shape: 21x fewer records over a 400-row cap. ``consumer-flow``
is ``limit=500`` -- a 2,000-row cap -- so it pays a 5x larger sort 21x more
often. The earlier number was right about the topic it measured and does not
carry to the topic that actually fails.

The fix is amortization, not a smaller cache: let the dict grow to
``max_rows * _ROW_CAP_SLACK_FACTOR`` and trim back to ``max_rows`` once, so a
trim costs ``O(max_rows log max_rows)`` per ``max_rows`` records instead of
per record. Eviction count drops from ``O(records)`` to
``O(records / max_rows)``.

GATE-DIRECTION LAW: nothing here touches ``/ready``. The cache holds MORE
rows between trims, never fewer, and ``get_rows`` still orders the full
retained set before truncating to the exposure's ``limit``, so a served
answer can only become more complete. ``test_serving_limit_is_unchanged``
and ``test_eviction_still_drops_the_oldest_rows`` pin that direction.

RED before the fix: ``_RECORDS`` distinct keys over a 2,000-row cap perform
``_RECORDS - 2000 == 18,000`` full-cache sorts, which no constant multiple of
``_RECORDS / max_rows`` can satisfy.
"""

from __future__ import annotations

import builtins
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.snapshot_cache import SnapshotCache

_TOPIC = "onex.snapshot.projection.consumer-flow.v1"
_SOURCE_TOPIC = "onex.evt.omnimarket.projection-consumer-flow-applied.v1"

# The live ``consumer-flow`` exposure declares ``limit: 500``
# (node_projection_consumer_flow/contract.yaml), so the row cap is 2,000.
_LIMIT = 500
_MAX_ROWS = _LIMIT * 4

# Enough records to exceed the cap by an order of magnitude while keeping the
# test fast. The live backlog is 1,293,082; the SCALING is what is asserted,
# and it is the scaling that does not fit a progress deadline.
_RECORDS = 20_000

# A trim must not happen more than a small constant number of times per
# ``max_rows`` records consumed. Ten trims is what perfect amortization gives
# for this backlog; the bound leaves generous slack and is still ~1,700x
# below the pre-fix count of 18,000.
_MAX_EVICTION_SORTS = 32


def _make_cache(limit: int = _LIMIT) -> SnapshotCache:
    exposure = ProjectionTableConfig(
        topic=_TOPIC,
        table="consumer_flow_windows",
        columns=(
            "consumer_group",
            "topic",
            "window_start",
            "window_end",
            "node_id",
            "messages_in",
            "flow_state",
            "evaluated_at",
        ),
        order_by="window_end DESC",
        freshness_column="window_end",
        limit=limit,
        bus_backed=True,
        key_columns=("consumer_group", "topic", "window_start"),
    )
    return SnapshotCache(
        {_TOPIC: exposure},
        bootstrap_servers="unused:9092",
        group_id="test-eviction-amortization-group",
    )


_EPOCH = datetime(2026, 9, 6, tzinfo=UTC)


def _observed_at(index: int) -> str:
    return (_EPOCH + timedelta(seconds=index)).isoformat()


def _delta_bytes(index: int) -> bytes:
    """One snapshot delta whose key is UNIQUE per record.

    That is the live ``consumer-flow`` shape: ``window_start`` sits inside the
    compaction key, so a replay of N records installs N distinct cache keys
    and the row cap is never relieved by an in-place overwrite.
    """
    payload = {
        "topic": _TOPIC,
        "key": ["group-a", "onex.evt.some.topic.v1", f"2026-09-06T{index:07d}"],
        "op": "upsert",
        "row": {
            "consumer_group": "group-a",
            "window_start": index,
            "messages_in": index,
        },
        # STRICTLY monotonic across the whole replay, so "oldest by
        # observed_at" is exactly "lowest index" and the eviction-direction
        # assertion below is a statement about the algorithm rather than about
        # a timestamp helper that happens to wrap.
        "observed_at": _observed_at(index),
        "source_event_id": f"flow-{index}",
        "source_topic": _SOURCE_TOPIC,
        "source_partition": 0,
        "source_offset": index,
        "projection_version": "projection_snapshot.v1",
    }
    return json.dumps(payload).encode("utf-8")


def _replay(cache: SnapshotCache, records: int) -> None:
    for index in range(records):
        cache.apply_message(_TOPIC, None, _delta_bytes(index), [])


@pytest.fixture
def counted_sorts(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count only the FULL-CACHE sorts the eviction path performs.

    ``sorted`` is patched in the module's own namespace rather than a counter
    being added to production code, so the assertion is about the shipped
    algorithm and not about an instrumentation hook that could drift from it.
    """
    calls: list[int] = []
    real_sorted = builtins.sorted

    def counting_sorted(iterable: Any, **kwargs: Any) -> list[Any]:
        materialized = list(iterable)
        calls.append(len(materialized))
        return real_sorted(materialized, **kwargs)

    monkeypatch.setattr(
        "omnimarket.projection.snapshot_cache.sorted", counting_sorted, raising=False
    )
    return calls


@pytest.mark.unit
def test_eviction_is_amortized_not_once_per_record(counted_sorts: list[int]) -> None:
    """The whole defect: one full-cache sort per record once over the cap."""
    cache = _make_cache()
    _replay(cache, _RECORDS)

    assert len(counted_sorts) <= _MAX_EVICTION_SORTS, (
        f"{len(counted_sorts)} full-cache eviction sorts for {_RECORDS} records "
        f"over a {_MAX_ROWS}-row cap. The pre-fix algorithm sorts once per "
        f"record past the cap ({_RECORDS - _MAX_ROWS}); at the live "
        f"consumer-flow backlog of 1,293,082 records that is ~1.29M sorts of "
        f"2,000 rows each, on a pod limited to 500m CPU."
    )


@pytest.mark.unit
def test_retained_rows_stay_bounded(counted_sorts: list[int]) -> None:
    """Amortizing must not turn the cap into an unbounded cache."""
    cache = _make_cache()
    _replay(cache, _RECORDS)

    assert cache.row_count(_TOPIC) <= _MAX_ROWS * 2, (
        "the amortized trim must keep the cache within a constant factor of "
        "the declared row cap; memory is bounded, only the trim FREQUENCY "
        "changed"
    )


@pytest.mark.unit
def test_serving_limit_is_unchanged() -> None:
    """GATE-DIRECTION: the served page is still exactly ``exposure.limit``."""
    cache = _make_cache()
    _replay(cache, _RECORDS)

    rows = cache.get_rows(_TOPIC)
    assert len(rows) == _LIMIT


@pytest.mark.unit
def test_eviction_still_drops_the_oldest_rows() -> None:
    """GATE-DIRECTION: retention direction is unchanged -- oldest out first.

    ``observed_at`` is monotonic in ``window_start`` here, so every retained
    row must come from the tail of the replay. A served answer may become more
    complete (more candidates survive between trims); it must never lose a
    NEWER row in favour of an older one.
    """
    cache = _make_cache()
    _replay(cache, _RECORDS)

    retained = [
        int(row["window_start"]) for row in cache.get_rows(_TOPIC, limit=_MAX_ROWS * 2)
    ]
    assert retained, "positive control: the cache must not be empty"
    assert min(retained) >= _RECORDS - (_MAX_ROWS * 2), (
        "an evicted row is older than every retained row; the oldest-first "
        "eviction direction must survive amortization"
    )


@pytest.mark.unit
def test_counting_fixture_sees_the_pre_fix_shape(counted_sorts: list[int]) -> None:
    """Positive control for the counter itself.

    A replay that never exceeds the cap performs ZERO eviction sorts, and a
    counter that reports zero for BOTH cases would prove nothing. Driving
    ``_MAX_ROWS + 1`` distinct keys must trip the eviction path at least once,
    so a zero in the test above is a real measurement and not a dead hook.
    """
    cache = _make_cache(limit=1)  # cap of 4 rows -- trips immediately
    _replay(cache, 64)
    assert counted_sorts, (
        "the eviction path was never entered; the sorted() patch is not on "
        "the code path this test claims to measure"
    )
