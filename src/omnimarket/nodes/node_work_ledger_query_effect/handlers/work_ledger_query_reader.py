# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only, lazy Postgres work ledger query boundary."""

from __future__ import annotations

import contextlib
import importlib
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerLaneActivity,
    ModelWorkLedgerLatestEntry,
    ModelWorkLedgerLatestPage,
    ModelWorkLedgerLatestQuery,
    ModelWorkLedgerRowRecord,
    ModelWorkLedgerStateEntity,
    ModelWorkLedgerStatePage,
    ModelWorkLedgerStateQuery,
)

# psycopg2 ships no type stubs; bind its sql module as Any.
pgsql: Any = importlib.import_module("psycopg2.sql")


class ModelWorkLedgerQuerySource(BaseModel):
    """Required contract configuration for the database ledger reader."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    relation: str = Field(pattern=r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
    # The entity table the point queries read (OMN-21948); a source without one
    # answers every point query as an error.
    state_relation: str | None = Field(
        default=None, pattern=r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$"
    )
    # The newest-row-per-subject table the latest-state query reads (OMN-23606); a source without one
    # answers latest-state as an error.
    latest_relation: str | None = Field(
        default=None, pattern=r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$"
    )
    dsn_env: str = Field(min_length=1)
    parity_receipt_lane: str = Field(min_length=1)
    binding_ref: str = Field(min_length=1)


def load_work_ledger_query_source(contract_path: Path) -> ModelWorkLedgerQuerySource:
    """Load and validate the contract's declared ledger source."""
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("work_ledger_query"), dict):
        raise ValueError("work_ledger_query.ledger_source must be a mapping")
    source = raw["work_ledger_query"].get("ledger_source")
    if not isinstance(source, dict):
        raise ValueError("work_ledger_query.ledger_source must be a mapping")
    return ModelWorkLedgerQuerySource.model_validate(source)


class WorkLedgerReadError(RuntimeError):
    """A failed database read, its message already redacted and type-prefixed."""


class ProtocolWorkLedgerQueryReader(Protocol):
    """Read rows, daily parity receipts and projection freshness."""

    def read_rows(
        self, *, since: datetime | None, until: datetime
    ) -> tuple[ModelWorkLedgerRowRecord, ...]: ...
    def read_parity_receipts(
        self, *, since: datetime, until: datetime
    ) -> tuple[ModelWorkLedgerRowRecord, ...]: ...
    def freshness(
        self, *, until: datetime
    ) -> tuple[datetime | None, datetime | None]: ...

    def read_history(
        self, *, ledger_id: str, first_seq: int, last_seq: int, until: datetime
    ) -> tuple[ModelWorkLedgerRowRecord, ...]: ...


@runtime_checkable
class ProtocolWorkLedgerFilteredRowReader(Protocol):
    """Read rows of a window narrowed in the store before they are sent (OMN-19609).

    ``kinds`` keeps rows of those row types exactly. ``contains`` keeps rows whose
    text holds that string, a superset of every row the ``lane`` filter keeps (it
    matches the lane, from or to cell); ``term`` keeps rows whose text holds the
    request's term ignoring case (an ASCII term only, so it is a superset of what the
    handler keeps); the handler still applies its own filters, so the answer is the
    answer of the unfiltered read. ``since`` is ``None`` for a read with no start (OMN-23615).
    """

    def read_rows_matching(
        self,
        *,
        since: datetime | None,
        until: datetime,
        kinds: tuple[str, ...],
        contains: str | None,
        term: str | None = None,
    ) -> tuple[ModelWorkLedgerRowRecord, ...]: ...


@runtime_checkable
class ProtocolWorkLedgerOldestRowReader(Protocol):
    """Name the oldest row of the whole ledger, so a narrowed read with no start keeps its parity window (OMN-23615)."""

    def oldest_row_ts(self, *, until: datetime) -> datetime | None: ...


@runtime_checkable
class ProtocolWorkLedgerStateReader(Protocol):
    """Read one page of the open set from ``work_ledger_state`` (OMN-21948)."""

    def read_state_page(
        self, *, query: ModelWorkLedgerStateQuery
    ) -> ModelWorkLedgerStatePage: ...


@runtime_checkable
class ProtocolWorkLedgerLatestReader(Protocol):
    """Read one keyset page of the newest row per lane or ticket and row type from ``work_ledger_latest`` (OMN-23694)."""

    def read_latest_page(
        self, *, query: ModelWorkLedgerLatestQuery
    ) -> ModelWorkLedgerLatestPage: ...


#: The ruling horizon of the ``open-rulings`` verb: a ruling older than this is
#: no longer in flight.
RULING_IN_FLIGHT_HOURS = 24

#: Column order of the point query statement, one row per entity; the first
#: five columns repeat on every row so an empty page still carries the table's
#: watermark and counts.
STATE_PAGE_COLUMNS = (
    "watermark",
    "total",
    "released",
    "expired",
    "ledger_newest",
    "entity_key",
    "kind",
    "lane",
    "ticket",
    "repo",
    "pr",
    "scope_to",
    "scope_surface",
    "until_at",
    "opened_at",
    "row_id",
    "row_ts",
    "row_type",
    "row_lane",
    "raw_row",
    "source",
    "projected_at",
    "act_rows",
    "act_newest_ts",
    "act_newest_kind",
    "act_lease_rows",
)

_STATE_KINDS = {
    EnumWorkLedgerQueryKind.HOLDS: ["hold"],
    EnumWorkLedgerQueryKind.INBOX: ["msg", "hold"],
    EnumWorkLedgerQueryKind.OPEN_CLAIMS: ["claim"],
    EnumWorkLedgerQueryKind.OPEN_RULINGS: ["ruling"],
}

_STATE_PAGE_SQL = """
WITH meta AS (
    SELECT max(projected_at) AS watermark,
           count(*) FILTER (WHERE kind = ANY(%s)) AS total,
           count(*) FILTER (WHERE kind = 'hold' AND NOT is_open AND closed_at IS NOT NULL) AS released,
           count(*) FILTER (WHERE kind = 'hold' AND is_open AND scope_surface IS NOT NULL AND until_at < %s) AS expired,
           (SELECT max(row_ts) FROM {relation}) AS ledger_newest
    FROM {state}
), page AS (
    SELECT s.entity_key, s.kind, s.lane, s.ticket, s.repo, s.pr, s.scope_to,
           s.scope_surface, s.until_at, s.opened_at,
           r.row_id, r.row_ts, r.row_type, r.row_lane, r.raw_row, r.source, r.projected_at
    FROM {state} s LEFT JOIN {relation} r ON r.row_id = s.opened_row_id
    WHERE s.is_open AND s.kind = ANY(%s){filters}
    ORDER BY s.opened_at, s.entity_key
    LIMIT %s
){activity_cte}
SELECT meta.watermark, meta.total, meta.released, meta.expired, meta.ledger_newest,
       page.entity_key, page.kind, page.lane, page.ticket, page.repo, page.pr,
       page.scope_to, page.scope_surface, page.until_at, page.opened_at,
       page.row_id, page.row_ts, page.row_type, page.row_lane, page.raw_row,
       page.source, page.projected_at, {activity_columns}
FROM meta LEFT JOIN page ON true{activity_join}
ORDER BY page.opened_at, page.entity_key
"""
_ACTIVITY_NONE = (
    "NULL::bigint, NULL::timestamptz, NULL::text, NULL::text[]",
    "",
    "",
)
# Rows of the claim's lane since the claim opened: the count, the newest row and
# the rows that name a lease, from one pass over the lane's rows.
_ACTIVITY_CLAIMS = (
    "act.act_rows, act.act_newest_ts, act.act_newest_kind, act.act_lease_rows",
    r""", act AS (
    SELECT p.entity_key,
           count(x.row_id) AS act_rows,
           max(x.row_ts) AS act_newest_ts,
           (array_agg(x.row_type ORDER BY x.row_ts DESC, x.row_id DESC))[1] AS act_newest_kind,
           array_agg(x.raw_row) FILTER (WHERE x.raw_row ~ '\|\s*lease(-id|_id)?=') AS act_lease_rows
    FROM page p JOIN {relation} x ON x.row_lane = p.lane AND x.row_ts >= p.opened_at
    GROUP BY p.entity_key
)""",
    " LEFT JOIN act ON act.entity_key = page.entity_key",
)
_LEASE_RE = re.compile(r"\|\s*lease(?:-id|_id)?=(\S+)")


def _filters(query: ModelWorkLedgerStateQuery) -> tuple[str, list[object]]:
    """The kind's WHERE fragments, values bound in the order the text names them."""
    sql: list[str] = []
    params: list[object] = []
    point = query.point
    if point is EnumWorkLedgerQueryKind.HOLDS:
        sql.append(" AND NOT (s.scope_surface IS NOT NULL AND s.until_at < %s)")
        params.append(query.now)
        if query.pr:
            sql.append(
                " AND (%s = ANY(string_to_array(s.pr, ',')) OR (s.pr IS NULL AND s.repo = %s))"
            )
            params += [query.pr, query.pr.split("#")[0]]
        if query.repo:
            sql.append(" AND s.repo = %s")
            params.append(query.repo)
        if query.surface:
            sql.append(" AND s.scope_surface = %s")
            params.append(query.surface)
        if query.to:
            sql.append(
                " AND (%s = ANY(string_to_array(s.scope_to, ',')) OR 'all' = ANY(string_to_array(s.scope_to, ',')))"
            )
            params.append(query.to)
    elif point is EnumWorkLedgerQueryKind.INBOX:
        sql.append(" AND string_to_array(s.scope_to, ',') && %s::text[]")
        params.append([*query.lanes, "all"])
    elif point is EnumWorkLedgerQueryKind.OPEN_CLAIMS:
        if query.lane:
            sql.append(" AND s.lane = %s")
            params.append(query.lane)
        if query.claims_since:
            sql.append(" AND s.opened_at >= %s")
            params.append(query.claims_since)
    else:  # open rulings: in flight, naming the ticket, not acknowledged by stamp
        sql.append(" AND s.opened_at >= %s")
        params.append(query.now - timedelta(hours=RULING_IN_FLIGHT_HOURS))
        if query.ticket:
            sql.append(" AND (s.ticket = %s OR r.raw_row ~ %s)")
            params += [
                query.ticket,
                rf"\|\s*tickets?=[^|]*\y{re.escape(query.ticket)}\y",
            ]
        sql.append(
            " AND NOT EXISTS (SELECT 1 FROM {state} a WHERE a.entity_key = 'msg:' || "
            "to_char(s.opened_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"') "
            "AND a.closed_at IS NOT NULL)"
        )
    if query.after is not None:
        sql.append(" AND (s.opened_at, s.entity_key) > (%s, %s)")
        params += [query.after.opened_at, query.after.entity_key]
    return "".join(sql), params


def format_database_error(exc: Exception, source: ModelWorkLedgerQuerySource) -> str:
    """Keep a database exception useful without exposing its connection string."""
    message = str(exc)
    dsn = os.environ.get(source.dsn_env)
    if dsn:
        message = message.replace(dsn, "[redacted]")
    if "postgres://" in message or "postgresql://" in message or "password=" in message:
        message = "database connection/query failed (connection details redacted)"
    return f"{type(exc).__name__}: {message}"


_LATEST_WATERMARK_SQL = "SELECT max(projected_at) FROM {latest}"

#: Column order of the latest-state page statement.
LATEST_PAGE_COLUMNS = (
    "subject_kind",
    "subject",
    "row_type",
    "row_id",
    "at",
    "lane",
    "ticket",
    "pr",
    "outcome",
    "to_cell",
)


def _latest_page_sql(query: ModelWorkLedgerLatestQuery) -> tuple[str, list[object]]:
    """The page statement and its parameters: every value is bound, none is formatted into the text."""
    clauses: list[str] = []
    params: list[object] = []
    if query.after is not None:
        clauses.append("(subject_kind, subject, row_type) > (%s, %s, %s)")
        params += [
            query.after.subject_kind.value,
            query.after.subject,
            query.after.row_type,
        ]
    if query.subject_kind is not None:
        clauses.append("subject_kind = %s")
        params.append(query.subject_kind.value)
    if query.subjects:
        clauses.append("subject = ANY(%s)")
        params.append(list(query.subjects))
    if query.row_types:
        clauses.append("row_type = ANY(%s)")
        params.append(list(query.row_types))
    if query.since is not None:
        clauses.append("at >= %s")
        params.append(query.since)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = (
        f"SELECT {', '.join(LATEST_PAGE_COLUMNS)} FROM {{latest}}{where}"
        " ORDER BY subject_kind, subject, row_type LIMIT %s"
    )
    params.append(query.limit + 1)
    return sql, params


class PostgresWorkLedgerQueryReader:
    """Read the contract-declared relation through a lazy read-only connection."""

    def __init__(self, source: ModelWorkLedgerQuerySource) -> None:
        self._source = source
        self._conn: Any | None = None

    def _connect(self) -> Any:
        if self._conn is not None and not self._conn.closed:
            return self._conn
        dsn = os.environ.get(self._source.dsn_env)
        if not dsn or not dsn.strip():
            raise RuntimeError(f"contract-declared {self._source.dsn_env} is unset")
        from omnimarket.projection.postgres_read_database import connect_read_only

        self._conn = connect_read_only(dsn)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            conn, self._conn = self._conn, None
            conn.close()

    def _query(
        self, template: str, params: tuple[object, ...]
    ) -> list[tuple[Any, ...]]:
        """Run ``template`` with the relation bound as a quoted identifier."""
        schema, table = self._source.relation.split(".", 1)
        names = {"relation": pgsql.Identifier(schema, table)}
        if self._source.state_relation is not None:
            names["state"] = pgsql.Identifier(
                *self._source.state_relation.split(".", 1)
            )
        if self._source.latest_relation is not None:
            names["latest"] = pgsql.Identifier(
                *self._source.latest_relation.split(".", 1)
            )
        query = pgsql.SQL(template).format(**names)
        try:
            with self._connect().cursor() as cursor:
                cursor.execute(query, params)
                return list(cursor.fetchall())
        except Exception as exc:
            with contextlib.suppress(Exception):
                self.close()
            raise WorkLedgerReadError(
                format_database_error(exc, self._source)
            ) from None

    @staticmethod
    def _records(rows: list[tuple[Any, ...]]) -> tuple[ModelWorkLedgerRowRecord, ...]:
        return tuple(
            ModelWorkLedgerRowRecord(
                row_id=row_id,
                row_ts=row_ts,
                row_type=row_type,
                row_lane=row_lane,
                text=raw_row.split("\n", 1)[0],
                raw_row=raw_row,
                source=source,
                projected_at=projected_at,
            )
            for row_id, row_ts, row_type, row_lane, raw_row, source, projected_at in rows
        )

    def read_rows(
        self, *, since: datetime | None, until: datetime
    ) -> tuple[ModelWorkLedgerRowRecord, ...]:
        if since is None:
            rows = self._query(
                "SELECT row_id, row_ts, row_type, row_lane, raw_row, source, projected_at FROM {relation} WHERE row_ts <= %s ORDER BY row_ts, projected_at, row_id",
                (until,),
            )
        else:
            rows = self._query(
                "SELECT row_id, row_ts, row_type, row_lane, raw_row, source, projected_at FROM {relation} WHERE row_ts >= %s AND row_ts <= %s ORDER BY row_ts, projected_at, row_id",
                (since, until),
            )
        return self._records(rows)

    def read_rows_matching(
        self,
        *,
        since: datetime | None,
        until: datetime,
        kinds: tuple[str, ...],
        contains: str | None,
        term: str | None = None,
    ) -> tuple[ModelWorkLedgerRowRecord, ...]:
        """The window's rows narrowed by row type and a text match, in the store."""
        clauses = ["row_ts <= %s"]
        params: list[object] = [until]
        if since is not None:
            clauses.insert(0, "row_ts >= %s")
            params.insert(0, since)
        if kinds:
            clauses.append("row_type = ANY(%s)")
            params.append(list(kinds))
        if contains:
            clauses.append("strpos(raw_row, %s) > 0")
            params.append(contains)
        if term:
            clauses.append("position(lower(%s) in lower(raw_row)) > 0")
            params.append(term)
        rows = self._query(
            "SELECT row_id, row_ts, row_type, row_lane, raw_row, source, projected_at FROM {relation} WHERE "
            + " AND ".join(clauses)
            + " ORDER BY row_ts, projected_at, row_id",
            tuple(params),
        )
        return self._records(rows)

    def oldest_row_ts(self, *, until: datetime) -> datetime | None:
        rows = self._query(
            "SELECT min(row_ts) FROM {relation} WHERE row_ts <= %s", (until,)
        )
        oldest = rows[0][0]
        if oldest is not None and not isinstance(oldest, datetime):
            raise TypeError("database oldest row_ts is not a timestamp")
        return oldest

    def read_history(
        self, *, ledger_id: str, first_seq: int, last_seq: int, until: datetime
    ) -> tuple[ModelWorkLedgerRowRecord, ...]:
        """One statement snapshot, scoped to one ledger and an exclusive ingest cutoff.

        Unsequenced/untimed legacy rows remain visible so the caller cannot
        mistake a partial historical backfill for complete retained history.
        """
        rows = self._query(
            "SELECT row_id, row_ts, row_type, row_lane, raw_row, source, projected_at, ledger_seq, ingested_at "
            "FROM {relation} WHERE ledger_id = %s "
            "AND (ledger_seq BETWEEN %s AND %s OR ledger_seq IS NULL) "
            "AND (ingested_at < %s OR ingested_at IS NULL) "
            "ORDER BY ledger_seq NULLS LAST, row_id",
            (ledger_id, first_seq, last_seq, until),
        )
        return tuple(
            ModelWorkLedgerRowRecord(
                row_id=row_id,
                row_ts=row_ts,
                row_type=row_type,
                row_lane=row_lane,
                text=raw_row.split("\n", 1)[0],
                raw_row=raw_row,
                source=source,
                projected_at=projected_at,
                ledger_seq=ledger_seq,
                ingested_at=ingested_at,
            )
            for row_id, row_ts, row_type, row_lane, raw_row, source, projected_at, ledger_seq, ingested_at in rows
        )

    def read_parity_receipts(
        self, *, since: datetime, until: datetime
    ) -> tuple[ModelWorkLedgerRowRecord, ...]:
        rows = self._query(
            "SELECT row_id, row_ts, row_type, row_lane, raw_row, source, projected_at FROM {relation} WHERE row_type = 'STATUS' AND row_lane = %s AND row_ts >= %s AND row_ts <= %s ORDER BY row_ts, projected_at, row_id",
            (self._source.parity_receipt_lane, since, until),
        )
        return self._records(rows)

    def freshness(self, *, until: datetime) -> tuple[datetime | None, datetime | None]:
        rows = self._query(
            "SELECT max(row_ts), max(projected_at) FROM {relation} WHERE row_ts <= %s",
            (until,),
        )
        row_ts, projected_at = rows[0]
        if row_ts is not None and not isinstance(row_ts, datetime):
            raise TypeError("database newest row_ts is not a timestamp")
        if projected_at is not None and not isinstance(projected_at, datetime):
            raise TypeError("database newest projected_at is not a timestamp")
        return row_ts, projected_at

    def read_state_page(
        self, *, query: ModelWorkLedgerStateQuery
    ) -> ModelWorkLedgerStatePage:
        """One keyset page of the open set, in one statement snapshot.

        The statement carries the table's watermark and counts on every row
        (an empty page is still a row), and reads one entity past the limit to
        learn whether more follow.
        """
        if self._source.state_relation is None:
            raise WorkLedgerReadError(
                "contract work_ledger_query.ledger_source.state_relation is not declared"
            )
        filters, filter_params = _filters(query)
        columns, cte, join = (
            _ACTIVITY_CLAIMS
            if query.point is EnumWorkLedgerQueryKind.OPEN_CLAIMS
            else _ACTIVITY_NONE
        )
        template = (
            _STATE_PAGE_SQL.replace("{filters}", filters)
            .replace("{activity_cte}", cte)
            .replace("{activity_columns}", columns)
            .replace("{activity_join}", join)
        )
        kinds = _STATE_KINDS[query.point]
        rows = self._query(
            template, (kinds, query.now, kinds, *filter_params, query.limit + 1)
        )
        if not rows:
            raise WorkLedgerReadError("state read returned no watermark row")
        meta = dict(zip(STATE_PAGE_COLUMNS, rows[0], strict=True))
        entities = [
            self._entity(dict(zip(STATE_PAGE_COLUMNS, row, strict=True)))
            for row in rows
            if row[STATE_PAGE_COLUMNS.index("entity_key")] is not None
        ]
        return ModelWorkLedgerStatePage(
            entities=tuple(entities[: query.limit]),
            watermark=meta["watermark"],
            has_more=len(entities) > query.limit,
            total=meta["total"],
            released=meta["released"],
            expired=meta["expired"],
            ledger_newest=meta["ledger_newest"],
        )

    def read_latest_page(
        self, *, query: ModelWorkLedgerLatestQuery
    ) -> ModelWorkLedgerLatestPage:
        """One keyset page of ``work_ledger_latest`` and the table's watermark; one entry past the limit tells has_more."""
        if self._source.latest_relation is None:
            raise WorkLedgerReadError(
                "contract work_ledger_query.ledger_source.latest_relation is not declared"
            )
        sql, params = _latest_page_sql(query)
        marks = self._query(_LATEST_WATERMARK_SQL, ())
        rows = self._query(sql, tuple(params))
        entries = [
            ModelWorkLedgerLatestEntry(
                **dict(zip(LATEST_PAGE_COLUMNS[:-1], row[:-1], strict=True)),
                to=row[-1],
            )
            for row in rows
        ]
        watermark = marks[0][0] if marks else None
        return ModelWorkLedgerLatestPage(
            entries=tuple(entries[: query.limit]),
            watermark=watermark,
            has_more=len(entries) > query.limit,
        )

    @staticmethod
    def _entity(row: dict[str, Any]) -> ModelWorkLedgerStateEntity:
        if row["row_id"] is None:
            raise WorkLedgerReadError(
                f"state entity {row['entity_key']} has no opened row in the log"
            )
        activity = None
        if row["act_rows"] is not None:
            leases: list[str] = []
            for raw in row["act_lease_rows"] or ():
                for lease in _LEASE_RE.findall(raw.split("\n", 1)[0]):
                    if lease != "operator" and lease not in leases:
                        leases.append(lease)
            activity = ModelWorkLedgerLaneActivity(
                rows=row["act_rows"],
                newest_row_ts=row["act_newest_ts"],
                newest_row_kind=row["act_newest_kind"],
                leases=tuple(leases),
            )
        return ModelWorkLedgerStateEntity(
            entity_key=row["entity_key"],
            kind=row["kind"],
            lane=row["lane"],
            ticket=row["ticket"],
            repo=row["repo"],
            pr=row["pr"],
            scope_to=row["scope_to"],
            scope_surface=row["scope_surface"],
            until_at=row["until_at"],
            opened_at=row["opened_at"],
            opened_row=ModelWorkLedgerRowRecord(
                row_id=row["row_id"],
                row_ts=row["row_ts"],
                row_type=row["row_type"],
                row_lane=row["row_lane"],
                text=row["raw_row"].split("\n", 1)[0],
                raw_row=row["raw_row"],
                source=row["source"],
                projected_at=row["projected_at"],
            ),
            activity=activity,
        )
