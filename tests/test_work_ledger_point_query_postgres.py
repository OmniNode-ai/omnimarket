# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Point queries against a real PostgreSQL work_ledger_state (OMN-21948).

Rows are projected the way the projection writer stores them (its own row insert,
the fold's own state ops and its own guarded upserts), into a disposable schema.
The production reader then answers holds, inbox, open-claims and open-rulings
from the generated ``is_open`` column. Only a loopback or Unix-socket DSN is
accepted; no deployment database is read or mutated.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg2
import pytest
from psycopg2.extensions import make_dsn, parse_dsn

from omnimarket.handlers.work_ledger_fold import fold_row
from omnimarket.models.model_work_ledger_fold_request import ModelWorkLedgerFoldRequest
from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerQueryRequest,
)
from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_projection import (
    _INSERT_ROW,
    _op_args,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    ModelWorkLedgerQuerySource,
    PostgresWorkLedgerQueryReader,
)

pytestmark = pytest.mark.integration
MIGRATIONS = (
    Path(__file__).parents[1]
    / "src/omnimarket/nodes/node_projection_work_ledger/migrations"
)
ENV = "ONEX_TEST_LEDGER_POINT_DSN"
K = EnumWorkLedgerQueryKind
NOW = datetime(2026, 11, 2, 3, tzinfo=UTC)

LEDGER = (
    "2026-11-01T09:00:00Z | CLAIM | lane=lane-open | ticket=OMN-1 | host=h051 | run=r1 | scope=x",
    "2026-11-01T09:05:00Z | STATUS | lane=lane-open | working",
    "2026-11-01T09:06:00Z | CLAIM | lane=lane-done | ticket=OMN-2 | host=h051 | run=r2 | scope=y",
    "2026-11-01T09:30:00Z | TERMINAL | lane=lane-done | outcome=done | finished",
    "2026-11-02T01:00:00Z | HOLD | lane=orch | id=hold-live | to=lane-open | repo=omnimarket | surface=s1 | until=2026-11-09T00:00:00Z | pause s1",
    "2026-11-02T01:01:00Z | HOLD | lane=orch | id=hold-freed | to=lane-open | repo=omnimarket | surface=s2 | pause s2",
    "2026-11-02T01:02:00Z | RELEASE | lane=orch | re=hold-freed | lifted",
    "2026-11-02T01:03:00Z | HOLD | lane=orch | id=hold-old | to=lane-open | repo=omnimarket | surface=s3 | until=2026-11-01T12:00:00Z | stale",
    "2026-11-02T01:10:00Z | MSG | from=orch | to=lane-open | id=msg-open | ticket=OMN-1 | please look at the plan",
    "2026-11-02T01:11:00Z | MSG | from=orch | to=lane-open | id=msg-acked | ticket=OMN-1 | already answered",
    "2026-11-02T01:12:00Z | ACK | from=lane-open | to=orch | id=ack-1 | re=msg-acked | done",
    '2026-11-02T01:20:00Z | RULING | lane=orch | ticket=OMN-77 | "go" | open ruling',
    '2026-11-02T01:21:00Z | RULING | lane=orch | ticket=OMN-78 | "go" | acknowledged ruling',
    "2026-11-02T01:22:00Z | ACK | from=lane-open | to=orch | id=ack-2 | re=2026-11-02T01:21:00Z | seen",
)


def _test_dsn() -> str | None:
    dsn = os.environ.get(ENV)
    if dsn:
        return dsn
    service = {
        key: os.environ.get(f"INTEGRATION_POSTGRES_{key.upper()}")
        for key in ("host", "port", "user", "password", "db")
    }
    if not all(service.values()):
        return None
    return make_dsn(
        host=service["host"],
        port=service["port"],
        user=service["user"],
        password=service["password"],
        dbname=service["db"],
    )


def project(conn: Any, schema: str, raws: Iterable[str]) -> None:
    """Store each row and fold its state ops with the projection writer's statements."""
    now = datetime.now(UTC)
    with conn.cursor() as cursor:
        for raw in raws:
            result = fold_row(ModelWorkLedgerFoldRequest(raw_row=raw, source="test"))
            row = result.row
            cursor.execute(
                re.sub(r"\$\d+", "%s", _INSERT_ROW).replace(
                    "omninode_internal.", f"{schema}."
                ),
                (
                    row.row_id,
                    row.ledger_id,
                    row.row_ts,
                    row.row_type,
                    row.row_lane,
                    "[]",
                    row.raw_row,
                    row.source,
                    now,
                    row.ledger_seq,
                ),
            )
            for op in result.ops:
                sql, args = _op_args(op, now)
                cursor.execute(
                    re.sub(r"\$\d+", "%s", sql).replace(
                        "omninode_internal.", f"{schema}."
                    ),
                    _positional(sql, args),
                )


def _positional(sql: str, args: tuple[Any, ...]) -> tuple[Any, ...]:
    """The writer's statements number their parameters; psycopg2 wants them in text order."""
    return tuple(args[int(n) - 1] for n in re.findall(r"\$(\d+)", sql))


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, str]]:
    dsn = _test_dsn()
    if not dsn:
        pytest.skip(f"{ENV} must name an ephemeral local PostgreSQL database")
    monkeypatch.setenv(ENV, dsn)
    host = parse_dsn(dsn).get("host", "")
    if host not in {"127.0.0.1", "::1", "localhost"} and not host.startswith("/"):
        pytest.fail(
            "point query proof requires an explicit loopback or Unix socket host"
        )
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    schema = f"ledger_point_{uuid4().hex}"
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"CREATE SCHEMA {schema}")
            for name in ("0000_create_work_ledger.sql", "0003_work_ledger_seq.sql"):
                cursor.execute(
                    (MIGRATIONS / name)
                    .read_text()
                    .replace("omninode_internal.", f"{schema}.")
                )
        yield conn, schema
    finally:
        with conn.cursor() as cursor:
            cursor.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.close()


def reader_for(schema: str, dsn_env: str = ENV) -> PostgresWorkLedgerQueryReader:
    return PostgresWorkLedgerQueryReader(
        ModelWorkLedgerQuerySource(
            relation=f"{schema}.work_ledger_rows",
            state_relation=f"{schema}.work_ledger_state",
            dsn_env=dsn_env,
            parity_receipt_lane="work-ledger-parity",
            binding_ref="ephemeral-test",
        )
    )


def node(schema: str, dsn_env: str = ENV) -> HandlerWorkLedgerQuery:
    return HandlerWorkLedgerQuery(reader=reader_for(schema, dsn_env), now=lambda: NOW)


def ask(
    handler: HandlerWorkLedgerQuery, kind: EnumWorkLedgerQueryKind, **fields: Any
) -> Any:
    return handler.handle(
        ModelWorkLedgerQueryRequest(correlation_id=uuid4(), query=kind, **fields)
    )


def test_point_query_postgres_holds_read_is_open_not_the_log(database: Any) -> None:
    conn, schema = database
    project(conn, schema, LEDGER)
    result = ask(node(schema), K.HOLDS)
    assert result.status == "ok"
    assert [h.hold_id for h in result.holds] == ["hold-live"]
    assert result.lines[0].startswith("HOLD hold-live by=orch scope=<to=lane-open;")
    assert result.hold_counts.checked == 3
    assert result.hold_counts.released == 1
    assert result.hold_counts.expired == 1
    assert result.watermark is not None
    assert result.page_size == 500


def test_point_query_postgres_holds_filter_by_surface(database: Any) -> None:
    conn, schema = database
    project(conn, schema, LEDGER)
    assert ask(node(schema), K.HOLDS, surface="s1").matched == 1
    assert ask(node(schema), K.HOLDS, surface="nope").matched == 0
    assert ask(node(schema), K.HOLDS, to="lane-open").matched == 1
    assert ask(node(schema), K.HOLDS, to="someone-else").matched == 0


def test_point_query_postgres_inbox_lists_unanswered_work_for_a_lane(
    database: Any,
) -> None:
    conn, schema = database
    project(conn, schema, LEDGER)
    result = ask(node(schema), K.INBOX, lanes=("lane-open",))
    assert result.status == "ok"
    # an expired hold stays in the inbox, as in the verb: only a RELEASE removes it
    assert result.lines[0].startswith(
        "inbox lane-open: 3 open, 3 need action (HOLD:2 ask:1)"
    )
    assert any("MSG id=msg-open" in line for line in result.lines)
    assert not any("msg-acked" in line for line in result.lines)
    assert ask(node(schema), K.INBOX, lane="nobody").lines == (
        "inbox nobody: 0 open, 0 need action (HOLD:0 ask:0), 0 FYI, newest=none",
    )


def test_point_query_postgres_open_claims_carry_the_lane_activity(
    database: Any,
) -> None:
    conn, schema = database
    project(conn, schema, LEDGER)
    result = ask(node(schema), K.OPEN_CLAIMS)
    assert result.status == "ok"
    assert [c.lane for c in result.open_claims] == ["lane-open"]
    claim = result.open_claims[0]
    # the claim, a STATUS and two ACKs sent from the lane (row_lane takes from=)
    assert claim.rows == 4
    assert claim.newest_row_kind == "ACK"
    assert claim.newest_row_stamp == "2026-11-02T01:22:00Z"
    assert result.lines[0].startswith("open-claims since=beginning claims=2 open=1 ")
    assert "ticket=OMN-1" in result.lines[1]
    assert ask(node(schema), K.OPEN_CLAIMS, lane="lane-done").matched == 0


def test_point_query_postgres_open_rulings_skip_the_acknowledged_one(
    database: Any,
) -> None:
    conn, schema = database
    project(conn, schema, LEDGER)
    assert ask(node(schema), K.OPEN_RULINGS, ticket="OMN-77").lines == (
        "OPEN-RULINGS ticket=OMN-77 n=1 stamps=2026-11-02T01:20:00Z",
    )
    assert ask(node(schema), K.OPEN_RULINGS, ticket="OMN-78").lines == (
        "OPEN-RULINGS ticket=OMN-78 n=0 stamps=none",
    )


def test_page_postgres_cursor_walks_every_entity_exactly_once(database: Any) -> None:
    conn, schema = database
    raws = [
        f"2026-11-01T{h:02d}:00:00Z | CLAIM | lane=lane-{h:02d} | ticket=OMN-{h} | host=h | run=r"
        for h in range(10)
    ]
    project(conn, schema, raws)
    handler = node(schema)
    lanes: list[str] = []
    cursor = None
    pages = 0
    while True:
        result = ask(handler, K.OPEN_CLAIMS, page_size=3, cursor=cursor)
        assert result.status == "ok"
        assert result.page_size == 3
        assert result.watermark is not None
        lanes += [c.lane for c in result.open_claims]
        pages += 1
        cursor = result.next_cursor
        if cursor is None:
            break
    assert pages == 4
    assert lanes == [f"lane-{h:02d}" for h in range(10)]


def test_fault_postgres_unreachable_database_is_status_error_for_every_point_query(
    database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, schema = database
    monkeypatch.setenv(
        "ONEX_TEST_UNREACHABLE", "postgresql://nobody:nopass@127.0.0.1:1/none"
    )
    handler = node(schema, dsn_env="ONEX_TEST_UNREACHABLE")
    for kind, extra in (
        (K.HOLDS, {}),
        (K.INBOX, {"lanes": ("lane-open",)}),
        (K.OPEN_CLAIMS, {}),
        (K.OPEN_RULINGS, {"ticket": "OMN-77"}),
    ):
        result = ask(handler, kind, **extra)
        assert result.status == "error"
        assert result.error
        assert "nopass" not in result.error
        assert result.lines == ()
        assert result.matched == 0


def test_fault_postgres_missing_state_table_is_status_error(database: Any) -> None:
    conn, schema = database
    with conn.cursor() as cursor:
        cursor.execute(f"DROP TABLE {schema}.work_ledger_state")
    result = ask(node(schema), K.HOLDS)
    assert result.status == "error"
    assert result.lines == ()


def _percentile95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[int(len(ordered) * 0.95) - 1]


def test_point_query_postgres_timed_loop_meets_the_bars_over_100_calls(
    database: Any,
) -> None:
    """100 calls each, a fresh reader (connection) per call, against a loaded state
    table: 2,000 open claims over 42,000 log rows, 3,000 open messages, 60 holds."""
    import time

    conn, schema = database
    with conn.cursor() as cursor:
        cursor.execute(
            f"""
            INSERT INTO {schema}.work_ledger_rows
                (row_id, ledger_id, row_ts, row_type, row_lane, tickets, raw_row, source, projected_at)
            SELECT md5('c' || i), 'rolling-work-ledger', ts, 'CLAIM', 'l-' || i, '[]',
                   to_char(ts, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') || ' | CLAIM | lane=l-' || i || ' | ticket=OMN-' || i || ' | host=h | run=r',
                   'timed', now()
            FROM (SELECT i, timestamptz '2026-10-01' + i * interval '1 minute' AS ts
                  FROM generate_series(1, 2000) i) g;
            INSERT INTO {schema}.work_ledger_rows
                (row_id, ledger_id, row_ts, row_type, row_lane, tickets, raw_row, source, projected_at)
            SELECT md5('s' || i), 'rolling-work-ledger', ts, 'STATUS', 'l-' || (i % 2000 + 1), '[]',
                   to_char(ts, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') || ' | STATUS | lane=l-' || (i % 2000 + 1) || ' | step',
                   'timed', now()
            FROM (SELECT i, timestamptz '2026-10-05' + i * interval '10 second' AS ts
                  FROM generate_series(1, 40000) i) g;
            INSERT INTO {schema}.work_ledger_rows
                (row_id, ledger_id, row_ts, row_type, row_lane, tickets, raw_row, source, projected_at)
            SELECT md5('m' || i), 'rolling-work-ledger', ts, 'MSG', 'orch', '[]',
                   to_char(ts, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') || ' | MSG | from=orch | to=lane-t | id=m' || i || ' | ask',
                   'timed', now()
            FROM (SELECT i, timestamptz '2026-10-20' + i * interval '1 minute' AS ts
                  FROM generate_series(1, 3000) i) g;
            INSERT INTO {schema}.work_ledger_rows
                (row_id, ledger_id, row_ts, row_type, row_lane, tickets, raw_row, source, projected_at)
            SELECT md5('h' || i), 'rolling-work-ledger', ts, 'HOLD', 'orch', '[]',
                   to_char(ts, 'YYYY-MM-DD"T"HH24:MI:SS"Z"') || ' | HOLD | lane=orch | id=h' || i || ' | to=lane-t | repo=r | pause',
                   'timed', now()
            FROM (SELECT i, timestamptz '2026-10-25' + i * interval '1 minute' AS ts
                  FROM generate_series(1, 60) i) g;
            INSERT INTO {schema}.work_ledger_state
                (entity_key, kind, lane, ticket, opened_at, opened_row_id, projected_at)
            SELECT 'claim:' || row_lane, 'claim', row_lane,
                   substring(raw_row from 'ticket=(OMN-[0-9]+)'), row_ts, row_id, now()
            FROM {schema}.work_ledger_rows WHERE row_type = 'CLAIM';
            INSERT INTO {schema}.work_ledger_state
                (entity_key, kind, lane, scope_to, opened_at, opened_row_id, projected_at)
            SELECT 'msg:' || substring(raw_row from 'id=(m[0-9]+)'),
                   'msg', 'orch', 'lane-t', row_ts, row_id, now()
            FROM {schema}.work_ledger_rows WHERE row_type = 'MSG';
            INSERT INTO {schema}.work_ledger_state
                (entity_key, kind, lane, repo, scope_to, opened_at, opened_row_id, projected_at)
            SELECT 'hold:' || row_id, 'hold', 'orch', 'r', 'lane-t', row_ts, row_id, now()
            FROM {schema}.work_ledger_rows WHERE row_type = 'HOLD';
            ANALYZE {schema}.work_ledger_rows;
            ANALYZE {schema}.work_ledger_state;
            """
        )
    cases = (
        (K.HOLDS, {}, 1.0, 60),
        (K.INBOX, {"lanes": ("lane-t",)}, 1.0, 500),
        (K.OPEN_CLAIMS, {}, 3.0, 500),
    )
    for kind, extra, bar, expected in cases:
        elapsed: list[float] = []
        for _ in range(100):
            started = time.perf_counter()
            reader = reader_for(schema)
            result = ask(
                HandlerWorkLedgerQuery(reader=reader, now=lambda: NOW), kind, **extra
            )
            elapsed.append(time.perf_counter() - started)
            assert result.status == "ok", result.error
            assert result.matched == expected
            reader.close()
        assert _percentile95(elapsed) <= bar, f"{kind.value} p95 over {bar}s"
