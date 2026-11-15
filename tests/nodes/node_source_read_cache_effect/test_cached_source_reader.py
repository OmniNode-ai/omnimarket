# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reader the board steps call: a source is read only when the cache has no current answer for its revision (OMN-19998).

Golden: the first read goes to the source and is stored; the second read of the same revision is a hit and the source is NOT read
(the counting fetch is the failing control of "a second cycle with unchanged sources reads from the cache"); a new revision is a miss
and reads the source again. Error chain: an unreachable backend, a payload that cannot be decoded, a reader with no handler and a
cached value the caller no longer accepts each end in the source's answer, never in a failure.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest

from omnimarket.models.model_source_read_cache import (
    EnumSourceCacheOutcome,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader import (
    CachedSourceReader,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.handler_source_read_cache import (
    HandlerSourceReadCache,
)
from tests.nodes.node_source_read_cache_effect.support import (
    PREFIX,
    TTL_BACKSTOP,
    Clock,
    DownBackend,
    key,
    memory_reader,
)

pytestmark = pytest.mark.unit


class Source:
    """A source that counts how often it is read."""

    def __init__(self) -> None:
        self.reads = 0

    def fetch(self) -> list[int]:
        self.reads += 1
        return [1, 2, 3]


def _read(
    reader: CachedSourceReader, source: Source, revision: str = "r1", **kwargs: Any
) -> tuple[list[int], EnumSourceCacheOutcome]:
    return reader.read(
        key(revision), source.fetch, json.dumps, lambda text: json.loads(text), **kwargs
    )


def test_golden_second_read_of_an_unchanged_source_comes_from_the_cache() -> None:
    reader, _ = memory_reader()
    source = Source()
    first, first_outcome = _read(reader, source)
    second, second_outcome = _read(reader, source)
    assert (first_outcome, second_outcome) == (
        EnumSourceCacheOutcome.MISS,
        EnumSourceCacheOutcome.HIT,
    )
    assert first == second == [1, 2, 3]
    assert source.reads == 1
    assert (reader.tally.hits, reader.tally.misses, reader.tally.stores) == (1, 1, 1)
    assert reader.tally.source_reads == 1


def test_a_changed_source_is_a_miss_and_is_read() -> None:
    reader, _ = memory_reader()
    source = Source()
    _read(reader, source, "r1")
    _read(reader, source, "r2")
    assert source.reads == 2


def test_a_value_the_caller_no_longer_accepts_is_read_again() -> None:
    reader, _ = memory_reader()
    source = Source()
    _read(reader, source)
    _read(reader, source, usable=lambda _value: False)
    assert source.reads == 2


def test_store_if_false_stores_nothing() -> None:
    reader, backend = memory_reader()
    source = Source()
    _read(reader, source, store_if=lambda _value: False)
    _read(reader, source, store_if=lambda _value: False)
    assert source.reads == 2
    assert backend.keys == []


def test_the_overlay_ttl_backstop_ends_a_hit() -> None:
    clock = Clock()
    reader, _ = memory_reader(clock)
    source = Source()
    _read(reader, source)
    clock.now += TTL_BACKSTOP + 1
    _read(reader, source)
    assert source.reads == 2


def test_error_chain_a_payload_that_does_not_decode_falls_through_to_the_source() -> (
    None
):
    reader, backend = memory_reader()
    backend.put(key().render(PREFIX), "not json", TTL_BACKSTOP)
    source = Source()
    value, outcome = _read(reader, source)
    assert (value, outcome) == ([1, 2, 3], EnumSourceCacheOutcome.MISS)
    assert source.reads == 1


def test_error_chain_an_unreachable_backend_still_returns_the_source_answer() -> None:
    reader = CachedSourceReader(
        HandlerSourceReadCache(DownBackend(), PREFIX, TTL_BACKSTOP)
    )
    source = Source()
    value, outcome = _read(reader, source)
    assert (value, outcome) == ([1, 2, 3], EnumSourceCacheOutcome.UNAVAILABLE)
    assert reader.tally.unavailable == 1
    assert reader.tally.stores == 0


def test_error_chain_a_reader_with_no_handler_reads_the_source_every_time() -> None:
    reader = CachedSourceReader(
        None, disabled_reason="overlay: NodeOverlayNotFoundError"
    )
    source = Source()
    _read(reader, source)
    _read(reader, source)
    assert source.reads == 2
    assert reader.tally.bypassed == 2
    assert reader.tally.disabled_reason.startswith("overlay:")


def test_a_failing_source_stores_nothing_and_propagates() -> None:
    reader, backend = memory_reader()

    def boom() -> list[int]:
        raise OSError("source down")

    with pytest.raises(OSError, match="source down"):
        reader.read(key(), boom, json.dumps, json.loads)
    assert backend.keys == []


def test_read_many_golden_values_in_order_and_a_second_call_is_all_hits() -> None:
    reader, _ = memory_reader()
    reads = [(key(str(n), f"src{n}"), (lambda n=n: [n])) for n in range(3)]
    assert reader.read_many(reads, json.dumps, json.loads, max_workers=2) == [
        [0],
        [1],
        [2],
    ]
    assert (reader.tally.misses, reader.tally.stores) == (3, 3)
    assert reader.read_many(reads, json.dumps, json.loads, max_workers=2) == [
        [0],
        [1],
        [2],
    ]
    assert (reader.tally.hits, reader.tally.misses, reader.tally.stores) == (3, 3, 3)
    assert reader.tally.source_reads == 3


def test_read_many_the_misses_are_fetched_at_the_same_time() -> None:
    reader, _ = memory_reader()
    barrier = threading.Barrier(3)

    def wait(n: int) -> int:
        barrier.wait(timeout=5)
        return n

    reads = [(key(str(n), f"src{n}"), (lambda n=n: wait(n))) for n in range(3)]
    assert reader.read_many(reads, json.dumps, json.loads, max_workers=3) == [0, 1, 2]


def test_read_many_never_runs_more_than_max_workers_fetches_at_once() -> None:
    reader, _ = memory_reader()
    lock = threading.Lock()
    running = 0
    peak = 0

    def count(n: int) -> int:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with lock:
            running -= 1
        return n

    reads = [(key(str(n), f"src{n}"), (lambda n=n: count(n))) for n in range(6)]
    assert reader.read_many(reads, json.dumps, json.loads, max_workers=2) == list(
        range(6)
    )
    assert 1 < peak <= 2


def test_read_many_the_ttl_given_is_kept_past_the_backstop() -> None:
    clock = Clock()
    reader, _ = memory_reader(clock)
    source = Source()
    reads = [(key("r1"), source.fetch)]
    reader.read_many(reads, json.dumps, json.loads, max_workers=1, ttl_seconds=5000)
    clock.now += TTL_BACKSTOP + 1
    assert reader.read_many(reads, json.dumps, json.loads, max_workers=1) == [[1, 2, 3]]
    assert source.reads == 1


def test_read_many_without_a_ttl_the_backstop_ends_the_entry() -> None:
    clock = Clock()
    reader, _ = memory_reader(clock)
    source = Source()
    reads = [(key("r1"), source.fetch)]
    reader.read_many(reads, json.dumps, json.loads, max_workers=1)
    clock.now += TTL_BACKSTOP + 1
    reader.read_many(reads, json.dumps, json.loads, max_workers=1)
    assert source.reads == 2


def test_read_many_a_failing_fetch_propagates_and_a_failing_batch_stores_nothing() -> (
    None
):
    reader, backend = memory_reader()

    def boom() -> list[int]:
        raise OSError("source down")

    reads = [(key("r1", "src1"), boom), (key("r2", "src2"), boom)]
    with pytest.raises(OSError, match="source down"):
        reader.read_many(reads, json.dumps, json.loads, max_workers=2)
    assert backend.keys == []


def test_read_many_a_batch_cut_short_keeps_what_it_read() -> None:
    reader, backend = memory_reader()
    source = Source()

    def boom() -> list[int]:
        raise OSError("source down")

    reads = [(key("r1", "src1"), source.fetch), (key("r2", "src2"), boom)]
    with pytest.raises(OSError, match="source down"):
        reader.read_many(reads, json.dumps, json.loads, max_workers=1)
    assert len(backend.keys) == 1
    assert reader.tally.stores == 1
    # the next cycle starts from there: the first read is a hit
    assert reader.read_many(reads[:1], json.dumps, json.loads, max_workers=1) == [
        [1, 2, 3]
    ]
    assert source.reads == 1


def test_read_many_an_unreachable_backend_still_returns_every_value() -> None:
    reader = CachedSourceReader(
        HandlerSourceReadCache(DownBackend(), PREFIX, TTL_BACKSTOP)
    )
    sources = [Source(), Source()]
    reads = [(key(str(n), f"src{n}"), source.fetch) for n, source in enumerate(sources)]
    assert reader.read_many(reads, json.dumps, json.loads, max_workers=2) == [
        [1, 2, 3],
        [1, 2, 3],
    ]
    assert reader.tally.unavailable == 2
    assert reader.tally.stores == 0


def test_read_many_a_reader_with_no_handler_bypasses_every_read() -> None:
    reader = CachedSourceReader(
        None, disabled_reason="overlay: NodeOverlayNotFoundError"
    )
    source = Source()
    reads = [(key(str(n)), source.fetch) for n in range(2)]
    assert reader.read_many(reads, json.dumps, json.loads, max_workers=2) == [
        [1, 2, 3],
        [1, 2, 3],
    ]
    assert reader.tally.bypassed == 2
    assert reader.tally.source_reads == 2


def test_read_many_max_workers_below_one_is_refused() -> None:
    reader, _ = memory_reader()
    with pytest.raises(ValueError, match="max_workers"):
        reader.read_many(
            [(key(), Source().fetch)], json.dumps, json.loads, max_workers=0
        )


def test_the_tally_line_is_json() -> None:
    reader, _ = memory_reader()
    _read(reader, Source())
    doc = json.loads(reader.tally.as_json_line())
    assert doc["source_cache"]["misses"] == 1
    assert doc["source_cache"]["backend"] == "memory"
