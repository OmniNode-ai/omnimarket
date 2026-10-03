# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The unit module tests/test_omn17427_terminal_required_bar_projection.py proves
the bound value; this module proves Postgres stores it in the NUMERIC(5,3)
column through the deployed async writer's statement.
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
from decimal import Decimal
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


async def _accepts_sql(pg: _Postgres) -> bool:
    try:
        connection = await asyncpg.connect(**pg.connect_kwargs(), timeout=1)
    except (OSError, asyncpg.PostgresError):
        return False
    try:
        return bool(await connection.fetchval("SELECT 1") == 1)
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
    if _DOCKER is None:
        pytest.skip("docker unavailable and INTEGRATION_POSTGRES_PASSWORD unset")
    database = "omn17427"
    container_id: str | None = None
    try:
        created = subprocess.run(
            [
                str(_DOCKER),
                "run",
                "--detach",
                "--rm",
                "--name",
                f"omn17427-pg-{uuid4().hex[:12]}",
                "--label",
                "omnimarket.omn17427=local-test",
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
    schema = f"omn17427_{uuid4().hex[:12]}"
    try:
        await admin.execute(_ROLES_SQL)
        await admin.execute(f"CREATE SCHEMA {schema}")
        await admin.execute(f"SET search_path TO {schema}, public")
        for migration in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            # Keep migration 0051's internal audit table in the disposable schema.
            await admin.execute(
                migration.read_text(encoding="utf-8")
                .replace("CREATE INDEX CONCURRENTLY", "CREATE INDEX")
                .replace("omninode_internal.", f"{schema}.")
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
        adapter._pool = pool
        runner = DelegationProjectionRunner()
        runner._db = adapter
        yield runner
    finally:
        await pool.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_column_is_numeric(postgres: _Postgres) -> None:
    async with _provisioned(postgres) as (admin, schema):
        data_type = await admin.fetchval(
            "SELECT data_type FROM information_schema.columns WHERE table_schema = $1 "
            "AND table_name = 'delegation_events' AND column_name = 'required_bar'",
            schema,
        )
    assert data_type == "numeric"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_async_writer_stores_the_declared_bar(postgres: _Postgres) -> None:
    cid = str(uuid4())
    payload: dict[str, Any] = {
        "status": "completed",
        "correlation_id": cid,
        "task_type": "document",
        "tenant_id": "omninode",
        "model_name": "Qwen3.8-27B",
        "provider": "local",
        "quality_gate_passed": True,
        "quality_score": 1.0,
        "required_quality_bar": 0.85,
        "score_vs_required_bar": "at_or_above_bar",
        "metrics": {"cost_usd": 0.0},
    }
    async with (
        _provisioned(postgres) as (admin, schema),
        _runner(postgres, schema) as runner,
    ):
        meta = MessageMeta(partition=0, offset=1, fallback_id=cid)
        assert await runner._project_delegate_skill_terminal(payload, meta)
        row = await admin.fetchrow(
            "SELECT required_bar, actual_score FROM delegation_events "
            "WHERE correlation_id = $1",
            cid,
        )
        assert row is not None
        assert row["required_bar"] == Decimal("0.850")
        assert row["actual_score"] is None

        del payload["required_quality_bar"]
        del payload["score_vs_required_bar"]
        assert await runner._project_delegate_skill_terminal(payload, meta)
        row = await admin.fetchrow(
            "SELECT required_bar, actual_score FROM delegation_events "
            "WHERE correlation_id = $1",
            cid,
        )
        assert row is not None
        assert row["required_bar"] == Decimal("0.850")
        assert row["actual_score"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unscored_terminal_stores_null(postgres: _Postgres) -> None:
    cid = str(uuid4())
    payload: dict[str, Any] = {
        "status": "failed",
        "correlation_id": cid,
        "task_type": "document",
        "tenant_id": "omninode",
        "model_name": "Qwen3.8-27B",
        "provider": "local",
        "quality_gate_passed": False,
        "quality_score": 0.0,
        "error_message": "inference_failed",
        "metrics": {"cost_usd": 0.0},
    }
    async with (
        _provisioned(postgres) as (admin, schema),
        _runner(postgres, schema) as runner,
    ):
        assert await runner._project_delegate_skill_terminal(
            payload, MessageMeta(partition=0, offset=1, fallback_id=cid)
        )
        row = await admin.fetchrow(
            "SELECT required_bar, actual_score FROM delegation_events "
            "WHERE correlation_id = $1",
            cid,
        )
        assert row is not None
        assert row["required_bar"] is None
        assert row["actual_score"] is None
