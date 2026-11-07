# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres proof of the per-day view and the walk cursor (OMN-21970).

The SQLite module proves the local store. It cannot prove that node
projection_delegation's migrations 0065 and 0066 apply on top of the whole
chain, that adding the cursor numbers the rows a live table already holds,
that a later insert takes the next number, or that the per-day view and the
decisions walk serve every run through the same table reader the dashboard's
API uses on Postgres. Each test names the failure it exists to catch:

* rows written before migration 0065 keep a NULL cursor, so a walk skips them;
* a row inserted after it has no cursor, or a duplicate one;
* the per-day view stops short of 600 runs over 20 days, puts a run on the
  wrong day, or counts failed and unrecorded runs differently from the
  dashboard;
* another tenant's days are served;
* the decisions walk cannot reach the runs older than the newest 500.

THE RED HALF IS MECHANICAL. Before migrations 0065 and 0066 there is no
``projection_cursor`` column and no ``projection_delegation_daily`` relation.

WHICH DATABASE. As in the OMN-19970 module: CI's provisioned PostgreSQL 16 when
``INTEGRATION_POSTGRES_PASSWORD`` is set, otherwise an owned localhost-only
container, skipped when docker is absent.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.models import (
    ModelProjectionReadRequest,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.table_reader import TableRowSource

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parents[1]
_MIGRATIONS_DIR = (
    _ROOT / "src" / "omnimarket" / "nodes" / "node_projection_delegation" / "migrations"
)
_CURSOR_MIGRATION = "0065_delegation_events_projection_cursor.sql"
_DOCKER = shutil.which("docker")
_DAILY = "onex.snapshot.projection.delegation.daily.v1"
_DECISIONS = "onex.snapshot.projection.delegation.decisions.v1"
_TENANT = UUID("0af28cd0-0654-42e0-a050-358ee8240940")
_OTHER = UUID("11111111-2222-4333-8444-555555555555")
_FIRST_DAY = datetime(2026, 10, 1, tzinfo=UTC)

_ROLES_SQL = """
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_dashboard') THEN
    CREATE ROLE app_dashboard WITH NOLOGIN NOSUPERUSER NOBYPASSRLS;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tenant_projection_writer') THEN
    CREATE ROLE tenant_projection_writer WITH NOLOGIN NOSUPERUSER NOBYPASSRLS;
  END IF;
END$$;
"""


@dataclass(frozen=True)
class _Postgres:
    host: str
    port: int
    database: str
    user: str = "postgres"
    password: str = ""

    def connect_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "user": self.user,
            "database": self.database,
            "host": self.host,
            "port": self.port,
            "ssl": False,
        }
        if self.password:
            kwargs["password"] = self.password
        return kwargs

    def dsn(self, schema: str) -> str:
        options = quote(f"-c search_path={schema},public")
        auth = quote_plus(self.user)
        if self.password:
            auth += ":" + quote_plus(self.password)
        return (
            f"postgresql://{auth}@{self.host}:{self.port}/{self.database}"
            f"?options={options}"
        )


async def _accepts_sql(pg: _Postgres) -> bool:
    try:
        connection = await asyncpg.connect(**pg.connect_kwargs(), timeout=1)
    except (OSError, asyncpg.PostgresError):
        return False
    try:
        return await connection.fetchval("SELECT 1") == 1
    finally:
        await connection.close()


@pytest.fixture(scope="module")
def postgres() -> Iterator[_Postgres]:
    """CI's provisioned PostgreSQL 16, else an owned localhost-only container."""
    if os.environ.get("INTEGRATION_POSTGRES_PASSWORD"):
        yield _Postgres(
            host=os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost"),
            port=int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")),
            database=os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra"),
            user=os.environ.get("INTEGRATION_POSTGRES_USER", "postgres"),
            password=os.environ["INTEGRATION_POSTGRES_PASSWORD"],
        )
        return
    if _DOCKER is None:  # pragma: no cover - host dependent
        pytest.skip("docker unavailable and INTEGRATION_POSTGRES_PASSWORD unset")
    database = "omn21970"
    container_id: str | None = None
    try:
        created = subprocess.run(
            [
                str(_DOCKER),
                "run",
                "--detach",
                "--rm",
                "--name",
                f"omn21970-pg-{uuid4().hex[:12]}",
                "--label",
                "omnimarket.omn21970=local-test",
                "--env",
                "POSTGRES_HOST_AUTH_METHOD=trust",
                "--env",
                f"POSTGRES_DB={database}",
                "--publish",
                "127.0.0.1::5432",
                "postgres:16-alpine",
            ],
            check=True,
            capture_output=True,
        )
        container_id = created.stdout.decode("utf-8").strip()
        published = subprocess.run(
            [str(_DOCKER), "port", container_id, "5432/tcp"],
            check=True,
            capture_output=True,
        ).stdout.decode("utf-8")
        host, port = published.strip().splitlines()[0].rsplit(":", maxsplit=1)
        pg = _Postgres(host=host, port=int(port), database=database)
        for _ in range(150):
            if asyncio.run(_accepts_sql(pg)):
                break
            time.sleep(0.1)
        else:
            msg = "owned PostgreSQL 16 endpoint did not accept SELECT 1"
            raise RuntimeError(msg)
        yield pg
    finally:
        if container_id is not None:
            subprocess.run(
                [str(_DOCKER), "rm", "--force", container_id],
                check=False,
                capture_output=True,
            )


async def _apply(admin: asyncpg.Connection, schema: str, migration: Path) -> None:
    await admin.execute(
        migration.read_text(encoding="utf-8")
        .replace("CREATE INDEX CONCURRENTLY", "CREATE INDEX")
        .replace("omninode_internal.", f"{schema}.")
        .replace("public.", f"{schema}.")
    )


@asynccontextmanager
async def _provisioned(
    pg: _Postgres, *, stop_before: str | None = None
) -> AsyncIterator[tuple[asyncpg.Connection, str]]:
    """Apply the node's migration chain into a throwaway schema, up to
    ``stop_before`` when given (that migration and later are left unapplied)."""
    admin = await asyncpg.connect(**pg.connect_kwargs())
    schema = f"omn21970_{uuid4().hex[:12]}"
    try:
        await admin.execute(_ROLES_SQL)
        await admin.execute(f"CREATE SCHEMA {schema}")
        await admin.execute(f"SET search_path TO {schema}, public")
        registry_migration = (
            _MIGRATIONS_DIR.parents[1]
            / "node_projection_tenant_registry"
            / "migrations"
            / "0000_create_tenant_registry_mirror.sql"
        )
        await admin.execute(registry_migration.read_text(encoding="utf-8"))
        for migration in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            if stop_before is not None and migration.name >= stop_before:
                break
            await _apply(admin, schema, migration)
        yield admin, schema
    finally:
        with contextlib.suppress(asyncpg.PostgresError):
            await admin.execute("SET search_path TO public")
            await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin.close()


async def _insert(
    admin: asyncpg.Connection,
    correlation_id: str,
    at: datetime,
    *,
    tenant: UUID = _TENANT,
    terminal_ok: bool | None = True,
    gate: bool | None = True,
) -> None:
    await admin.execute(
        "INSERT INTO delegation_events (correlation_id, tenant_id, task_type, "
        "delegated_to, model_name, quality_gate_passed, terminal_ok, "
        "tokens_input, tokens_output, created_at, written_at) "
        "VALUES ($1, $2, 'reasoning', 'qwen3.8-27b', 'qwen3.8-27b', $3, $4, "
        "100, 10, $5, $5)",
        correlation_id,
        tenant,
        gate,
        terminal_ok,
        at,
    )


async def _six_hundred_runs(admin: asyncpg.Connection) -> None:
    for day in range(20):
        at = _FIRST_DAY + timedelta(days=day, hours=23, minutes=30)
        for n in range(30):
            await _insert(admin, f"r{day:02d}-{n:02d}", at + timedelta(seconds=n))


def _topics(schema: str) -> dict[str, Any]:
    return {
        topic: cfg.model_copy(update={"relation_schema": schema})
        for topic, cfg in build_projection_topic_map().items()
        if topic in {_DAILY, _DECISIONS}
    }


async def _read(
    pg: _Postgres,
    schema: str,
    topic: str,
    *,
    tenant: UUID = _TENANT,
    since: str | None = None,
) -> dict[str, Any]:
    source = TableRowSource.for_database_url(pg.dsn(schema))
    try:
        result = await HandlerProjectionRead(
            topic_map=_topics(schema), row_source=source
        ).handle(
            ModelProjectionReadRequest(topic=topic, tenant_id=str(tenant), since=since)
        )
    finally:
        await source.close()
    assert result.ok, result.response
    return {"rows": result.rows, "next_cursor": result.next_cursor}


@pytest.mark.asyncio
async def test_the_cursor_numbers_existing_rows_and_each_later_insert(
    postgres: _Postgres,
) -> None:
    async with _provisioned(postgres, stop_before=_CURSOR_MIGRATION) as (
        admin,
        schema,
    ):
        await _insert(admin, "before-1", _FIRST_DAY)
        await _insert(admin, "before-2", _FIRST_DAY)
        for migration in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            if migration.name >= _CURSOR_MIGRATION:
                await _apply(admin, schema, migration)
        await _insert(admin, "after", _FIRST_DAY)
        rows = await admin.fetch(
            "SELECT correlation_id, projection_cursor FROM delegation_events "
            "ORDER BY projection_cursor"
        )
    cursors = [row["projection_cursor"] for row in rows]
    assert None not in cursors
    assert len(set(cursors)) == 3
    assert rows[-1]["correlation_id"] == "after"


@pytest.mark.asyncio
async def test_the_per_day_view_counts_every_run_on_its_utc_day(
    postgres: _Postgres,
) -> None:
    async with _provisioned(postgres) as (admin, schema):
        await _six_hundred_runs(admin)
        page = await _read(postgres, schema, _DAILY)
    assert len(page["rows"]) == 1
    days = page["rows"][0]["days"]
    assert [day["day"] for day in days] == [
        (_FIRST_DAY + timedelta(days=d)).date().isoformat() for d in range(20)
    ]
    assert all(day["runs"] == 30 for day in days)
    assert sum(day["tokensIn"] for day in days) == 60_000
    assert page["rows"][0]["total_runs"] == 600


@pytest.mark.asyncio
async def test_the_per_day_verdicts_match_the_dashboard_and_stay_in_tenant(
    postgres: _Postgres,
) -> None:
    async with _provisioned(postgres) as (admin, schema):
        await _insert(admin, "passed", _FIRST_DAY)
        await _insert(admin, "gate-failed", _FIRST_DAY, gate=False)
        await _insert(admin, "terminal-failed", _FIRST_DAY, terminal_ok=False)
        # quality_gate_passed is NOT NULL here, so an unrecorded run is a
        # local-store case only (the SQLite module covers it).
        await _insert(admin, "terminal-unknown", _FIRST_DAY, terminal_ok=None)
        await _insert(admin, "theirs", _FIRST_DAY, tenant=_OTHER)
        mine = await _read(postgres, schema, _DAILY)
        theirs = await _read(postgres, schema, _DAILY, tenant=_OTHER)
    day = mine["rows"][0]["days"][0]
    assert (day["runs"], day["passed"], day["failed"], day["unrecorded"]) == (
        4,
        2,
        2,
        0,
    )
    assert theirs["rows"][0]["days"][0]["runs"] == 1


@pytest.mark.asyncio
async def test_the_decisions_walk_reaches_every_run(postgres: _Postgres) -> None:
    async with _provisioned(postgres) as (admin, schema):
        await _six_hundred_runs(admin)
        page = await _read(postgres, schema, _DECISIONS)
        assert len(page["rows"]) == 500
        seen = {row["correlation_id"] for row in page["rows"]}
        cursor = page["next_cursor"]
        for _ in range(10):
            if cursor is None:
                break
            page = await _read(postgres, schema, _DECISIONS, since=cursor)
            seen |= {row["correlation_id"] for row in page["rows"]}
            cursor = page["next_cursor"]
    assert len(seen) == 600
