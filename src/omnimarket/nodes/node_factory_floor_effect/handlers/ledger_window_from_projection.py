# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory floor's ledger window read from the work ledger projection, one UTC day at a time through the source read cache (OMN-19998, ledger cutover Wave 0).

The whole window in one projection read is far too slow for the step (a 96-hour window measured 102 s on the dev bus, 2026-11-14), so the window is read a day at a time: every UTC day from the window's first midnight through yesterday is closed, read once and kept in the source read cache under its date (a closed day never changes), and only today is read every run. Each day is one fresh bus read (its own session): a rows query whose ``since`` is the day's start and whose ``until`` is the next day's start, open-ended (no ``until``) for today. A status that is not ok is refused with the reader, the day, the status and the node's reason named -- never an empty window and never a file in its place; an answer that reports a file read is refused too. The node's lines are bare row lines (no ``L<n>`` prefix) and only the rows stamped inside the day are kept, so a node that answers the same lines for every window cannot duplicate rows; the rows stamped before the window's exact start are dropped and the per-lane reduction runs once over what is left, in day order, today last. The cache-missing closed days are read at most ``read_workers`` at a time (one at a time when the overlay names no count) and each is kept for ``cache_ttl_seconds``; with no cache the closed days are read one after another and nothing is kept. This module opens nothing: no subprocess, no ``open()``, no ``Path`` read.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from omnimarket.models.model_factory_floor_input import ModelFloorLedgerRow
from omnimarket.models.model_source_read_cache import ModelSourceCacheKey
from omnimarket.models.model_work_ledger_reader import (
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


def _read_day(
    *,
    reader: str,
    read_factory: ProjectionReadFactory,
    day_start: datetime,
    day_end: datetime | None,
) -> tuple[ModelFloorLedgerRow, ...]:
    """One day's rows: one fresh read (its own bus session), only the rows stamped inside the day.

    The node's watermark of a rows answer is the window's newest row, so a closed day always answers ``stale`` (measured on the dev bus
    2026-11-14: 2026-11-13 and 2026-11-12, both ``watermark-older-than-bound`` with every row present). A closed day is therefore read
    for ``ok`` or ``stale``; today, whose newest row is the projection's own lag, is read for ``ok`` only.

    ``day_end`` is the next day's start for a closed day and ``None`` for today (the read's open
    end). A status that is not ok is a ``SourceRefusedError`` naming the reader, the day, the
    status and the node's reason -- never an empty window; an answer that reports a file read is
    refused too.
    """
    read = read_factory()
    result = read(
        ModelReaderRequest(
            reader=reader,
            query=EnumReaderQuery.ROWS,
            since=day_start,
            until=day_end,
        )
    )
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
    return tuple(
        row
        for row in parse_projection_lines(result.lines)
        if row.ts >= day_start and (day_end is None or row.ts < day_end)
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
) -> tuple[ModelFloorLedgerRow, ...]:
    """The window's rows from the projection, one UTC day at a time: the closed days through ``cache``
    (each kept for ``cache_ttl_seconds``, the cache-missing ones at most ``read_workers`` at a time),
    today every run, reduced once over ``kinds``. Yesterday is read live, not kept, until ``settle_seconds`` after midnight: the
    projection lags the ledger, so a day just ended may not hold its last rows yet.

    Each day is one fresh read from ``read_factory``; a day whose answer is not ok is refused with
    the day named -- never an empty window, never a file in its place.
    """
    now_utc = now.astimezone(UTC)
    since_exact = now_utc - timedelta(days=window_days)
    today = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    first = since_exact.replace(hour=0, minute=0, second=0, microsecond=0)
    closed = [first + timedelta(days=n) for n in range((today - first).days)]
    merged: list[ModelFloorLedgerRow] = []
    unsettled = (
        closed.pop()
        if closed and (now_utc - today).total_seconds() < settle_seconds
        else None
    )
    if cache is None:
        for day in closed:
            merged.extend(
                _read_day(
                    reader=reader,
                    read_factory=read_factory,
                    day_start=day,
                    day_end=day + timedelta(days=1),
                )
            )
    else:
        reads: list[
            tuple[ModelSourceCacheKey, Callable[[], tuple[ModelFloorLedgerRow, ...]]]
        ] = []
        for day in closed:

            def read_closed(day: datetime = day) -> tuple[ModelFloorLedgerRow, ...]:
                return _read_day(
                    reader=reader,
                    read_factory=read_factory,
                    day_start=day,
                    day_end=day + timedelta(days=1),
                )

            reads.append(
                (
                    ModelSourceCacheKey(
                        source_id=f"ledger-projection:{reader}:{day:%Y-%m-%d}",
                        params_digest=ModelSourceCacheKey.digest(
                            {"rows_format": ROWS_FORMAT}
                        ),
                        revision="closed-day",
                    ),
                    read_closed,
                )
            )
        for rows in cache.read_many(
            reads,
            encode_rows,
            decode_rows,
            max_workers=read_workers or 1,
            ttl_seconds=cache_ttl_seconds,
        ):
            merged.extend(rows)
    if unsettled is not None:
        merged.extend(
            _read_day(
                reader=reader,
                read_factory=read_factory,
                day_start=unsettled,
                day_end=today,
            )
        )
    merged.extend(
        _read_day(
            reader=reader, read_factory=read_factory, day_start=today, day_end=None
        )
    )
    return reduce_ledger_rows(
        tuple(row for row in merged if row.ts >= since_exact), kinds
    )
