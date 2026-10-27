# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real PostgreSQL migration, first-ingest and read-only history proof.

Only an explicitly supplied loopback/Unix-socket test DSN is accepted. Every
test owns a disposable schema; no deployment database is read or mutated.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg2
import pytest
from psycopg2.extensions import make_dsn, parse_dsn

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.models.work_ledger_query import ModelWorkLedgerQueryRequest
from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_projection import (
    _INSERT_ROW,
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
ENV = "ONEX_TEST_LEDGER_HISTORY_DSN"


def _test_dsn() -> str | None:
    dsn = os.environ.get(ENV)
    if dsn:
        return dsn
    # The hosted Tests job exports its ephemeral PostgreSQL 16 service as
    # INTEGRATION_POSTGRES_*; use it so CI executes this proof instead of skipping.
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


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, str]]:
    dsn = _test_dsn()
    if not dsn:
        pytest.skip(f"{ENV} must name an ephemeral local PostgreSQL database")
    # The production reader resolves its DSN from this variable.
    monkeypatch.setenv(ENV, dsn)
    host = parse_dsn(dsn).get("host", "")
    if host not in {"127.0.0.1", "::1", "localhost"} and not host.startswith("/"):
        pytest.fail(
            "history migration proof requires an explicit loopback or Unix socket host"
        )
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    schema = f"ledger_history_{uuid4().hex}"
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


def migrate(conn: Any, schema: str) -> None:
    with conn.cursor() as cursor:
        cursor.execute(
            (MIGRATIONS / "0006_work_ledger_ingested_at.sql")
            .read_text()
            .replace("omninode_internal.", f"{schema}.")
        )


def insert(
    conn: Any, schema: str, seq: int, *, ledger_id: str = "rolling-work-ledger"
) -> str:
    # A future writer stamp cannot move the ingest cutoff or canonical order.
    raw = f"2027-01-{5 - seq:02d}T00:00:00Z | CLAIM | lane=member | run=run-a | principal=dev-member | ledger={ledger_id}"
    sql = re.sub(r"\$\d+", "%s", _INSERT_ROW).replace(
        "omninode_internal.", f"{schema}."
    )
    with conn.cursor() as cursor:
        cursor.execute(
            sql,
            (
                work_ledger_row_id(raw),
                ledger_id,
                datetime(2027, 1, 5 - seq, tzinfo=UTC),
                "CLAIM",
                "member",
                "[]",
                raw,
                f"archive/2026-10-{23 if seq <= 2 else 24}",
                datetime.now(UTC),
                seq,
            ),
        )
    return raw


def reader(schema: str) -> PostgresWorkLedgerQueryReader:
    return PostgresWorkLedgerQueryReader(
        ModelWorkLedgerQuerySource(
            relation=f"{schema}.work_ledger_rows",
            dsn_env=ENV,
            parity_receipt_lane="work-ledger-parity",
            binding_ref="ephemeral-test",
        )
    )


def test_postgres_history_migration_preserves_unknown_legacy_time_and_first_ingest(
    database,
) -> None:
    conn, schema = database
    insert(conn, schema, 1)
    migrate(conn, schema)
    migrate(conn, schema)
    insert(conn, schema, 2)
    with conn.cursor() as cursor:
        cursor.execute(
            f"SELECT ledger_seq, ingested_at FROM {schema}.work_ledger_rows ORDER BY ledger_seq"
        )
        old, new = cursor.fetchall()
    assert old == (1, None)
    assert new[1] is not None
    insert(conn, schema, 2)
    with conn.cursor() as cursor:
        cursor.execute(
            f"SELECT ingested_at FROM {schema}.work_ledger_rows WHERE ledger_seq=2"
        )
        assert cursor.fetchone()[0] == new[1]


def test_postgres_history_returns_both_sources_and_rejects_truncation_and_gaps(
    database,
) -> None:
    conn, schema = database
    migrate(conn, schema)
    expected = {seq: insert(conn, schema, seq) for seq in (4, 2, 1, 3)}
    insert(conn, schema, 1, ledger_id="another-ledger")
    request = ModelWorkLedgerQueryRequest.model_validate(
        {
            "correlation_id": str(uuid4()),
            "query": "rows",
            "until": datetime.now(UTC) + timedelta(seconds=1),
            "history": {
                "ledger_id": "rolling-work-ledger",
                "first_seq": 1,
                "last_seq": 4,
                "sources": ["archive/2026-10-23", "archive/2026-10-24"],
            },
        }
    )
    source = reader(schema)
    try:
        node = HandlerWorkLedgerQuery(reader=source)
        result = node.handle(request)
        assert result.error is None
        assert [row.raw_row for row in result.rows] == [
            expected[i] for i in range(1, 5)
        ]
        assert result.completeness.status == "complete"
        assert result.completeness.rows_read == 4
        assert all(row.ingested_at is not None for row in result.rows)
        truncated = node.handle(request.model_copy(update={"limit": 2}))
        assert truncated.completeness.status == "incomplete"
        assert "truncated" in truncated.completeness.reasons
        with conn.cursor() as cursor:
            cursor.execute(f"DELETE FROM {schema}.work_ledger_rows WHERE ledger_seq=2")
        gap = node.handle(request)
        assert gap.completeness.status == "incomplete"
        assert "sequence-gap" in gap.completeness.reasons
        # The production reader really holds a read-only connection.
        with source._connect().cursor() as cursor:
            cursor.execute("SHOW transaction_read_only")
            assert cursor.fetchone()[0] == "on"
    finally:
        source.close()


def test_history_database_accepts_ci_service_environment(monkeypatch) -> None:
    monkeypatch.delenv(ENV, raising=False)
    for key, value in {
        "HOST": "127.0.0.1",
        "PORT": "15432",
        "USER": "postgres",
        "PASSWORD": "fixture-password",
        "DB": "ephemeral",
    }.items():
        monkeypatch.setenv(f"INTEGRATION_POSTGRES_{key}", value)
    resolved = parse_dsn(_test_dsn())
    assert resolved["host"] == "127.0.0.1"
    assert resolved["port"] == "15432"
    assert resolved["dbname"] == "ephemeral"
    assert resolved["password"] == "fixture-password"


def test_history_database_prefers_explicit_probe_environment(monkeypatch) -> None:
    monkeypatch.setenv(ENV, "host=/tmp/fixture dbname=probe")
    monkeypatch.setenv("INTEGRATION_POSTGRES_HOST", "127.0.0.1")
    assert parse_dsn(_test_dsn())["host"] == "/tmp/fixture"


def test_history_database_never_uses_ambient_runtime_dsn(monkeypatch) -> None:
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.delenv("INTEGRATION_POSTGRES_HOST", raising=False)
    monkeypatch.setenv("OMNINODE_INTERNAL_DB_URL", "postgresql://runtime/forbidden")
    assert _test_dsn() is None
