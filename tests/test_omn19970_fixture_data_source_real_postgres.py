# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres proof of the fixture label and the savings split (OMN-19970).

The unit module proves the writers name ``data_source`` and that ``onex
metering`` skips fixtures. It cannot prove that Postgres has the column and its
check, that the deployed async writer reads the envelope tag, or that the
Overview's summary view keeps fixture savings out of the measured total. Here
the node's whole migration chain is applied to a throwaway PostgreSQL 16
schema, and the runner writes real and seeded terminals.

THE RED HALF IS MECHANICAL. Before migrations 0049 and 0050 there is no
``data_source`` column and no ``fixtureSavingsUsd``, so every test here fails.

WHICH DATABASE. As in the OMN-19860 module: CI's provisioned PostgreSQL 16 when
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
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_dev_seed_effect.handlers.handler_dev_seed import (
    HandlerDevSeed,
)
from omnimarket.nodes.node_dev_seed_effect.models.model_dev_seed_request import (
    ModelDevSeedRequest,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.models import (
    ModelProjectionReadRequest,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.envelope import unwrap_envelope
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.table_reader import TableRowSource
from tests.helpers.tenant_registry import (
    PROJECTION_TENANT_SLUG,
    PROJECTION_TENANT_UUID,
)

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parents[1]
_MIGRATIONS_DIR = (
    _ROOT / "src" / "omnimarket" / "nodes" / "node_projection_delegation" / "migrations"
)
_DOCKER = shutil.which("docker")
_TENANT = PROJECTION_TENANT_SLUG


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
    database = "omn19970"
    container_id: str | None = None
    try:
        created = subprocess.run(
            [
                str(_DOCKER),
                "run",
                "--detach",
                "--rm",
                "--name",
                f"omn19970-pg-{uuid4().hex[:12]}",
                "--label",
                "omnimarket.omn19970=local-test",
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
    schema = f"omn19970_{uuid4().hex[:12]}"
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
        await admin.execute(
            "INSERT INTO tenant_registry_mirror "
            "(tenant_slug, tenant_uuid, status) VALUES ($1, $2, 'active')",
            _TENANT,
            PROJECTION_TENANT_UUID,
        )
        for migration in sorted(_MIGRATIONS_DIR.glob("*.sql")):
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
        adapter._pool = pool  # type: ignore[attr-defined]
        runner = DelegationProjectionRunner()
        runner._db = adapter  # type: ignore[assignment]
        yield runner
    finally:
        await pool.close()


def _payload(correlation_id: str, savings: float, **extra: object) -> dict[str, Any]:
    return {
        "status": "completed",
        "correlation_id": correlation_id,
        "task_type": "document",
        "tenant_id": _TENANT,
        "quality_gate_passed": True,
        "metrics": {"cost_usd": 0.0, "cost_savings_usd": savings},
        **extra,
    }


def _tagged(payload: dict[str, Any], data_source: str) -> dict[str, Any]:
    return {
        **payload,
        "_envelope": {"metadata": {"tags": {"data_source": data_source}}},
    }


async def _source(admin: asyncpg.Connection, correlation_id: str) -> Any:
    return await admin.fetchval(
        "SELECT data_source FROM delegation_events WHERE correlation_id = $1",
        correlation_id,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_migration_adds_a_checked_column_defaulting_to_real(
    postgres: _Postgres,
) -> None:
    async with _provisioned(postgres) as (admin, schema):
        column = await admin.fetchrow(
            "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = $1 AND table_name = 'delegation_events' "
            "AND column_name = 'data_source'",
            schema,
        )
        assert column is not None
        assert column["data_type"] == "text"
        assert column["is_nullable"] == "NO"
        assert "'real'" in column["column_default"]
        constraint = await admin.fetchval(
            "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = $1 AND c.conname = 'delegation_events_data_source_check'",
            schema,
        )
        assert constraint is not None
        assert "'real'" in constraint
        assert "'fixture'" in constraint


@pytest.mark.integration
@pytest.mark.asyncio
async def test_runner_labels_from_the_envelope_tag_and_defaults_to_real(
    postgres: _Postgres,
) -> None:
    fixture, real, bogus = str(uuid4()), str(uuid4()), str(uuid4())
    async with (
        _provisioned(postgres) as (admin, schema),
        _runner(postgres, schema) as runner,
    ):
        cases = (
            (fixture, _tagged(_payload(fixture, 1.0), "fixture")),
            (real, _payload(real, 1.0)),
            (bogus, _tagged(_payload(bogus, 1.0), "bogus")),
        )
        for n, (corr, data) in enumerate(cases):
            assert await runner._project_delegate_skill_terminal(
                data, MessageMeta(partition=0, offset=n, fallback_id=corr)
            )
        assert await _source(admin, fixture) == "fixture"
        assert await _source(admin, real) == "real"
        assert await _source(admin, bogus) == "real"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_summary_view_keeps_fixture_savings_out_of_the_measured_total(
    postgres: _Postgres,
) -> None:
    real, fixture = str(uuid4()), str(uuid4())
    async with (
        _provisioned(postgres) as (admin, schema),
        _runner(postgres, schema) as runner,
    ):
        await runner._project_delegate_skill_terminal(
            _payload(real, 0.25), MessageMeta(partition=0, offset=1, fallback_id=real)
        )
        await runner._project_delegate_skill_terminal(
            _tagged(_payload(fixture, 5.0), "fixture"),
            MessageMeta(partition=0, offset=2, fallback_id=fixture),
        )
        row = await admin.fetchrow(
            'SELECT "totalDelegations", "totalSavingsUsd", "fixtureSavingsUsd", '
            '"fixtureDelegations" FROM projection_delegation_summary'
        )
        options = await admin.fetchval(
            "SELECT reloptions FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = $1 AND c.relname = 'projection_delegation_summary'",
            schema,
        )
    assert row is not None
    assert row["totalDelegations"] == 2
    assert row["totalSavingsUsd"] == pytest.approx(0.25)
    assert row["fixtureSavingsUsd"] == pytest.approx(5.0)
    assert row["fixtureDelegations"] == 1
    # 0050 re-creates the view, which drops security_invoker unless it is set again.
    assert options is not None
    assert "security_invoker=true" in options


@pytest.mark.integration
@pytest.mark.asyncio
async def test_seed_wire_messages_land_labelled_through_the_runner_seam(
    postgres: _Postgres,
) -> None:
    messages = HandlerDevSeed().wire_messages(ModelDevSeedRequest(tenant_id=_TENANT))
    async with (
        _provisioned(postgres) as (admin, schema),
        _runner(postgres, schema) as runner,
    ):
        counts = []
        for seed_run in range(2):
            for n, (_key, value) in enumerate(messages):
                data = unwrap_envelope(value)
                assert data is not None
                corr = str(data["correlation_id"])
                assert await runner._project_delegate_skill_terminal(
                    data,
                    MessageMeta(
                        partition=0,
                        offset=seed_run * len(messages) + n,
                        fallback_id=corr,
                    ),
                )
            counts.append(
                await admin.fetchval("SELECT COUNT(*) FROM delegation_events")
            )
        assert counts == [len(messages), len(messages)]
        sources = await admin.fetch(
            "SELECT data_source, COUNT(*) AS n FROM delegation_events GROUP BY data_source"
        )
        # Read through the same node and table adapter the dashboard uses.
        topics = {
            topic: cfg.model_copy(update={"relation_schema": schema})
            for topic, cfg in build_projection_topic_map().items()
            if cfg.table in {"delegation_events", "projection_delegation_summary"}
            and cfg.bus_backed
        }
        source = TableRowSource.for_database_url(postgres.dsn(schema))
        read = HandlerProjectionRead(topic_map=topics, row_source=source)
        try:
            for topic, cfg in topics.items():
                result = await read.handle(
                    ModelProjectionReadRequest(
                        topic=topic, tenant_id=str(PROJECTION_TENANT_UUID)
                    )
                )
                assert result.ok, result.response
                assert result.row_count > 0
                if cfg.table == "delegation_events":
                    assert result.row_count == len(messages)
                    assert {r.get("data_source") for r in result.rows} == {"fixture"}
                else:
                    assert result.rows[0]["fixtureDelegations"] == len(messages)
                    assert result.rows[0]["totalSavingsUsd"] == 0
        finally:
            await source.close()
    assert {r["data_source"]: r["n"] for r in sources} == {"fixture": len(messages)}
