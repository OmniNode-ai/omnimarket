# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build ledger_seq gap reports, and read their facts from work_ledger_rows (OMN-20541).

Shared by node_work_ledger_seq_gap_effect and any node that needs the database
ledger's completeness as a witness, so neither imports the other's handler.
"""

from __future__ import annotations

import contextlib
import os
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime
from itertools import pairwise

from omnimarket.models.model_work_ledger_seq_gap_report import (
    MAX_RANGES,
    ModelSeqRange,
    ModelWorkLedgerSeqGapReport,
    WorkLedgerSeqFacts,
)


def build_report(
    *,
    ledger_id: str,
    from_seq: int,
    gaps: Sequence[tuple[int, int]],
    duplicates: Sequence[int],
    duplicate_count: int,
    rows_with_seq: int,
    rows_without_seq: int,
    min_seq: int | None,
    max_seq: int | None,
    max_seq_row_ts: datetime | None,
    newest_row_ts: datetime | None,
    window_since: datetime | None = None,
    window_until: datetime | None = None,
    expected_max_seq: int | None = None,
) -> ModelWorkLedgerSeqGapReport:
    """Add the leading gap and retain bounded details with unbounded totals.

    Expected tail loss is reported separately from gaps between present rows.
    An empty examination has no observed gap or contiguous prefix.
    """
    if from_seq < 1:
        raise ValueError("from_seq must be at least 1")
    if expected_max_seq is not None and expected_max_seq < 0:
        raise ValueError("expected_max_seq must be nonnegative")
    ranges = sorted(gaps)
    if min_seq is not None and min_seq > from_seq:
        ranges.insert(0, (from_seq, min_seq - 1))
    first_missing = ranges[0][0] if ranges else None
    missing_count = sum(last - first + 1 for first, last in ranges)
    contiguous_through = (
        None
        if max_seq is None or first_missing == from_seq
        else first_missing - 1
        if first_missing is not None
        else max_seq
    )
    tail_missing = (
        max(0, expected_max_seq - (max_seq or 0)) if expected_max_seq is not None else 0
    )
    return ModelWorkLedgerSeqGapReport(
        ledger_id=ledger_id,
        window_since=window_since,
        window_until=window_until,
        from_seq=from_seq,
        to_seq=max_seq,
        rows_with_seq=rows_with_seq,
        rows_without_seq=rows_without_seq,
        first_missing_seq=first_missing,
        missing_count=missing_count,
        missing_ranges=tuple(
            ModelSeqRange(first=first, last=last) for first, last in ranges[:MAX_RANGES]
        ),
        missing_ranges_truncated=len(ranges) > MAX_RANGES,
        duplicate_seqs=tuple(sorted(set(duplicates))[:MAX_RANGES]),
        duplicate_count=duplicate_count,
        contiguous_through=contiguous_through,
        max_seq=max_seq,
        max_seq_row_ts=max_seq_row_ts,
        newest_row_ts=newest_row_ts,
        expected_max_seq=expected_max_seq,
        tail_missing=tail_missing,
        exact=(
            rows_with_seq > 0
            and missing_count == 0
            and duplicate_count == 0
            and tail_missing == 0
        ),
    )


def report_from_seqs(
    seqs: Iterable[int],
    *,
    ledger_id: str,
    from_seq: int = 1,
    rows_without_seq: int = 0,
    max_seq_row_ts: datetime | None = None,
    newest_row_ts: datetime | None = None,
    window_since: datetime | None = None,
    window_until: datetime | None = None,
    expected_max_seq: int | None = None,
) -> ModelWorkLedgerSeqGapReport:
    """Compute facts for small inputs; timestamps are caller-supplied metadata."""
    counts: Counter[int] = Counter()
    for seq in seqs:
        if type(seq) is not int or seq < 1:
            raise ValueError("sequences must be positive integers")
        if seq >= from_seq:
            counts[seq] += 1
    present = sorted(counts)
    gaps = [
        (left + 1, right - 1) for left, right in pairwise(present) if right > left + 1
    ]
    duplicates = [seq for seq in present if counts[seq] > 1]
    return build_report(
        ledger_id=ledger_id,
        from_seq=from_seq,
        gaps=gaps,
        duplicates=duplicates,
        duplicate_count=len(duplicates),
        rows_with_seq=sum(counts.values()),
        rows_without_seq=rows_without_seq,
        min_seq=present[0] if present else None,
        max_seq=present[-1] if present else None,
        max_seq_row_ts=max_seq_row_ts,
        newest_row_ts=newest_row_ts,
        window_since=window_since,
        window_until=window_until,
        expected_max_seq=expected_max_seq,
    )


def redact_database_error(exc: Exception, dsn_env: str) -> str:
    """Follow the ledger row reader's DSN redaction at the shared boundary."""
    message = str(exc)
    dsn = os.environ.get(dsn_env)
    if dsn:
        message = message.replace(dsn, "[redacted]")
    if "postgres://" in message or "postgresql://" in message or "password=" in message:
        message = "database connection/query failed (connection details redacted)"
    return f"{type(exc).__name__}: {message}"


class PostgresWorkLedgerSeqReader:
    def __init__(
        self,
        dsn_env: str,
        relation: str = "omninode_internal.work_ledger_rows",
    ) -> None:
        if not re.fullmatch(r"[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*", relation):
            raise ValueError("unsafe work ledger relation")
        self._dsn_env = dsn_env
        self._relation = relation

    def read_gap_facts(
        self,
        *,
        ledger_id: str,
        from_seq: int,
        since: datetime | None,
        until: datetime | None,
    ) -> WorkLedgerSeqFacts:
        """Window timestamps choose bounds; all ledger rows supply presence.

        Counts with a seq cover the examined range, including rows outside the
        timestamp window. Unsequenced counts and newest_row_ts cover the window.
        Inclusive timestamp bounds match the existing ledger read boundary.
        """
        if from_seq < 1 or (since is None) != (until is None):
            raise ValueError("invalid sequence range or unpaired window bounds")
        sql = f"""
            WITH window_rows AS (
                SELECT ledger_seq, row_ts FROM {self._relation}
                WHERE ledger_id = %(ledger_id)s
                  AND (%(since)s::timestamptz IS NULL OR row_ts >= %(since)s)
                  AND (%(until)s::timestamptz IS NULL OR row_ts <= %(until)s)
            ), bounds AS (
                SELECT
                    CASE WHEN %(since)s::timestamptz IS NULL THEN %(from_seq)s
                         ELSE greatest(%(from_seq)s, min(ledger_seq)) END AS lo,
                    max(ledger_seq) AS hi,
                    count(*) FILTER (WHERE ledger_seq IS NULL) AS without_seq,
                    max(row_ts) AS newest
                FROM window_rows
            ), examined AS (
                SELECT ledger_seq, row_ts FROM {self._relation}, bounds
                WHERE ledger_id = %(ledger_id)s AND ledger_seq BETWEEN lo AND hi
            ), present AS (
                SELECT ledger_seq, count(*) AS n, max(row_ts) AS row_ts
                FROM examined GROUP BY ledger_seq
            ), ordered AS (
                SELECT ledger_seq, lead(ledger_seq) OVER (ORDER BY ledger_seq) AS next
                FROM present
            ), gaps AS (
                SELECT ledger_seq + 1 AS first, next - 1 AS last FROM ordered
                WHERE next > ledger_seq + 1
            ), duplicates AS (
                SELECT ledger_seq FROM examined
                GROUP BY ledger_seq HAVING count(*) > 1
            )
            SELECT lo,
                coalesce((SELECT array_agg(ARRAY[first, last] ORDER BY first) FROM gaps),
                         ARRAY[]::bigint[][]),
                coalesce((SELECT array_agg(ledger_seq ORDER BY ledger_seq) FROM
                          (SELECT ledger_seq FROM duplicates ORDER BY ledger_seq LIMIT 50) d),
                         ARRAY[]::bigint[]),
                (SELECT count(*) FROM duplicates),
                coalesce((SELECT sum(n) FROM present), 0),
                without_seq,
                (SELECT min(ledger_seq) FROM present),
                (SELECT max(ledger_seq) FROM present),
                (SELECT row_ts FROM present ORDER BY ledger_seq DESC LIMIT 1),
                newest
            FROM bounds
        """
        conn = None
        try:
            dsn = os.environ.get(self._dsn_env)
            if not dsn or not dsn.strip():
                raise RuntimeError(f"contract-declared {self._dsn_env} is unset")
            from omnimarket.projection.postgres_read_database import connect_read_only

            conn = connect_read_only(dsn)
            with conn.cursor() as cursor:
                cursor.execute(
                    sql,
                    {
                        "ledger_id": ledger_id,
                        "from_seq": from_seq,
                        "since": since,
                        "until": until,
                    },
                )
                row = cursor.fetchone()
            return WorkLedgerSeqFacts(
                ledger_id=ledger_id,
                from_seq=int(row[0]),
                gaps=tuple((int(first), int(last)) for first, last in row[1]),
                duplicates=tuple(int(seq) for seq in row[2]),
                duplicate_count=int(row[3]),
                rows_with_seq=int(row[4]),
                rows_without_seq=int(row[5]),
                min_seq=row[6],
                max_seq=row[7],
                max_seq_row_ts=row[8],
                newest_row_ts=row[9],
                window_since=since,
                window_until=until,
            )
        except Exception as exc:
            raise RuntimeError(redact_database_error(exc, self._dsn_env)) from None
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()


__all__: list[str] = [
    "PostgresWorkLedgerSeqReader",
    "build_report",
    "redact_database_error",
    "report_from_seqs",
]
