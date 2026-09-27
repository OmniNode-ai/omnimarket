# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres proof that delegation_events rows name their caller (OMN-19860).

The unit module proves the fold and the columns the writers hand the adapter.
It cannot prove that Postgres has the column or that the deployed async
writer's statement names it correctly. Here the node's whole migration chain is
applied to a throwaway PostgreSQL 16 schema, and both effect writers write
terminals with a lane and a session, without them, and with malformed ones.

THE RED HALF IS MECHANICAL. Before migration 0048 the table has no
``caller_lane`` column: ``test_the_migration_adds_the_column`` fails, and every
write naming the column raises UndefinedColumn.

WHICH DATABASE. When ``INTEGRATION_POSTGRES_PASSWORD`` is set (the CI
integration job's PostgreSQL 16 service), that server is used, with every test
in its own throwaway schema. Otherwise the module starts its own
localhost-only PostgreSQL 16 container, and skips when docker is absent.
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
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter
from omnimarket.projection.runner import MessageMeta

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parents[1]
_MIGRATIONS_DIR = (
    _ROOT / "src" / "omnimarket" / "nodes" / "node_projection_delegation" / "migrations"
)
_DOCKER = shutil.which("docker")


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


def _payload(correlation_id: str, **extra: object) -> dict[str, Any]:
    return {
        "status": "completed",
        "correlation_id": correlation_id,
        "task_type": "research",
        "tenant_id": "omninode",
        "metrics": {"cost_usd": 0.0},
        **extra,
    }


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
    database = "omn19860"
    container_id: str | None = None
    try:
        created = subprocess.run(
            [
                str(_DOCKER),
                "run",
                "--detach",
                "--rm",
                "--name",
                f"omn19860-pg-{uuid4().hex[:12]}",
                "--label",
                "omnimarket.omn19860=local-test",
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


@asynccontextmanager
async def _provisioned(pg: _Postgres) -> AsyncIterator[tuple[asyncpg.Connection, str]]:
    """Apply the node's whole migration chain into a throwaway schema."""
    admin = await asyncpg.connect(**pg.connect_kwargs())
    schema = f"omn19860_{uuid4().hex[:12]}"
    try:
        await admin.execute(_ROLES_SQL)
        await admin.execute(f"CREATE SCHEMA {schema}")
        await admin.execute(f"SET search_path TO {schema}, public")
        for migration in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            await admin.execute(
                migration.read_text(encoding="utf-8").replace(
                    "CREATE INDEX CONCURRENTLY", "CREATE INDEX"
                )
            )
        yield admin, schema
    finally:
        with contextlib.suppress(asyncpg.PostgresError):
            await admin.execute("SET search_path TO public")
            await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin.close()


@asynccontextmanager
async def _runner(
    pg: _Postgres, schema: str
) -> AsyncIterator[DelegationProjectionRunner]:
    pool = await asyncpg.create_pool(
        **pg.connect_kwargs(),
        min_size=1,
        max_size=2,
        server_settings={"search_path": f"{schema},public"},
    )
    try:
        adapter = AsyncpgAdapter(dsn="postgresql://unused")
        adapter._pool = pool  # type: ignore[attr-defined]
        runner = DelegationProjectionRunner()
        runner._db = adapter  # type: ignore[assignment]
        yield runner
    finally:
        await pool.close()


class _NullPublisher:
    """No broker here; the republish seam is not what this module proves."""

    def publish(self, *args: object, **kwargs: object) -> bool:
        return True


_LANE = "delegation-fix-delegation-events-no-caller-lane-83"


async def _stored_caller(
    admin: asyncpg.Connection, correlation_id: str
) -> tuple[Any, Any]:
    row = await admin.fetchrow(
        "SELECT caller_lane, session_id, task_type FROM delegation_events "
        "WHERE correlation_id = $1",
        correlation_id,
    )
    assert row is not None, f"no delegation_events row for {correlation_id}"
    assert row["task_type"] == "research"
    return row["caller_lane"], row["session_id"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_migration_adds_the_column(postgres: _Postgres) -> None:
    async with _provisioned(postgres) as (admin, schema):
        data_type = await admin.fetchval(
            "SELECT data_type FROM information_schema.columns WHERE table_schema = $1 "
            "AND table_name = 'delegation_events' AND column_name = 'caller_lane'",
            schema,
        )
        index = await admin.fetchval(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = $1 "
            "AND indexname = 'idx_delegation_events_caller_lane'",
            schema,
        )
    assert data_type == "text"
    assert index is not None
    assert "WHERE (caller_lane IS NOT NULL)" in index


@pytest.mark.integration
@pytest.mark.asyncio
async def test_deployed_async_writer_stores_keeps_and_refuses(
    postgres: _Postgres,
) -> None:
    attributed, malformed = str(uuid4()), str(uuid4())
    session = str(uuid4())
    async with (
        _provisioned(postgres) as (admin, schema),
        _runner(postgres, schema) as runner,
    ):
        meta = MessageMeta(partition=0, offset=1, fallback_id=attributed)
        assert await runner._project_delegate_skill_terminal(
            _payload(attributed, caller_lane=_LANE, session_id=session), meta
        )
        assert await _stored_caller(admin, attributed) == (_LANE, session)
        # The same correlation re-emitted with no lane: nothing is clobbered.
        await runner._project_delegate_skill_terminal(_payload(attributed), meta)
        assert (await _stored_caller(admin, attributed))[0] == _LANE
        # A malformed lane and session still write the delegation's own row.
        assert await runner._project_delegate_skill_terminal(
            _payload(malformed, caller_lane="two words", session_id="nope"),
            MessageMeta(partition=0, offset=2, fallback_id=malformed),
        )
        assert await _stored_caller(admin, malformed) == (None, None)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_local_sync_writer_stores_the_lane_and_session(
    postgres: _Postgres,
) -> None:
    corr = str(uuid4())
    session = str(uuid4())
    async with _provisioned(postgres) as (admin, schema):
        adapter = PostgresSyncProjectionAdapter(postgres.dsn(schema))
        try:
            payload: dict[str, object] = _payload(
                corr, caller_lane=_LANE, session_id=session
            )
            payload["_db"] = adapter
            HandlerProjectionDelegation(publisher=_NullPublisher()).handle(payload)
        finally:
            close = getattr(adapter, "close", None)
            if callable(close):
                close()
        assert await _stored_caller(admin, corr) == (_LANE, session)
