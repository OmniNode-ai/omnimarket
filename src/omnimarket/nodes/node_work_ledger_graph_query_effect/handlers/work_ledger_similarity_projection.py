# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the projected ledger page and decode its typed row cells."""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import closing
from datetime import datetime

from omnimarket.handlers.work_ledger_row_parser import parse_ledger_row
from omnimarket.models.work_ledger_graph.model_work_ledger_similarity import (
    ModelLedgerSimilarityCursor,
    ModelLedgerSimilarityIndexRequest,
    ModelLedgerSimilarityRow,
)
from omnimarket.projection.postgres_read_database import connect_read_only

_PAGE = """
SELECT row_id, row_ts, row_type, row_lane, tickets, raw_row
FROM omninode_internal.work_ledger_rows
WHERE ledger_id = %s AND row_ts < %s AND (row_ts, row_id) > (%s, %s)
ORDER BY row_ts, row_id
LIMIT %s
"""


type LedgerSimilarityPage = tuple[
    tuple[ModelLedgerSimilarityRow, ...], ModelLedgerSimilarityCursor | None
]
type LedgerSimilarityPageReader = Callable[
    [ModelLedgerSimilarityIndexRequest], LedgerSimilarityPage
]


def projected_row(
    row_id: str,
    row_ts: datetime,
    row_type: str,
    lane: str | None,
    tickets: list[str],
    raw_row: str,
) -> ModelLedgerSimilarityRow:
    """Use projected columns and explicit key=value cells from that same row."""
    event = parse_ledger_row(raw_row)
    fields: dict[str, str] = {}
    for cell in event.cells:
        if cell.value:
            fields.setdefault(cell.key, cell.value)
    return ModelLedgerSimilarityRow(
        row_id=row_id,
        row_ts=row_ts,
        row_type=row_type,
        lane=lane,
        ticket=tuple(tickets),
        text=event.free_text,
        question=fields.get("question") if row_type == "RULING" else None,
        kind=fields.get("kind"),
        pr=fields.get("pr"),
        head=fields.get("head"),
        outcome=fields.get("outcome"),
    )


def read_page(request: ModelLedgerSimilarityIndexRequest) -> LedgerSimilarityPage:
    window = request.projection
    dsn = os.environ.get("OMNINODE_INTERNAL_DB_URL")
    if not dsn:
        raise ValueError("OMNINODE_INTERNAL_DB_URL is unset")
    after_ts = window.after.row_ts if window.after is not None else window.since
    after_id = window.after.row_id if window.after is not None else ""
    try:
        with closing(connect_read_only(dsn)) as conn, conn.cursor() as cursor:
            cursor.execute(
                _PAGE,
                (
                    request.ledger_id,
                    window.until,
                    after_ts,
                    after_id,
                    window.page_size + 1,
                ),
            )
            records = cursor.fetchall()
    except Exception as exc:
        # Driver diagnostics may include connection material. Keep it out of the result.
        raise ValueError("work ledger projection read failed") from exc
    rows = tuple(projected_row(*record) for record in records[: window.page_size])
    next_cursor = None
    if len(records) > window.page_size:
        next_cursor = ModelLedgerSimilarityCursor(
            row_ts=rows[-1].row_ts, row_id=rows[-1].row_id
        )
    return rows, next_cursor
