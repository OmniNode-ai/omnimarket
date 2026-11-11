# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor's paged bus reads: every declared read's pages at once, a short read never a floor (OMN-23669).

The read node refuses any answer over its byte bound, so the floor reads the window a page at a time with the node's
own ``next_cursor``, each declared read in its own worker of the settings' pool. Every number is the settings'; a page
that does not answer, a cursor still open after the declared pages, or a spent time budget is an unread read: the step
raises naming the read, and no floor is drawn from the pages that did arrive.
"""

from __future__ import annotations

import math
import os
import shlex
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from omnimarket.models.model_point_query import ModelPointQuery
from omnimarket.nodes.node_factory_floor_effect.models.model_floor_ledger_pages import (
    ModelFloorLedgerPages,
    ModelFloorLedgerPagesResult,
    ModelFloorLedgerRead,
    ModelFloorLedgerReadCount,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    SourceRefusedError,
    parse_ledger_text,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters.point_read_adapters import (
    CommandPointRead,
)
from omnimarket.nodes.node_work_ledger_reader_effect.protocols import (
    ProtocolLedgerPointRead,
)


class HandlerFloorLedgerPages:
    """The floor's seam from the ledger's paged bus reads to the floor's typed rows."""

    def handle(
        self,
        settings: ModelFloorLedgerPages,
        *,
        window_days: int,
        kinds: frozenset[str],
        now: datetime,
        environ: Mapping[str, str] | None = None,
        source: ProtocolLedgerPointRead | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> ModelFloorLedgerPagesResult:
        """The window's rows. Raises ``SourceRefusedError`` naming what is missing or unread; a failed read is never
        an empty result.

        ``source`` is the injected read port; without one the command the settings' variable holds is the route, and
        each page runs under the time left of the budget so every page together stays inside it.
        """
        started = monotonic()
        env = os.environ if environ is None else environ
        argv = shlex.split(env.get(settings.read_command_env, ""))
        if source is None and not argv:
            raise SourceRefusedError(
                f"{settings.read_command_env} is not set (fail fast, no default): export the bus read command"
            )
        # The window is half-open; one second more keeps a row stamped exactly at ``now``.
        until = now + timedelta(seconds=1)
        executor = ThreadPoolExecutor(max_workers=settings.workers)
        try:
            futures = [
                executor.submit(
                    _read_pages,
                    read,
                    settings,
                    window_days=window_days,
                    now=now,
                    until=until,
                    argv=argv,
                    source=source,
                    started=started,
                    monotonic=monotonic,
                )
                for read in settings.reads
            ]
            finished = [
                (future.result(), read)
                for future, read in zip(futures, settings.reads, strict=True)
            ]
        except BaseException:
            # The first refusal stops the step: the reads that did not start are dropped, not run to nothing.
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        executor.shutdown(wait=True)
        lines: set[str] = set()
        counts: list[ModelFloorLedgerReadCount] = []
        for (read_lines, pages), read in finished:
            lines.update(read_lines)
            counts.append(
                ModelFloorLedgerReadCount(
                    name=read.name, pages=pages, lines=len(read_lines)
                )
            )
        return ModelFloorLedgerPagesResult(
            rows=parse_ledger_text(
                "\n".join(
                    f"L{i} {line}" for i, line in enumerate(sorted(lines), start=1)
                ),
                kinds,
            ),
            reads=tuple(counts),
            pages=sum(count.pages for count in counts),
        )


def _read_pages(
    read: ModelFloorLedgerRead,
    settings: ModelFloorLedgerPages,
    *,
    window_days: int,
    now: datetime,
    until: datetime,
    argv: list[str],
    source: ProtocolLedgerPointRead | None,
    started: float,
    monotonic: Callable[[], float],
) -> tuple[set[str], int]:
    """One read's lines, page by page. Any page that does not answer, a cursor still open after the declared
    pages, and a spent time budget each raise: the read is unread, never short."""
    since = (
        now - timedelta(hours=read.window_hours)
        if read.window_hours is not None
        else now - timedelta(days=window_days)
    )
    collected: set[str] = set()
    cursor: str | None = None
    pages = 0
    for page in range(1, settings.max_pages + 1):
        left = settings.time_budget_seconds - (monotonic() - started)
        if left <= 0:
            raise SourceRefusedError(
                f"the ledger read {read.name} is unread: the {settings.time_budget_seconds} s time budget ended before page {page}"
            )
        port = source or CommandPointRead(
            argv, max(1, min(settings.page_timeout_seconds, math.ceil(left)))
        )
        answer = port.read(
            ModelPointQuery(
                query="rows",
                since=since,
                until=until,
                kinds=read.kinds,
                term=read.term,
                page_size=settings.page_size,
                cursor=cursor,
            )
        )
        if answer.error:
            raise SourceRefusedError(
                f"the ledger read {read.name} is unread: page {page} did not answer ({answer.error}); no floor is drawn from a short read"
            )
        collected.update(answer.lines)
        pages = page
        cursor = answer.next_cursor
        if not cursor:
            return collected, pages
    raise SourceRefusedError(
        f"the ledger read {read.name} is unread: {settings.max_pages} pages and the cursor is still open; no floor is drawn from a short read"
    )
