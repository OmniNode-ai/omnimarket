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
import json
from collections.abc import Sequence
from datetime import datetime

from omnimarket.models.work_ledger_query import ModelWorkLedgerRowRecord
from omnimarket.nodes.node_work_ledger_query_effect.handlers import (
    work_ledger_query_replay as replay,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    WorkLedgerReadError,
)

_QUERY = "rows"


def encode_rows_cursor(row: ModelWorkLedgerRowRecord) -> str:
    """Opaque cursor naming the last row of a page and the query it belongs to."""
    body = json.dumps(
        {"q": _QUERY, "t": row.row_ts.isoformat(), "i": row.row_id},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(body.encode()).decode().rstrip("=")


def decode_rows_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        body = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if body["q"] != _QUERY:
            raise WorkLedgerReadError(f"cursor belongs to query {body['q']!r}")
        return datetime.fromisoformat(body["t"]), str(body["i"])
    except WorkLedgerReadError:
        raise
    except (binascii.Error, ValueError, KeyError, TypeError):
        raise WorkLedgerReadError("cursor is not a page cursor of this query") from None


def rows_page(
    rows: Sequence[replay.LedgerRow],
    *,
    lanes: Sequence[str],
    page_size: int,
    cursor: str | None,
    kinds: Sequence[str],
    since: datetime | None,
    ticket: str | None,
    pr: str | None,
    row_ref: str | None,
    term: str | None,
) -> tuple[int, list[ModelWorkLedgerRowRecord], str | None]:
    """The matched total, one page of the selection and the cursor of the next page."""
    filters = {
        "since": since,
        "ticket": ticket,
        "pr": pr,
        "kinds": kinds,
        "row_ref": row_ref,
        "term": term,
    }
    chosen: dict[str, ModelWorkLedgerRowRecord] = {}
    for lane in lanes or (None,):
        for row in replay.select_rows(rows, lane=lane, **filters):  # type: ignore[arg-type]
            chosen.setdefault(row.record.row_id, row.record)
    ordered = sorted(chosen.values(), key=lambda r: (r.row_ts, r.row_id))
    left = ordered
    if cursor:
        after = decode_rows_cursor(cursor)
        left = [r for r in ordered if (r.row_ts, r.row_id) > after]
    page = left[:page_size]
    more = len(left) > len(page)
    return len(ordered), page, encode_rows_cursor(page[-1]) if more and page else None
