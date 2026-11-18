# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One keyset page of a ``rows`` query that carries ``page_size`` (OMN-23421).

The selection is the ``query`` verb's own (``replay.select_rows``), taken per
named lane and joined without repeats; the page is the oldest rows first, and
the cursor names the last row of the page and the query it belongs to, the way
the point queries' cursors do (``work_ledger_state_answer``).
"""

from __future__ import annotations

import base64
import binascii
import bisect
import json
from collections.abc import Sequence
from datetime import datetime
from typing import NamedTuple

from omnimarket.models.work_ledger_query import ModelWorkLedgerRowRecord
from omnimarket.nodes.node_work_ledger_query_effect.handlers import (
    work_ledger_query_replay as replay,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    WorkLedgerReadError,
)

_QUERY = "rows"


class RowsCursor(NamedTuple):
    """What a rows cursor names: the last row of a page and the window's end it was read to."""

    row_ts: datetime
    row_id: str
    until: datetime | None


def encode_rows_cursor(row: ModelWorkLedgerRowRecord, until: datetime) -> str:
    """Opaque cursor naming the last row of a page, the query it belongs to and the window's end.

    The ``until`` pins every later page to the first page's window (OMN-24031): the window's total
    and rows no longer move with the clock between pages, and a held window can be found by it.
    """
    body = json.dumps(
        {
            "q": _QUERY,
            "t": row.row_ts.isoformat(),
            "i": row.row_id,
            "u": until.isoformat(),
        },
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")


def decode_rows_cursor(cursor: str) -> RowsCursor:
    try:
        body = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if body["q"] != _QUERY:
            raise WorkLedgerReadError(f"cursor belongs to query {body['q']!r}")
        until = body.get("u")
        return RowsCursor(
            datetime.fromisoformat(body["t"]),
            str(body["i"]),
            datetime.fromisoformat(until) if until is not None else None,
        )
    except WorkLedgerReadError:
        raise
    except (binascii.Error, ValueError, KeyError, TypeError):
        raise WorkLedgerReadError("cursor is not a page cursor of this query") from None


def select_ordered(
    rows: Sequence[replay.LedgerRow],
    *,
    lanes: Sequence[str],
    kinds: Sequence[str],
    since: datetime | None,
    ticket: str | None,
    pr: str | None,
    row_ref: str | None,
    term: str | None,
) -> tuple[ModelWorkLedgerRowRecord, ...]:
    """The selection of a rows query, taken per named lane, joined without repeats, oldest first."""
    chosen: dict[str, ModelWorkLedgerRowRecord] = {}
    for lane in lanes or (None,):
        selected = replay.select_rows(
            rows,
            since=since,
            lane=lane,
            pr=pr,
            ticket=ticket,
            kinds=kinds,
            row_ref=row_ref,
            term=term,
        )
        for row in selected:
            chosen.setdefault(row.record.row_id, row.record)
    return tuple(sorted(chosen.values(), key=_order_key))


def _order_key(row: ModelWorkLedgerRowRecord) -> tuple[datetime, str]:
    return (row.row_ts, row.row_id)


def page_of(
    ordered: Sequence[ModelWorkLedgerRowRecord],
    *,
    page_size: int,
    after: RowsCursor | None,
) -> tuple[int, list[ModelWorkLedgerRowRecord], bool]:
    """The matched total, one page cut after the cursor's row, and whether more rows remain."""
    start = (
        bisect.bisect_right(ordered, (after.row_ts, after.row_id), key=_order_key)
        if after is not None
        else 0
    )
    page = list(ordered[start : start + page_size])
    return len(ordered), page, start + len(page) < len(ordered)
