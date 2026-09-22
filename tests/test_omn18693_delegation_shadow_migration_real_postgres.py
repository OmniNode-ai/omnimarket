# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Native PostgreSQL proof for the restored shadow-comparison projection."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from urllib.parse import quote, quote_plus
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_UUID

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MIGRATION = (
    _REPO_ROOT
    / "src/omnimarket/nodes/node_projection_delegation/migrations"
    / "0044_restore_delegation_shadow_comparisons.sql"
)
_RUNNER = _REPO_ROOT / "scripts/run-projection-migrations.py"
_WRITER_ROLE = "tenant_projection_writer"
_DASHBOARD_ROLE = "app_dashboard"


def _pg_bin(name: str) -> str:
    candidate = shutil.which(name)
    if candidate:
        return candidate
    bundled = Path("/opt/homebrew/opt/postgresql@16/bin") / name
    if bundled.is_file():
        return str(bundled)
    raise AssertionError(f"PostgreSQL 16 {name} is required for this native proof")


def _pg_env() -> dict[str, str]:
    environment = os.environ.copy()
    environment["LANG"] = "en_US.UTF-8"
    environment["LC_ALL"] = "C"
    return environment


@pytest.fixture
def pg_socket_dir(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Start and register an isolated PostgreSQL 16 integration endpoint.

    The native cluster is deliberately reached through the established
    ``INTEGRATION_POSTGRES_*`` configuration contract, rather than through a
    test-only connection path.  That makes the real runner and adapter proof
    exercise the same explicit connection inputs used by repository
    integration tests while retaining a private, fail-closed PG16 cluster.
    """
    initdb = _pg_bin("initdb")
    pg_ctl = _pg_bin("pg_ctl")
    version = subprocess.run(
        [initdb, "--version"],
        check=True,
        capture_output=True,
        text=True,
        env=_pg_env(),
    ).stdout
    assert "PostgreSQL) 16." in version, version
    root = Path(tempfile.mkdtemp(prefix="omn18693-pg-"))
    data = root / "data"
    socket = root / "socket"
    socket.mkdir()
    subprocess.run(
        [
            initdb,
            "-D",
            str(data),
            "-U",
            "postgres",
            "--auth-local=trust",
            "-E",
            "UTF8",
        ],
        check=True,
        capture_output=True,
        env=_pg_env(),
    )
    subprocess.run(
        [
            pg_ctl,
            "-D",
            str(data),
            "-l",
            str(root / "postgres.log"),
            "-o",
            f"-k {socket} -h ''",
            "-w",
            "start",
        ],
        check=True,
        capture_output=True,
        env=_pg_env(),
    )
    try:
        monkeypatch.setenv("INTEGRATION_POSTGRES_HOST", str(socket))
        monkeypatch.setenv("INTEGRATION_POSTGRES_PORT", "5432")
        monkeypatch.setenv("INTEGRATION_POSTGRES_USER", "postgres")
        monkeypatch.setenv("INTEGRATION_POSTGRES_DB", "postgres")
        yield str(socket)
    finally:
        subprocess.run(
            [pg_ctl, "-D", str(data), "-m", "immediate", "-w", "stop"],
            check=False,
            capture_output=True,
            env=_pg_env(),
        )
        shutil.rmtree(root, ignore_errors=True)


def _integration_postgres_dsn(user: str | None = None) -> str:
    """Build the native test connection from required integration settings."""
    host = os.environ["INTEGRATION_POSTGRES_HOST"]
    port = os.environ["INTEGRATION_POSTGRES_PORT"]
    configured_user = user or os.environ["INTEGRATION_POSTGRES_USER"]
    database = os.environ["INTEGRATION_POSTGRES_DB"]
    return (
        f"postgresql://{quote_plus(configured_user)}@/{quote_plus(database)}"
        f"?host={quote(host, safe='')}&port={quote(port, safe='')}"
    )


async def _connect(user: str = "postgres") -> asyncpg.Connection:
    return await asyncpg.connect(_integration_postgres_dsn(user))


def _load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("omn18693_migration_runner", _RUNNER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runner_tree(tmp_path: Path, migration: Path = _MIGRATION) -> Path:
    directory = tmp_path / "nodes/node_projection_delegation/migrations"
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copy2(migration, directory / _MIGRATION.name)
    return directory.parents[1]


async def _apply_with_real_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    migration: Path = _MIGRATION,
) -> int:
    module = _load_runner()
    real_connect = asyncpg.connect

    async def local_connect(_: str) -> asyncpg.Connection:
        return await real_connect(_integration_postgres_dsn())

    monkeypatch.setattr(module, "NODES_ROOT", _runner_tree(tmp_path, migration))
    monkeypatch.setattr(
        module,
        "asyncpg",
        SimpleNamespace(connect=local_connect, PostgresError=asyncpg.PostgresError),
    )
    return await module.run("postgresql://ignored", False, "node_projection_delegation")


async def _create_projection_roles() -> None:
    connection = await _connect()
    try:
        for role in (_WRITER_ROLE, _DASHBOARD_ROLE):
            await connection.execute(
                f"CREATE ROLE {role} LOGIN NOSUPERUSER NOBYPASSRLS"
            )
    finally:
        await connection.close()


async def _relation_flags() -> asyncpg.Record:
    connection = await _connect()
    try:
        return await connection.fetchrow(
            """
            SELECT class.relrowsecurity, class.relforcerowsecurity,
                   attribute.attnotnull,
                   pg_get_expr(default_value.adbin, default_value.adrelid) AS tenant_default
            FROM pg_class class
            JOIN pg_attribute attribute
              ON attribute.attrelid = class.oid AND attribute.attname = 'tenant_id'
            LEFT JOIN pg_attrdef default_value
              ON default_value.adrelid = attribute.attrelid
             AND default_value.adnum = attribute.attnum
            WHERE class.oid = 'public.delegation_shadow_comparisons'::regclass
            """
        )
    finally:
        await connection.close()


async def _write_with_actual_handler(correlation_id: str) -> str:
    pool = await asyncpg.create_pool(
        _integration_postgres_dsn(_WRITER_ROLE), min_size=1, max_size=1
    )
    adapter = AsyncpgAdapter(dsn="postgresql://native-test")
    adapter._pool = pool  # type: ignore[attr-defined]
    runner = DelegationProjectionRunner()
    runner._db = adapter  # type: ignore[assignment]
    try:
        assert await runner._project_shadow_comparison(
            {
                "correlation_id": correlation_id,
                "task_type": "native-proof",
                "primary_agent": "primary",
                "shadow_agent": "shadow",
            },
            MessageMeta(partition=0, offset=0, fallback_id=correlation_id),
        )
    finally:
        await pool.close()
    return str(HOUSE_TENANT_UUID)


_HISTORICAL_LEGACY_TABLE = """
CREATE TABLE public.delegation_shadow_comparisons (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    correlation_id TEXT UNIQUE NOT NULL,
    session_id TEXT,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    task_type TEXT NOT NULL,
    primary_agent TEXT NOT NULL,
    shadow_agent TEXT NOT NULL,
    divergence_detected BOOLEAN DEFAULT false,
    divergence_score NUMERIC(18, 9),
    primary_latency_ms INT,
    shadow_latency_ms INT,
    primary_cost_usd NUMERIC(18, 9),
    shadow_cost_usd NUMERIC(18, 9),
    divergence_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fresh_migration_uses_runner_and_actual_handler_with_rls(
    pg_socket_dir: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _create_projection_roles()
    admin = await _connect()
    try:
        await admin.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
    finally:
        await admin.close()
    assert await _apply_with_real_runner(tmp_path, monkeypatch) == 1
    assert await _apply_with_real_runner(tmp_path, monkeypatch) == 0
    flags = await _relation_flags()
    assert flags["relrowsecurity"] is True
    assert flags["relforcerowsecurity"] is True
    assert flags["attnotnull"] is True
    assert flags["tenant_default"] is None

    correlation_id = str(uuid4())
    tenant = await _write_with_actual_handler(correlation_id)
    admin = await _connect()
    try:
        assert (
            await admin.fetchval(
                "SELECT tenant_id::text FROM public.delegation_shadow_comparisons "
                "WHERE correlation_id = $1",
                correlation_id,
            )
            == tenant
        )
    finally:
        await admin.close()

    reader = await _connect(_WRITER_ROLE)
    try:
        rejected_insert = (
            "INSERT INTO public.delegation_shadow_comparisons "
            "(correlation_id, tenant_id, task_type, primary_agent, shadow_agent) "
            "VALUES ($1, $2::uuid, 'rejected', 'primary', 'shadow')"
        )
        with pytest.raises(asyncpg.PostgresError):
            await reader.execute(rejected_insert, str(uuid4()), tenant)
        assert (
            await reader.fetchval(
                "SELECT count(*) FROM public.delegation_shadow_comparisons"
            )
            == 0
        )
        await reader.execute("SELECT set_config('app.tenant_id', $1, false)", tenant)
        assert (
            await reader.fetchval(
                "SELECT count(*) FROM public.delegation_shadow_comparisons"
            )
            == 1
        )
        await reader.execute(
            "SELECT set_config('app.tenant_id', $1, false)", str(uuid4())
        )
        with pytest.raises(asyncpg.PostgresError):
            await reader.execute(rejected_insert, str(uuid4()), tenant)
        assert (
            await reader.fetchval(
                "SELECT count(*) FROM public.delegation_shadow_comparisons"
            )
            == 0
        )
    finally:
        await reader.close()

    dashboard = await _connect(_DASHBOARD_ROLE)
    try:
        assert (
            await dashboard.fetchval(
                "SELECT count(*) FROM public.delegation_shadow_comparisons"
            )
            == 0
        )
        await dashboard.execute("SELECT set_config('app.tenant_id', $1, false)", tenant)
        assert (
            await dashboard.fetchval(
                "SELECT count(*) FROM public.delegation_shadow_comparisons "
                "WHERE correlation_id = $1",
                correlation_id,
            )
            == 1
        )
        await dashboard.execute(
            "SELECT set_config('app.tenant_id', $1, false)", str(uuid4())
        )
        assert (
            await dashboard.fetchval(
                "SELECT count(*) FROM public.delegation_shadow_comparisons"
            )
            == 0
        )
    finally:
        await dashboard.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_empty_legacy_table_is_upgraded_by_the_real_runner(
    pg_socket_dir: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _create_projection_roles()
    admin = await _connect()
    try:
        await admin.execute(_HISTORICAL_LEGACY_TABLE)
    finally:
        await admin.close()
    assert await _apply_with_real_runner(tmp_path, monkeypatch) == 1
    flags = await _relation_flags()
    assert flags["relrowsecurity"] is True
    assert flags["relforcerowsecurity"] is True
    assert flags["attnotnull"] is True
    assert flags["tenant_default"] is None
    correlation_id = str(uuid4())
    tenant = await _write_with_actual_handler(correlation_id)
    admin = await _connect()
    try:
        assert (
            await admin.fetchval(
                "SELECT tenant_id::text FROM public.delegation_shadow_comparisons "
                "WHERE correlation_id = $1",
                correlation_id,
            )
            == tenant
        )
    finally:
        await admin.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_populated_unattributed_legacy_table_fails_without_partial_upgrade(
    pg_socket_dir: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _create_projection_roles()
    admin = await _connect()
    try:
        await admin.execute(_HISTORICAL_LEGACY_TABLE)
        await admin.execute(
            """
            INSERT INTO public.delegation_shadow_comparisons
                (id, correlation_id, task_type, primary_agent, shadow_agent)
            VALUES ($1, 'legacy-row', 'legacy', 'primary', 'shadow')
            """,
            uuid4(),
        )
    finally:
        await admin.close()
    with pytest.raises(
        asyncpg.PostgresError, match="legacy rows with no tenant attribution"
    ):
        await _apply_with_real_runner(tmp_path, monkeypatch)

    admin = await _connect()
    try:
        assert (
            await admin.fetchval(
                "SELECT count(*) FROM public.delegation_shadow_comparisons"
            )
            == 1
        )
        assert (
            await admin.fetchval(
                """
            SELECT EXISTS (
                SELECT 1 FROM pg_attribute
                WHERE attrelid = 'public.delegation_shadow_comparisons'::regclass
                  AND attname = 'tenant_id' AND NOT attisdropped
            )
            """
            )
            is False
        )
        assert (
            await admin.fetchval(
                "SELECT relrowsecurity FROM pg_class "
                "WHERE oid = 'public.delegation_shadow_comparisons'::regclass"
            )
            is False
        )
        assert (
            await admin.fetchval(
                "SELECT count(*) FROM omnimarket_schema_migrations "
                "WHERE node_name = 'node_projection_delegation' AND version = $1",
                _MIGRATION.name,
            )
            == 0
        )
    finally:
        await admin.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mutated_text_tenant_identity_is_rejected_atomically(
    pg_socket_dir: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation control: the real policy rejects TEXT in place of UUID."""
    mutated = tmp_path / "mutated" / _MIGRATION.name
    mutated.parent.mkdir()
    mutated.write_text(
        _MIGRATION.read_text(encoding="utf-8").replace(
            "tenant_id UUID NOT NULL,", "tenant_id TEXT NOT NULL,"
        ),
        encoding="utf-8",
    )
    await _create_projection_roles()
    with pytest.raises(asyncpg.PostgresError):
        await _apply_with_real_runner(tmp_path, monkeypatch, mutated)

    admin = await _connect()
    try:
        assert (
            await admin.fetchval(
                "SELECT to_regclass('public.delegation_shadow_comparisons')"
            )
            is None
        )
        assert (
            await admin.fetchval("SELECT count(*) FROM omnimarket_schema_migrations")
            == 0
        )
    finally:
        await admin.close()
