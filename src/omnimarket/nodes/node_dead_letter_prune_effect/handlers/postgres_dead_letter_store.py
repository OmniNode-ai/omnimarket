# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""event_ledger binding of ProtocolDeadLetterStore (psycopg2, synchronous).

The day of a row is the UTC date of coalesce(event_timestamp, ledger_written_at),
the expression idx_event_ledger_topic_timestamp indexes, so day scans use that
index. Deletes name exact (topic, partition, kafka_offset) keys, the table's
unique constraint, and each call is its own committed transaction.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from omnimarket.nodes.node_dead_letter_prune_effect.models import (
    ModelDeadLetterDay,
    ModelDeadLetterRow,
)

_TS = "coalesce(event_timestamp, ledger_written_at)"


def _bounds(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime.combine(day, dt.time(0), tzinfo=dt.UTC)
    return start, start + dt.timedelta(days=1)


class PostgresDeadLetterStore:
    def __init__(self, dsn: str, *, table: str = "event_ledger") -> None:
        if table != "event_ledger":
            raise ValueError("only event_ledger is supported")
        self._dsn = dsn
        self._conn: Any = None

    def _c(self) -> Any:
        import psycopg2  # type: ignore[import-untyped]

        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self._dsn)
        return self._conn

    def close(self) -> None:
        if self._conn is not None and not self._conn.closed:
            self._conn.close()

    def list_days(
        self, *, cutoff: dt.date, topic_like: str
    ) -> list[ModelDeadLetterDay]:
        conn = self._c()
        with conn, conn.cursor() as cur:
            # Topics first, so the day scan below is an equality on the index's
            # leading column rather than a LIKE the planner cannot push into it.
            cur.execute(
                "select distinct topic from event_ledger where topic like %s",
                (topic_like,),
            )
            topics = sorted(r[0] for r in cur.fetchall())
            out: list[ModelDeadLetterDay] = []
            end, _ = _bounds(cutoff)
            for topic in topics:
                cur.execute(
                    f"select partition, ({_TS} at time zone 'UTC')::date as d, count(*) "
                    f"from event_ledger where topic = %s and {_TS} < %s "
                    "group by 1, 2 order by 2, 1",
                    (topic, end),
                )
                out.extend(
                    ModelDeadLetterDay(topic=topic, partition=p, day=d, row_count=n)
                    for p, d, n in cur.fetchall()
                )
        return sorted(out, key=lambda x: (x.day, x.topic, x.partition))

    def read_chunk(
        self, *, topic: str, partition: int, day: dt.date, after_offset: int, limit: int
    ) -> list[ModelDeadLetterRow]:
        lo, hi = _bounds(day)
        conn = self._c()
        with conn, conn.cursor() as cur:
            cur.execute(
                "select ledger_entry_id::text, topic, partition, kafka_offset, event_key, "
                "event_value, onex_headers, envelope_id::text, correlation_id::text, "
                "event_type, source, event_timestamp, ledger_written_at "
                f"from event_ledger where topic = %s and partition = %s and {_TS} >= %s "
                f"and {_TS} < %s and kafka_offset > %s order by kafka_offset limit %s",
                (topic, partition, lo, hi, after_offset, limit),
            )
            rows = cur.fetchall()
        return [
            ModelDeadLetterRow.from_values(
                ledger_entry_id=r[0],
                topic=r[1],
                partition=r[2],
                kafka_offset=r[3],
                event_key=None if r[4] is None else bytes(r[4]),
                event_value=bytes(r[5]),
                onex_headers=r[6],
                envelope_id=r[7],
                correlation_id=r[8],
                event_type=r[9],
                source=r[10],
                event_timestamp=r[11],
                ledger_written_at=r[12],
            )
            for r in rows
        ]

    def window_offsets(
        self,
        *,
        topic: str,
        partition: int,
        day: dt.date,
        first_offset: int,
        last_offset: int,
    ) -> list[int]:
        lo, hi = _bounds(day)
        conn = self._c()
        with conn, conn.cursor() as cur:
            cur.execute(
                f"select kafka_offset from event_ledger where topic = %s and partition = %s "
                f"and {_TS} >= %s and {_TS} < %s and kafka_offset between %s and %s "
                "order by kafka_offset",
                (topic, partition, lo, hi, first_offset, last_offset),
            )
            return [r[0] for r in cur.fetchall()]

    def delete_offsets(self, *, topic: str, partition: int, offsets: list[int]) -> int:
        conn = self._c()
        with conn, conn.cursor() as cur:  # one committed transaction per call
            cur.execute(
                "delete from event_ledger where topic = %s and partition = %s "
                "and kafka_offset = any(%s)",
                (topic, partition, offsets),
            )
            n: int = cur.rowcount
        return n
