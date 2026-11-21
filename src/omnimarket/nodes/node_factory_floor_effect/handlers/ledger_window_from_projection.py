# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory floor's ledger window read from the work ledger projection, one UTC day at a time through the source read cache (OMN-19998, ledger cutover Wave 0).

The whole window in one projection read is far too slow for the step (a 96-hour window measured 102 s on the dev bus, 2026-11-14), so the window is read a day at a time: every UTC day from the window's first midnight through yesterday is closed, read once and kept in the source read cache under its date (a closed day never changes), and only today is read every run. Today is read first, before any closed day is read or stored: its freshness is the window's, so a today that is not ok is refused with the reader, the day, the status and the node's reason named -- nothing is stored in its place, and a closed day that would have been stored is not. A closed day is kept in the cache only when the projection provably holds it: today's answer holds a row stamped at or after the day's end, the day's own answer is complete (it returned every row it matched), and -- for the last closed day -- ``settle_seconds`` have passed since midnight UTC (the projection lags the ledger, so a day just ended may not hold its last rows yet). A day that is not kept is still read and still counts in the window; it is simply read again next run. The read holds to ``deadline_seconds`` from its first read (the caller's clock): a closed day whose turn comes after the deadline is not read at all, its date goes to the window's unread days and its rows are absent from the window. Every day that was read reports how complete its answer was, and the window's gaps name each answer that returned fewer rows than it matched and a today that did not read at exact parity. Each day is one fresh bus read (its own session): a rows query whose ``since`` is the day's start and whose ``until`` is the next day's start, open-ended (no ``until``) for today. A status that is not ok is refused with the reader, the day, the status and the node's reason named -- never an empty window and never a file in its place; an answer that reports a file read is refused too. The node's lines are bare row lines (no ``L<n>`` prefix) and only the rows stamped inside the day are kept, so a node that answers the same lines for every window cannot duplicate rows; the rows stamped before the window's exact start are dropped and the per-lane reduction runs once over what is left, in day order, today last. The cache-missing closed days are read at most ``read_workers`` at a time (one at a time when the overlay names no count) and each kept day's rows are kept for ``cache_ttl_seconds``; with no cache the closed days are all misses, read the same way, and nothing is kept. This module opens nothing: no subprocess, no ``open()``, no ``Path`` read; it writes the reader's one ``READER`` summary line per read to stderr.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from omnimarket.models.model_factory_floor_input import ModelFloorLedgerRow
from omnimarket.models.model_source_read_cache import ModelSourceCacheKey
from omnimarket.models.model_work_ledger_reader import (
    EnumReaderParity,
    EnumReaderQuery,
    EnumReaderStatus,
    ModelReaderRequest,
    ModelReaderResult,
)
from omnimarket.nodes.node_factory_floor_effect.handlers.ledger_window_by_archive import (
    ROWS_FORMAT,
    decode_rows,
    encode_rows,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_ledger_projection_window import (
    ModelProjectionWindow,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    SourceRefusedError,
    parse_projection_lines,
    reduce_ledger_rows,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader import (
    CachedSourceReader,
)

ProjectionRead = Callable[[ModelReaderRequest], ModelReaderResult]
ProjectionReadFactory = Callable[[], ProjectionRead]


@dataclass(frozen=True)
class _DayRead:
    """One day's read: its rows, and the answer's own count of the rows it matched and the lines it returned, at what parity."""

    rows: tuple[ModelFloorLedgerRow, ...]
    matched: int
    lines_read: int
    parity: EnumReaderParity


def _read_day(
    *,
    reader: str,
    read_factory: ProjectionReadFactory,
    day_start: datetime,
    day_end: datetime | None,
    read_parity: bool = False,
) -> _DayRead:
    """One day's rows: one fresh read (its own bus session), only the rows stamped inside the day.

    The node's watermark of a rows answer is the window's newest row, so a closed day always answers ``stale`` (measured on the dev bus
    2026-11-14: 2026-11-13 and 2026-11-12, both ``watermark-older-than-bound`` with every row present). A closed day is therefore read
    for ``ok`` or ``stale``; today, whose newest row is the projection's own lag, is read for ``ok`` only.

    ``day_end`` is the next day's start for a closed day and ``None`` for today (the read's open end). A status that is not ok is a
    ``SourceRefusedError`` naming the reader, the day, the status and the node's reason -- never an empty window; an answer that reports a
    file read is refused too. ``read_parity`` asks the reader to measure the answer's parity (its rows against the ledger's own record):
    today's read asks, a closed day's does not.
    """
    read = read_factory()
    result = read(
        ModelReaderRequest(
            reader=reader,
            query=EnumReaderQuery.ROWS,
            since=day_start,
            until=day_end,
            read_parity=read_parity,
        )
    )
    # The reader's own summary line for every read, so the step's stderr says file_read=no for each day it read (OMN-24490 AC4).
    sys.stderr.write(result.summary() + "\n")
    day = f"{day_start:%Y-%m-%d}"
    accepted = (
        {EnumReaderStatus.OK}
        if day_end is None
        else {EnumReaderStatus.OK, EnumReaderStatus.STALE}
    )
    if result.status not in accepted:
        raise SourceRefusedError(
            f"work ledger reader {reader} answered {result.status.value} for {day} "
            f"({result.reason}): repair the work ledger reader overlay or supply ledger_rows_file"
        )
    if result.file_read:
        raise SourceRefusedError(
            f"the projection read for {reader} on {day} reported a file read"
        )
    return _DayRead(
        rows=tuple(
            row
            for row in parse_projection_lines(result.lines)
            if row.ts >= day_start and (day_end is None or row.ts < day_end)
        ),
        matched=result.matched,
        lines_read=len(result.lines),
        parity=result.parity,
    )


def read_ledger_window_from_projection(
    *,
    reader: str,
    read_factory: ProjectionReadFactory,
    window_days: int,
    now: datetime,
    kinds: frozenset[str],
    cache: CachedSourceReader | None,
    cache_ttl_seconds: int,
    read_workers: int | None,
    settle_seconds: int,
    deadline_seconds: int,
    clock: Callable[[], float] = time.monotonic,
) -> ModelProjectionWindow:
    """The window's rows from the projection, one UTC day at a time: today first, then the closed days (each kept day kept in ``cache``
    for ``cache_ttl_seconds``, the kept days at most ``read_workers`` at a time), reduced once over ``kinds``.

    Today is read before any closed day: a today that is not ok refuses the window with the reader, the day, the status and the node's
    reason named -- nothing is stored in its place. A closed day is kept only when the projection provably holds it (a row stamped at or
    after the day's end in today's answer, a complete answer, and the settle seconds past midnight for the last closed day); a day that
    is not kept is still read and still counts in the window, simply read again next run. A closed day whose turn comes after
    ``deadline_seconds`` is not read: its date goes to the window's unread days. The window's gaps name every read that returned fewer
    rows than it matched, in day order, and a today that did not read at exact parity last.
    """
    now_utc = now.astimezone(UTC)
    since_exact = now_utc - timedelta(days=window_days)
    today = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    first = since_exact.replace(hour=0, minute=0, second=0, microsecond=0)
    closed = [first + timedelta(days=n) for n in range((today - first).days)]
    started = clock()

    def cache_key(day: datetime) -> ModelSourceCacheKey:
        return ModelSourceCacheKey(
            source_id=f"ledger-projection:{reader}:{day:%Y-%m-%d}",
            params_digest=ModelSourceCacheKey.digest({"rows_format": ROWS_FORMAT}),
            revision="closed-day",
        )

    today_read = _read_day(
        reader=reader,
        read_factory=read_factory,
        day_start=today,
        day_end=None,
        read_parity=True,
    )
    gaps: list[tuple[str, str]] = []
    if today_read.matched != today_read.lines_read:
        gaps.append(
            (
                f"{today:%Y-%m-%d}",
                f"{today:%Y-%m-%d}: {today_read.lines_read} of {today_read.matched} rows returned",
            )
        )

    projected_through = max((row.ts for row in today_read.rows), default=None)
    unsettled = (now_utc - today).total_seconds() < settle_seconds
    last_closed = closed[-1] if closed else None

    def storable(day: datetime, read: _DayRead) -> bool:
        """A closed day is kept only when the projection provably holds it: today's answer holds a row stamped at or after the
        day's end, the day's own answer is complete, and (for the last closed day) the settle seconds have passed since midnight."""
        if projected_through is None or projected_through < day + timedelta(days=1):
            return False
        if unsettled and day == last_closed:
            return False
        return read.matched == read.lines_read

    hits: dict[datetime, tuple[ModelFloorLedgerRow, ...]] = {}
    unread: list[datetime] = []
    pending: list[
        tuple[datetime, ModelSourceCacheKey, Callable[[], _DayRead | None]]
    ] = []
    for day in closed:
        if cache is not None:
            cached = cache.lookup(cache_key(day), decode_rows)
            if cached is not None:
                hits[day] = cached
                continue

        def read_day(day: datetime = day) -> _DayRead | None:
            # The deadline is judged when the read would start, not when it is queued: a day that waited behind the others
            # past the deadline is never read.
            if clock() - started >= deadline_seconds:
                return None
            return _read_day(
                reader=reader,
                read_factory=read_factory,
                day_start=day,
                day_end=day + timedelta(days=1),
            )

        pending.append((day, cache_key(day), read_day))

    reads: dict[datetime, _DayRead] = {}
    if pending:
        with ThreadPoolExecutor(max_workers=read_workers or 1) as pool:
            futures = {pool.submit(fetch): (day, key) for day, key, fetch in pending}
            try:
                for future in as_completed(futures):
                    day, key = futures[future]
                    read = future.result()
                    if read is None:
                        unread.append(day)
                        continue
                    reads[day] = read
                    if read.matched != read.lines_read:
                        gaps.append(
                            (
                                f"{day:%Y-%m-%d}",
                                f"{day:%Y-%m-%d}: {read.lines_read} of {read.matched} rows returned",
                            )
                        )
                    if cache is not None and storable(day, read):
                        cache.store(
                            key, read.rows, encode_rows, ttl_seconds=cache_ttl_seconds
                        )
            except BaseException:
                for pending_future in futures:
                    pending_future.cancel()
                raise
    merged: list[ModelFloorLedgerRow] = []
    for day in closed:
        if day in hits:
            merged.extend(hits[day])
        elif day in reads:
            merged.extend(reads[day].rows)
    merged.extend(today_read.rows)
    ordered_gaps = [sentence for _day, sentence in sorted(gaps)]
    if today_read.parity is not EnumReaderParity.EXACT:
        ordered_gaps.append(f"read parity {today_read.parity.value}")
    return ModelProjectionWindow(
        rows=reduce_ledger_rows(
            tuple(row for row in merged if row.ts >= since_exact), kinds
        ),
        unread_days=tuple(f"{day:%Y-%m-%d}" for day in sorted(unread)),
        gaps=tuple(ordered_gaps),
    )
