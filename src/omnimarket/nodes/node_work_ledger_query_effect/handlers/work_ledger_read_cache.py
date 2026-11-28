# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The held whole-window read of node_work_ledger_query_effect (OMN-26152).

An unpaged read rebuilt and re-parsed every row of its window on each query, which held the
runtime's GIL. The parsed window is held while the store's watermark (newest row timestamp and
newest projection time, both read per query) is unchanged, so an appended row misses the hold and
the next query reads again. The hold serves any later window of the same request shape that starts
no earlier than the held one, cut to the asked bounds, so a bounded window sliding with the clock
still hits. Capacity and TTL are the held rows window's settings; none is in code.
"""

from __future__ import annotations

import threading
import time
from bisect import bisect_left
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise

from omnimarket.models.work_ledger_query import ModelWorkLedgerRowRecord
from omnimarket.nodes.node_work_ledger_query_effect.handlers import (
    work_ledger_query_replay as replay,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_rows_window_cache import (
    ModelRowsWindowCacheSettings,
)


def sort_key(record: ModelWorkLedgerRowRecord) -> tuple[datetime, datetime, str]:
    """The store's row order: row_ts, projected_at, row_id."""
    return (
        record.row_ts,
        record.projected_at or datetime.max.replace(tzinfo=record.row_ts.tzinfo),
        record.row_id,
    )


@dataclass(frozen=True)
class HeldRead:
    """One whole-window read: parsed rows, the row_ts of every record read, the watermark."""

    since: datetime | None
    until: datetime
    parsed: tuple[replay.LedgerRow, ...]
    parsed_ts: tuple[datetime, ...]
    record_ts: tuple[datetime, ...]
    newest_row_ts: datetime | None
    newest_projected_at: datetime | None
    record_ids: frozenset[str] = frozenset()
    tail_key: tuple[datetime, datetime, str] | None = None

    @classmethod
    def build(
        cls,
        *,
        since: datetime | None,
        until: datetime,
        parsed: list[replay.LedgerRow],
        record_ts: list[datetime],
        newest_row_ts: datetime | None,
        newest_projected_at: datetime | None,
        records: Sequence[ModelWorkLedgerRowRecord] = (),
    ) -> HeldRead | None:
        """The hold, or None when the rows are not in row_ts order (a cut by time needs it)."""
        parsed_ts = tuple(r.record.row_ts for r in parsed)
        if any(b < a for a, b in pairwise(record_ts)) or any(
            b < a for a, b in pairwise(parsed_ts)
        ):
            return None
        return cls(
            since,
            until,
            tuple(parsed),
            parsed_ts,
            tuple(record_ts),
            newest_row_ts,
            newest_projected_at,
            frozenset(r.row_id for r in records),
            sort_key(records[-1]) if records else None,
        )

    def extend(
        self,
        *,
        until: datetime,
        records: Sequence[ModelWorkLedgerRowRecord],
        parsed: list[replay.LedgerRow],
        newest_row_ts: datetime | None,
        newest_projected_at: datetime | None,
    ) -> HeldRead | None:
        """This hold with rows projected since appended, or None when a full read is needed.

        Rows already held are skipped by id. None when the new rows are not in store order, or
        the first of them sorts before the held tail: appending would then differ from a rebuild.
        """
        if self.tail_key is None:
            return None
        new = [r for r in records if r.row_id not in self.record_ids]
        keys = [sort_key(r) for r in new]
        if any(b < a for a, b in pairwise(keys)):
            return None
        if keys and keys[0] < self.tail_key:
            return None
        return HeldRead(
            self.since,
            until,
            self.parsed + tuple(parsed),
            self.parsed_ts + tuple(r.record.row_ts for r in parsed),
            self.record_ts + tuple(r.row_ts for r in new),
            newest_row_ts,
            newest_projected_at,
            self.record_ids | {r.row_id for r in new},
            keys[-1] if keys else self.tail_key,
        )

    @property
    def oldest_ts(self) -> datetime | None:
        return self.record_ts[0] if self.record_ts else None

    def covers(
        self,
        since: datetime | None,
        until: datetime,
        newest_row_ts: datetime | None,
        newest_projected_at: datetime | None,
    ) -> bool:
        """True when the held rows are exactly the rows a read from ``since`` would return."""
        if (newest_row_ts, newest_projected_at) != (
            self.newest_row_ts,
            self.newest_projected_at,
        ):
            return False
        if until < self.until:
            return False
        return self.since_covers(since)

    def since_covers(self, since: datetime | None) -> bool:
        """True when the held window starts no later than ``since``."""
        if self.since is None:
            return True
        return since is not None and since >= self.since

    def cut(self, since: datetime | None) -> tuple[list[replay.LedgerRow], int]:
        """The parsed rows and the record count from ``since`` on."""
        if since is None:
            return list(self.parsed), len(self.record_ts)
        return (
            list(self.parsed[bisect_left(self.parsed_ts, since) :]),
            len(self.record_ts) - bisect_left(self.record_ts, since),
        )


class ReadCache:
    """A bounded, thread-safe, TTL-bound cache of held whole-window reads."""

    def __init__(
        self,
        settings: ModelRowsWindowCacheSettings,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[tuple[object, ...], tuple[HeldRead, float]] = {}

    def get(self, key: tuple[object, ...]) -> HeldRead | None:
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if now - entry[1] > self._settings.ttl_seconds:
                del self._entries[key]
                return None
            return entry[0]

    def put(self, key: tuple[object, ...], held: HeldRead) -> None:
        now = self._clock()
        with self._lock:
            self._entries[key] = (held, now)
            while len(self._entries) > self._settings.max_windows:
                oldest = min(self._entries, key=lambda k: self._entries[k][1])
                del self._entries[oldest]


__all__ = ["HeldRead", "ReadCache", "sort_key"]
