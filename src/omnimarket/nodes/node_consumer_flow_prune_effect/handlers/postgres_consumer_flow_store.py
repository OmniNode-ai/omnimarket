# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""consumer_flow_windows binding of ProtocolConsumerFlowStore (psycopg2, synchronous).

No index on the table leads with window_start, so a day is listed once with a
single scan (day_cursors) and every later read and delete goes through the
unique projection_cursor index with ``= any(cursors)``. Every statement also
bounds window_start to the named UTC day, so a wrong cursor can never reach a
row on another day. Each delete call is its own committed transaction.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from omnimarket.nodes.node_consumer_flow_prune_effect.models import (
    ModelConsumerFlowDay,
    ModelConsumerFlowRow,
)

_TABLE = "omninode_internal.consumer_flow_windows"
_COLUMNS = (
    "consumer_group, topic, window_start, window_end, node_id::text, "
    "ingest_sequence, messages_in, messages_out, messages_dlq, handler_errors, "
    "upstream_produced, upstream_evidence, flow_state, evaluated_at, projection_cursor"
)


def _bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime.combine(day, dt.time(0), tzinfo=dt.UTC)
    return start, start + dt.timedelta(days=1)


class PostgresConsumerFlowStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: Any = None

    def _c(self) -> Any:
        import psycopg2  # type: ignore[import-untyped]

        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self._dsn)
            with self._conn, self._conn.cursor() as cur:
                cur.execute("set time zone 'UTC'")
        return self._conn

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()

    def list_days(self, *, cutoff: dt.date) -> list[ModelConsumerFlowDay]:
        end, _ = _bounds(cutoff)
        conn = self._c()
        with conn, conn.cursor() as cur:
            cur.execute(
                "select (window_start at time zone 'UTC')::date as d, count(*) "
                f"from {_TABLE} where window_start < %s group by 1 order by 1",
                (end,),
            )
            return [ModelConsumerFlowDay(day=d, row_count=n) for d, n in cur.fetchall()]

    def day_cursors(self, *, day: dt.date) -> list[int]:
        lo, hi = _bounds(day)
        conn = self._c()
        with conn, conn.cursor() as cur:
            cur.execute(
                f"select projection_cursor from {_TABLE} "
                "where window_start >= %s and window_start < %s order by 1",
                (lo, hi),
            )
            return [r[0] for r in cur.fetchall()]

    def read_rows(
        self, *, day: dt.date, cursors: list[int]
    ) -> list[ModelConsumerFlowRow]:
        lo, hi = _bounds(day)
        conn = self._c()
        with conn, conn.cursor() as cur:
            cur.execute(
                f"select {_COLUMNS} from {_TABLE} where projection_cursor = any(%s) "
                "and window_start >= %s and window_start < %s order by projection_cursor",
                (cursors, lo, hi),
            )
            rows = cur.fetchall()
        return [
            ModelConsumerFlowRow(
                consumer_group=r[0],
                topic=r[1],
                window_start=r[2],
                window_end=r[3],
                node_id=r[4],
                ingest_sequence=r[5],
                messages_in=r[6],
                messages_out=r[7],
                messages_dlq=r[8],
                handler_errors=r[9],
                upstream_produced=r[10],
                upstream_evidence=r[11],
                flow_state=r[12],
                evaluated_at=r[13],
                projection_cursor=r[14],
            )
            for r in rows
        ]

    def delete_cursors(self, *, day: dt.date, cursors: list[int]) -> int:
        lo, hi = _bounds(day)
        conn = self._c()
        with conn, conn.cursor() as cur:  # one committed transaction per call
            cur.execute(
                f"delete from {_TABLE} where projection_cursor = any(%s) "
                "and window_start >= %s and window_start < %s",
                (cursors, lo, hi),
            )
            n: int = cur.rowcount
        return n
