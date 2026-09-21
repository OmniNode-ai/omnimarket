"""OMN-18987: 0043z stays a lexical, transactional predecessor to frozen 0044."""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
import pytest

_MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation"
    / "migrations"
)
_PRECHECK = _MIGRATIONS / "0043z_preflight_delegation_shadow_comparisons.sql"
_FROZEN = _MIGRATIONS / "0044_restore_delegation_shadow_comparisons.sql"
_FROZEN_SHA256 = "3a1089294056fafeebbe5fdbe1c0910d3dc178b37d9402e2f37c19a32161c298"
_PRECHECK_SHA256 = "3652f86ca33af7999d1f1b2f5c1d4d54644ff0bf342c2c5a4664f0df0d5a0700"
_LEDGER_VERSION = (
    "node:node_projection_delegation:0044_restore_delegation_shadow_comparisons.sql"
)
_TENANT = UUID("7527359e-3c87-53fd-a0ae-09fb9c2fe82d")


def _dsn() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    database = os.environ.get("INTEGRATION_POSTGRES_DB", "postgres")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


@asynccontextmanager
async def _database() -> AsyncIterator[asyncpg.Connection]:
    if not os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    ):
        pytest.skip("POSTGRES_PASSWORD is not set")
    try:
        conn = await asyncpg.connect(_dsn())
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"isolated PostgreSQL is unavailable: {exc}")
    try:
        await conn.execute(
            "DROP TABLE IF EXISTS public.delegation_shadow_comparisons CASCADE"
        )
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        await conn.execute("CREATE SCHEMA IF NOT EXISTS platform_catalog")
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS platform_catalog.schema_migrations "
            "(migration_stream text NOT NULL, domain text NOT NULL, "
            "version text NOT NULL, checksum text NOT NULL)"
        )
        for role in ("app_dashboard", "tenant_projection_writer"):
            await conn.execute(
                f"DO $$ BEGIN CREATE ROLE {role} NOLOGIN; "
                "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
            )
        yield conn
    finally:
        with contextlib.suppress(asyncpg.PostgresError):
            await conn.execute("ROLLBACK")
        await conn.execute(
            "DROP TABLE IF EXISTS public.delegation_shadow_comparisons CASCADE"
        )
        await conn.execute(
            "DELETE FROM platform_catalog.schema_migrations "
            "WHERE migration_stream = 'node:node_projection_delegation'"
        )
        await conn.close()


def _migration(path: Path) -> str:
    return path.read_text(encoding="utf-8")


async def _clean(conn: asyncpg.Connection) -> None:
    await conn.execute(
        "DROP TABLE IF EXISTS public.delegation_shadow_comparisons CASCADE"
    )
    await conn.execute(
        "DELETE FROM platform_catalog.schema_migrations "
        "WHERE migration_stream = 'node:node_projection_delegation'"
    )


async def _apply_precheck(conn: asyncpg.Connection) -> None:
    await conn.execute(_migration(_PRECHECK))


async def _apply_frozen(conn: asyncpg.Connection) -> None:
    await conn.execute(_migration(_FROZEN))


async def _ledger_0044(
    conn: asyncpg.Connection, checksum: str = _FROZEN_SHA256
) -> None:
    await conn.execute(
        "INSERT INTO platform_catalog.schema_migrations "
        "(migration_stream, domain, version, checksum) VALUES ($1, $2, $3, $4)",
        "node:node_projection_delegation",
        "tenant",
        _LEDGER_VERSION,
        checksum,
    )


async def _columns(conn: asyncpg.Connection) -> list[str]:
    return await conn.fetch(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='delegation_shadow_comparisons' "
        "ORDER BY ordinal_position"
    )


async def _create_valid_table(
    conn: asyncpg.Connection, *, populated: bool = True
) -> None:
    await conn.execute(
        "CREATE TABLE public.delegation_shadow_comparisons "
        "(id uuid, correlation_id text, tenant_id uuid, timestamp timestamptz, "
        "task_type text, primary_agent text, shadow_agent text, created_at timestamptz, "
        "session_id text)"
    )
    if populated:
        await conn.execute(
            "INSERT INTO public.delegation_shadow_comparisons "
            "(id, correlation_id, tenant_id, timestamp, task_type, primary_agent, "
            "shadow_agent, created_at, session_id) VALUES ($1,$2,$3,now(),'task',"
            "'primary','shadow',now(),'session')",
            uuid4(),
            f"corr-{uuid4().hex}",
            _TENANT,
        )


@pytest.mark.integration
class TestPre0044PostgresBehavior:
    async def test_absent_relation_is_a_noop_and_unrecorded_0044_creates_contract(
        self,
    ) -> None:
        async with _database() as conn:
            await _apply_precheck(conn)
            assert (
                await conn.fetchval(
                    "SELECT to_regclass('public.delegation_shadow_comparisons')"
                )
                is None
            )
            await _apply_frozen(conn)
            assert (
                await conn.fetchval(
                    "SELECT to_regclass('public.delegation_shadow_comparisons')"
                )
                is not None
            )

    async def test_empty_partial_relation_is_repaired_then_frozen_0044_converges(
        self,
    ) -> None:
        async with _database() as conn:
            await conn.execute("CREATE TABLE public.delegation_shadow_comparisons ()")
            await _apply_precheck(conn)
            await _apply_frozen(conn)
            names = {row["column_name"] for row in await _columns(conn)}
            assert {
                "id",
                "correlation_id",
                "tenant_id",
                "task_type",
                "created_at",
            } <= names
            assert await conn.fetchval(
                "SELECT relforcerowsecurity FROM pg_class WHERE oid='public.delegation_shadow_comparisons'::regclass"
            )

    async def test_populated_valid_relation_preserves_row_and_reasserts_recorded_posture(
        self,
    ) -> None:
        async with _database() as conn:
            await _create_valid_table(conn)
            before = await conn.fetchval(
                "SELECT count(*) FROM public.delegation_shadow_comparisons"
            )
            await _ledger_0044(conn)
            await _apply_precheck(conn)
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM public.delegation_shadow_comparisons"
                )
                == before
            )
            assert await conn.fetchval(
                "SELECT relforcerowsecurity FROM pg_class WHERE oid='public.delegation_shadow_comparisons'::regclass"
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM pg_policies WHERE schemaname='public' "
                    "AND tablename='delegation_shadow_comparisons' "
                    "AND policyname='tenant_isolation'"
                )
                == 1
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM pg_indexes WHERE indexname='idx_delegation_shadow_comparisons_timestamp'"
                )
                == 1
            )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM pg_indexes WHERE indexname='idx_delegation_shadow_comparisons_session_id'"
                )
                == 1
            )
            assert await conn.fetchval(
                "SELECT has_table_privilege('tenant_projection_writer', "
                "'public.delegation_shadow_comparisons', 'SELECT,INSERT,UPDATE')"
            )
            assert await conn.fetchval(
                "SELECT has_table_privilege('app_dashboard', "
                "'public.delegation_shadow_comparisons', 'SELECT')"
            )
            await _apply_precheck(conn)
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM public.delegation_shadow_comparisons"
                )
                == before
            )

    @pytest.mark.parametrize(
        "shape",
        [
            "missing_required",
            "null_tenant",
            "wrong_required_type",
            "wrong_optional_type",
            "duplicate_id",
            "conflicting_pk",
        ],
    )
    async def test_unsafe_populated_shapes_fail_before_schema_change(
        self, shape: str
    ) -> None:
        async with _database() as conn:
            if shape == "missing_required":
                await conn.execute(
                    "CREATE TABLE public.delegation_shadow_comparisons (id uuid)"
                )
                await conn.execute(
                    "INSERT INTO public.delegation_shadow_comparisons VALUES ($1)",
                    uuid4(),
                )
            elif shape == "null_tenant":
                await _create_valid_table(conn)
                await conn.execute(
                    "UPDATE public.delegation_shadow_comparisons SET tenant_id=NULL"
                )
            elif shape == "wrong_required_type":
                await conn.execute(
                    "CREATE TABLE public.delegation_shadow_comparisons "
                    "(id uuid, correlation_id text, tenant_id text, timestamp timestamptz, "
                    "task_type text, primary_agent text, shadow_agent text, created_at timestamptz)"
                )
                await conn.execute(
                    "INSERT INTO public.delegation_shadow_comparisons "
                    "(id, correlation_id, tenant_id, timestamp, task_type, primary_agent, shadow_agent, created_at) "
                    "VALUES ($1,'corr','bad',now(),'task','primary','shadow',now())",
                    uuid4(),
                )
            elif shape == "wrong_optional_type":
                await conn.execute(
                    "CREATE TABLE public.delegation_shadow_comparisons "
                    "(id uuid, correlation_id text, tenant_id uuid, timestamp timestamptz, "
                    "task_type text, primary_agent text, shadow_agent text, created_at timestamptz, session_id integer)"
                )
                await conn.execute(
                    "INSERT INTO public.delegation_shadow_comparisons "
                    "(id, correlation_id, tenant_id, timestamp, task_type, primary_agent, shadow_agent, created_at, session_id) "
                    "VALUES ($1,'corr',$2,now(),'task','primary','shadow',now(),1)",
                    uuid4(),
                    _TENANT,
                )
            elif shape == "duplicate_id":
                await conn.execute(
                    "CREATE TABLE public.delegation_shadow_comparisons "
                    "(id uuid, correlation_id text, tenant_id uuid, timestamp timestamptz, "
                    "task_type text, primary_agent text, shadow_agent text, created_at timestamptz)"
                )
                value = uuid4()
                await conn.executemany(
                    "INSERT INTO public.delegation_shadow_comparisons "
                    "(id, correlation_id, tenant_id, timestamp, task_type, primary_agent, shadow_agent, created_at) "
                    "VALUES ($1,$2,$3,now(),'task','primary','shadow',now())",
                    [(value, "a", _TENANT), (value, "b", _TENANT)],
                )
            else:
                await conn.execute(
                    "CREATE TABLE public.delegation_shadow_comparisons (id uuid PRIMARY KEY, tenant_id uuid)"
                )
                await conn.execute(
                    "ALTER TABLE public.delegation_shadow_comparisons ADD COLUMN other_key text"
                )
                await conn.execute(
                    "ALTER TABLE public.delegation_shadow_comparisons DROP CONSTRAINT delegation_shadow_comparisons_pkey"
                )
                await conn.execute(
                    "ALTER TABLE public.delegation_shadow_comparisons ADD PRIMARY KEY (other_key)"
                )
                await conn.execute(
                    "INSERT INTO public.delegation_shadow_comparisons (id, tenant_id, other_key) VALUES ($1,$2,'x')",
                    uuid4(),
                    _TENANT,
                )
            before = await _columns(conn)
            with pytest.raises(asyncpg.PostgresError):
                await _apply_precheck(conn)
            await conn.execute("ROLLBACK")
            assert await _columns(conn) == before
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM platform_catalog.schema_migrations WHERE version=$1",
                    _LEDGER_VERSION,
                )
                == 0
            )

    async def test_exact_runtime_ledger_identity_controls_reassertion(self) -> None:
        async with _database() as conn:
            await _create_valid_table(conn)
            await _ledger_0044(conn, checksum=_FROZEN_SHA256)
            await _apply_precheck(conn)
            assert await conn.fetchval(
                "SELECT relrowsecurity FROM pg_class WHERE oid='public.delegation_shadow_comparisons'::regclass"
            )
            await _clean(conn)
            await _create_valid_table(conn)
            await _ledger_0044(conn, checksum="wrong")
            await _apply_precheck(conn)
            assert not await conn.fetchval(
                "SELECT relrowsecurity FROM pg_class WHERE oid='public.delegation_shadow_comparisons'::regclass"
            )


def test_0043z_orders_before_frozen_0044_and_keeps_0044_bytes() -> None:
    names = [path.name for path in sorted(_MIGRATIONS.glob("004*.sql"))]
    assert names.index(_PRECHECK.name) < names.index(_FROZEN.name)
    assert sha256(_FROZEN.read_bytes()).hexdigest() == _FROZEN_SHA256


def test_0043z_is_one_transaction_and_uses_runtime_ledger_identity() -> None:
    source = _PRECHECK.read_text(encoding="utf-8")
    assert source.count("BEGIN;") == 1
    assert source.count("COMMIT;") == 1
    assert "platform_catalog.schema_migrations" in source
    assert (
        "node:node_projection_delegation:0044_restore_delegation_shadow_comparisons.sql"
        in source
    )
    assert _FROZEN_SHA256 in source


def test_0043z_refuses_ambiguous_history_before_repair() -> None:
    source = _PRECHECK.read_text(encoding="utf-8")
    for marker in (
        "missing required column",
        "has NULL required",
        "must be %",
        "duplicate historical id",
        "duplicate historical correlation_id",
        "existing primary key is not",
    ):
        assert marker in source
