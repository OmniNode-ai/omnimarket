# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick's ledger read over the bus: the window's rows, page by page, through the work ledger reader node.

The read node refuses an answer over its byte bound, so the tick pages with the node's own ``page_size`` and ``next_cursor``
and reads only the row kinds the overlay's ``ledger_reads`` names. A read that does not reach its last page (a page that did
not answer, a cursor still open after ``ledger_max_pages``, a spent time budget) is unread and refuses the tick: a rollup is
never drawn from a short read.
"""

from __future__ import annotations

import math
import shlex
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from omnimarket.models.friction_rollup import ModelFrictionRollupSource
from omnimarket.models.model_point_query import ModelPointQuery
from omnimarket.nodes.node_friction_rollup_tick_effect.models.model_friction_rollup_tick import (
    ModelFrictionRollupTickSettings,
    ModelTickLedgerRead,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters.point_read_adapters import (
    CommandPointRead,
)
from omnimarket.nodes.node_work_ledger_reader_effect.protocols import (
    ProtocolLedgerPointRead,
)

SOURCE_DISPLAY = "ledger-bus-read"


class LedgerUnreadError(RuntimeError):
    """The window could not be read to its last page; the message names what is missing."""


def read_command(
    settings: ModelFrictionRollupTickSettings, environ: Mapping[str, str]
) -> list[str]:
    return shlex.split(environ.get(settings.ledger_read_command_env, ""))


def read_window_sources(
    settings: ModelFrictionRollupTickSettings,
    *,
    now: datetime,
    argv: list[str],
    source: ProtocolLedgerPointRead | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> list[ModelFrictionRollupSource]:
    """One markdown source holding every first line the overlay's reads returned for the longest window."""
    if source is None and not argv:
        raise LedgerUnreadError(
            f"{settings.ledger_read_command_env} is not set (fail fast, no default): export the bus read command"
        )
    started = monotonic()
    since = now - timedelta(hours=settings.window_hours)
    # The window is half-open; one second more keeps a row stamped exactly at ``now``.
    until = now + timedelta(seconds=1)
    lines: list[str] = []
    for number, read in enumerate(settings.ledger_reads, start=1):
        lines.extend(
            _read_pages(
                read,
                number,
                settings,
                since=since.astimezone(UTC),
                until=until.astimezone(UTC),
                argv=argv,
                source=source,
                started=started,
                monotonic=monotonic,
            )
        )
    return [
        ModelFrictionRollupSource(
            display=SOURCE_DISPLAY,
            kind="markdown",
            text="\n".join(sorted(lines)) + "\n",
        )
    ]


def _read_pages(
    read: ModelTickLedgerRead,
    number: int,
    settings: ModelFrictionRollupTickSettings,
    *,
    since: datetime,
    until: datetime,
    argv: list[str],
    source: ProtocolLedgerPointRead | None,
    started: float,
    monotonic: Callable[[], float],
) -> list[str]:
    label = (
        f"read {number} ({','.join(read.kinds)}{' ' + read.term if read.term else ''})"
    )
    collected: list[str] = []
    cursor: str | None = None
    for page in range(1, settings.ledger_max_pages + 1):
        left = settings.ledger_time_budget_seconds - (monotonic() - started)
        if left <= 0:
            raise LedgerUnreadError(
                f"the ledger {label} is unread: the {settings.ledger_time_budget_seconds} s time budget ended before page {page}"
            )
        port = source or CommandPointRead(
            argv, max(1, min(settings.ledger_page_timeout_seconds, math.ceil(left)))
        )
        answer = port.read(
            ModelPointQuery(
                query="rows",
                since=since,
                until=until,
                kinds=read.kinds,
                term=read.term,
                page_size=settings.ledger_page_size,
                cursor=cursor,
            )
        )
        if answer.error:
            raise LedgerUnreadError(
                f"the ledger {label} is unread: page {page} did not answer ({answer.error})"
            )
        collected.extend(answer.lines)
        cursor = answer.next_cursor
        if not cursor:
            return collected
    raise LedgerUnreadError(
        f"the ledger {label} is unread: {settings.ledger_max_pages} pages and the cursor is still open"
    )
