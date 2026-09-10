# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18147: characterize the canonical terminal UPSERT under FORCE RLS.

The retained staging DLQ has a ``delegation-completed`` failure attributed to
the policy's ``USING`` arm.  This module deliberately does not guess that the
terminal event carried a different tenant from the existing correlation row.
It proves the two distinguishable cases against a disposable, real-Postgres
schema and the established non-superuser writer fixture:

* a cross-tenant same-correlation UPSERT is refused by ``USING``; and
* the canonical terminal path succeeds when its producer tenant agrees with
  the existing row.

That leaves a failure of the latter as evidence of a source defect, while a
green result makes the retained wire/deployment shape the next fact to recover.
Neither case weakens RLS or contacts staging.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_SLUG
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _real_delegation_completed_payload,
)
from tests.test_omn17422_quality_gate_tenant_scope_binding import (
    _BETA_TENANT_SLUG,
    _BETA_TENANT_UUID,
    _seed_beta_delegation_row,
)

_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation"
    / "migrations"
)
_DOCKER = shutil.which("docker")

if _DOCKER is None:  # pragma: no cover - host dependent
    pytest.skip(
        "docker unavailable — cannot run the hermetic PostgreSQL 16 OMN-18147 test",
        allow_module_level=True,
    )

_APP_DASHBOARD_ROLE_SQL = """
DO $$
BEGIN
  BEGIN
    CREATE ROLE app_dashboard WITH
      NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
  EXCEPTION
    WHEN duplicate_object OR unique_violation THEN
      NULL;
  END;
END;
$$;
"""


@dataclass(frozen=True)
class _LocalPostgres:
    host: str
    port: int
    database: str


async def _endpoint_accepts_sql(local_postgres: _LocalPostgres) -> bool:
    """Check the published local endpoint, not only container-internal readiness."""
    try:
        connection = await asyncpg.connect(
            user="postgres",
            database=local_postgres.database,
            host=local_postgres.host,
            port=local_postgres.port,
            ssl=False,
            timeout=1,
        )
    except (OSError, asyncpg.PostgresError):
        return False
    try:
        return await connection.fetchval("SELECT 1") == 1
    finally:
        await connection.close()


@pytest.fixture(scope="module")
def local_postgres() -> Iterator[_LocalPostgres]:
    """Owned localhost-only PostgreSQL 16 container, never a shared database."""
    container_name = f"omn18147-pg-{uuid4().hex[:12]}"
    database = "omn18147"
    container_id: str | None = None
    try:
        created = subprocess.run(
            [
                str(_DOCKER),
                "run",
                "--detach",
                "--rm",
                "--name",
                container_name,
                "--label",
                "omnimarket.omn18147=local-test",
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
        port_result = subprocess.run(
            [str(_DOCKER), "port", container_id, "5432/tcp"],
            check=True,
            capture_output=True,
        )
        published = port_result.stdout.decode("utf-8").strip()
        host, port = published.rsplit(":", maxsplit=1)
        assert host == "127.0.0.1"
        local_postgres = _LocalPostgres(host=host, port=int(port), database=database)
        for _ in range(100):
            if asyncio.run(_endpoint_accepts_sql(local_postgres)):
                break
            time.sleep(0.1)
        else:
            msg = "owned PostgreSQL 16 published endpoint did not accept SELECT 1"
            raise RuntimeError(msg)
        yield local_postgres
    finally:
        if container_id is not None:
            subprocess.run(
                [str(_DOCKER), "rm", "--force", container_id],
                check=False,
                capture_output=True,
            )


@asynccontextmanager
async def _rls_enforced_local_runner(
    local_postgres: _LocalPostgres,
) -> AsyncIterator[tuple[DelegationProjectionRunner, asyncpg.Connection, str, str]]:
    """Apply real migrations and yield an owned NOBYPASSRLS writer runner."""
    admin = await asyncpg.connect(
        user="postgres",
        database=local_postgres.database,
        host=local_postgres.host,
        port=local_postgres.port,
        ssl=False,
    )
    suffix = uuid4().hex[:12]
    schema = f"omn18147_{suffix}"
    writer_role = f"omn18147_writer_{suffix}"
    pool: asyncpg.Pool | None = None
    try:
        await admin.execute(f"CREATE SCHEMA {schema}")
        await admin.execute(f"SET search_path TO {schema}, public")
        await admin.execute(_APP_DASHBOARD_ROLE_SQL)
        for migration in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            await admin.execute(
                migration.read_text(encoding="utf-8").replace(
                    "CREATE INDEX CONCURRENTLY", "CREATE INDEX"
                )
            )
        await admin.execute(
            f"CREATE ROLE {writer_role} LOGIN NOSUPERUSER NOBYPASSRLS "
            "NOCREATEDB NOCREATEROLE NOREPLICATION"
        )
        await admin.execute(f"GRANT USAGE ON SCHEMA {schema} TO {writer_role}")
        await admin.execute(
            f"GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA {schema} "
            f"TO {writer_role}"
        )
        pool = await asyncpg.create_pool(
            user=writer_role,
            database=local_postgres.database,
            host=local_postgres.host,
            port=local_postgres.port,
            ssl=False,
            min_size=1,
            max_size=2,
            server_settings={"search_path": f"{schema},public"},
        )
        adapter = AsyncpgAdapter(dsn="postgresql://unused")
        adapter._pool = pool  # type: ignore[attr-defined]
        runner = DelegationProjectionRunner()
        runner._db = adapter  # type: ignore[assignment]
        yield runner, admin, schema, writer_role
    finally:
        if pool is not None:
            await pool.close()
        with contextlib.suppress(asyncpg.PostgresError):
            await admin.execute("SET search_path TO public")
            await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            await admin.execute(f"DROP ROLE IF EXISTS {writer_role}")
        await admin.close()


async def _assert_force_rls_premises(
    admin: asyncpg.Connection, schema: str, writer_role: str
) -> None:
    """Prove the disposable fixture is a non-bypass writer under FORCE RLS."""
    role = await admin.fetchrow(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = $1", writer_role
    )
    relation = await admin.fetchrow(
        "SELECT c.relrowsecurity, c.relforcerowsecurity "
        "FROM pg_class AS c "
        "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
        "WHERE n.nspname = $1 AND c.relname = 'delegation_events'",
        schema,
    )
    assert role is not None
    assert role["rolsuper"] is False
    assert role["rolbypassrls"] is False
    assert relation is not None
    assert relation["relrowsecurity"] is True
    assert relation["relforcerowsecurity"] is True


@pytest.mark.integration
@pytest.mark.asyncio
class TestDelegationCompletedSameCorrelationUnderForceRls:
    async def test_red_cross_tenant_terminal_update_is_refused_by_policy(
        self, local_postgres: _LocalPostgres
    ) -> None:
        """A valid but different producer tenant cannot update beta's row.

        This is the required policy positive control.  It demonstrates the
        exact ``USING`` boundary without claiming the retained event had this
        shape; the green control below is the canonical same-tenant path.
        """
        async with _rls_enforced_local_runner(local_postgres) as (
            runner,
            admin,
            schema,
            writer_role,
        ):
            await _assert_force_rls_premises(admin, schema, writer_role)
            correlation_id = str(uuid4())
            await _seed_beta_delegation_row(admin, correlation_id)
            payload = _real_delegation_completed_payload(
                correlation_id=correlation_id,
                tenant_id=HOUSE_TENANT_SLUG,
            )

            with pytest.raises(asyncpg.PostgresError) as excinfo:
                await runner._project_delegation_terminal_result(
                    payload,
                    MessageMeta(partition=0, offset=267, fallback_id=correlation_id),
                )

            assert excinfo.value.sqlstate == "42501"
            assert "row-level security policy" in str(excinfo.value)

    async def test_green_same_tenant_terminal_update_preserves_scope(
        self, local_postgres: _LocalPostgres
    ) -> None:
        """The canonical terminal can update its own correlation under RLS."""
        async with _rls_enforced_local_runner(local_postgres) as (
            runner,
            admin,
            schema,
            writer_role,
        ):
            await _assert_force_rls_premises(admin, schema, writer_role)
            correlation_id = str(uuid4())
            await _seed_beta_delegation_row(admin, correlation_id)
            payload = _real_delegation_completed_payload(
                correlation_id=correlation_id,
                tenant_id=_BETA_TENANT_SLUG,
            )

            projected = await runner._project_delegation_terminal_result(
                payload,
                MessageMeta(partition=0, offset=267, fallback_id=correlation_id),
            )

            assert projected is True
            async with admin.transaction():
                await admin.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", _BETA_TENANT_UUID
                )
                row = await admin.fetchrow(
                    "SELECT tenant_id, quality_gate_passed FROM delegation_events "
                    "WHERE correlation_id = $1",
                    correlation_id,
                )
            assert row is not None
            assert str(row["tenant_id"]) == _BETA_TENANT_UUID
            assert row["quality_gate_passed"] is True
