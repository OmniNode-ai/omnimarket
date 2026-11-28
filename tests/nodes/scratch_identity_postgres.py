# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A scratch Postgres holding the cloud identity tables, for tests that exercise real SQL (OMN-26474).

The server is the CI-provisioned INTEGRATION_POSTGRES_* one when it answers, else a private cluster started from the
``initdb`` and ``pg_ctl`` binaries on the host (PATH, or a distribution's versioned directory), on a unix socket in a
temporary directory. With neither the test run fails: it never skips, so a green run always means the SQL ran. Each test
gets its own database, created from the DDL below and dropped afterwards. The DDL carries the columns of the cloud owner's
migrations that the key paths read or write (read only reference; nothing here is applied anywhere else).
"""

from __future__ import annotations

import asyncio
import atexit
import glob
import os
import shutil
import socket
import subprocess
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from datetime import datetime
from urllib.parse import quote_plus
from uuid import UUID, uuid4

import asyncpg
import pytest

_DDL_TENANTS = """
CREATE TABLE tenants (
    tenant_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_slug text NOT NULL UNIQUE,
    provisioning_state text NOT NULL DEFAULT 'active'
)
"""
_DDL_KEYS = """
CREATE TABLE tenant_api_keys (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants (tenant_id),
    name text NOT NULL,
    key_hash text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    revoked_at timestamptz,
    is_active boolean NOT NULL DEFAULT true,
    {expires_at}
    CONSTRAINT chk_revocation_consistency CHECK (
        (is_active AND revoked_at IS NULL) OR (NOT is_active AND revoked_at IS NOT NULL)
    )
)
"""
_DDL_ACTIVE_NAME_INDEX = (
    "CREATE UNIQUE INDEX uq_tenant_api_keys_tenant_id_name_active "
    "ON tenant_api_keys (tenant_id, name) WHERE is_active"
)

_cluster_dir: str | None = None
_cluster_dsn: str | None = None


def _env_dsn() -> str | None:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        return None
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    return (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/postgres"
    )


def _binary(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    candidates = sorted(glob.glob(f"/usr/lib/postgresql/*/bin/{name}"), reverse=True)
    candidates += sorted(glob.glob(f"/opt/homebrew/opt/postgresql@*/bin/{name}"))
    return candidates[0] if candidates else None


def _free_port() -> int:
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _stop_cluster() -> None:
    global _cluster_dir, _cluster_dsn
    if _cluster_dir is None:
        return
    pg_ctl = _binary("pg_ctl")
    if pg_ctl:
        subprocess.run(
            [pg_ctl, "-D", f"{_cluster_dir}/data", "-m", "immediate", "stop"],
            check=False,
            capture_output=True,
        )
    shutil.rmtree(_cluster_dir, ignore_errors=True)
    _cluster_dir = _cluster_dsn = None


def _start_cluster() -> str | None:
    global _cluster_dir, _cluster_dsn
    if _cluster_dsn is not None:
        return _cluster_dsn
    initdb, pg_ctl = _binary("initdb"), _binary("pg_ctl")
    if not initdb or not pg_ctl:
        return None
    root = tempfile.mkdtemp(prefix="pgscratch", dir="/tmp")
    data, port = f"{root}/data", _free_port()
    subprocess.run(
        [
            initdb,
            "-D",
            data,
            "-U",
            "postgres",
            "--auth=trust",
            "-E",
            "UTF8",
            "--no-sync",
        ],
        check=True,
        capture_output=True,
    )
    options = (
        f"-c listen_addresses='' -c unix_socket_directories={root} -c port={port} "
        "-c fsync=off -c synchronous_commit=off -c full_page_writes=off"
    )
    subprocess.run(
        [pg_ctl, "-D", data, "-o", options, "-w", "-l", f"{root}/log", "start"],
        check=True,
        capture_output=True,
    )
    _cluster_dir = root
    _cluster_dsn = f"postgresql://postgres@/postgres?host={root}&port={port}"
    atexit.register(_stop_cluster)
    return _cluster_dsn


async def _reachable(dsn: str) -> bool:
    try:
        conn = await asyncio.wait_for(asyncpg.connect(dsn), timeout=3)
    except (OSError, TimeoutError, asyncpg.PostgresError):
        return False
    await conn.close()
    return True


async def _admin_dsn() -> str:
    env = _env_dsn()
    if env is not None and await _reachable(env):
        return env
    cluster = _start_cluster()
    if cluster is not None:
        return cluster
    pytest.fail(
        "no scratch Postgres: INTEGRATION_POSTGRES_* is unset or unreachable and no initdb/pg_ctl binaries were found"
    )


def _with_database(admin_dsn: str, database: str) -> str:
    base, _, query = admin_dsn.partition("?")
    root, _, _ = base.rpartition("/")
    return f"{root}/{database}" + (f"?{query}" if query else "")


@asynccontextmanager
async def scratch_identity_database(
    *, with_expires_at: bool = True
) -> AsyncIterator[str]:
    """A fresh database holding ``tenants`` and ``tenant_api_keys``; yields its DSN, drops it on exit."""
    admin_dsn = await _admin_dsn()
    database = f"scratch_{uuid4().hex}"
    admin = await asyncpg.connect(admin_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        dsn = _with_database(admin_dsn, database)
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(_DDL_TENANTS)
            await conn.execute(
                _DDL_KEYS.format(
                    expires_at="expires_at timestamptz," if with_expires_at else ""
                )
            )
            await conn.execute(_DDL_ACTIVE_NAME_INDEX)
        finally:
            await conn.close()
        try:
            yield dsn
        finally:
            await admin.execute(f'DROP DATABASE "{database}" WITH (FORCE)')
    finally:
        await admin.close()


async def add_tenant(
    dsn: str, *, state: str = "active", slug: str | None = None
) -> UUID:
    conn = await asyncpg.connect(dsn)
    try:
        tenant_id: UUID = await conn.fetchval(
            "INSERT INTO tenants (tenant_slug, provisioning_state) VALUES ($1, $2) RETURNING tenant_id",
            slug or f"tenant-{uuid4().hex[:12]}",
            state,
        )
        return tenant_id
    finally:
        await conn.close()


async def add_key(
    dsn: str,
    tenant_id: UUID,
    *,
    name: str,
    key_hash: str,
    expires_at: datetime | None = None,
    active: bool = True,
) -> UUID:
    columns = ["tenant_id", "name", "key_hash", "is_active", "revoked_at"]
    values = ["$1", "$2", "$3", "$4", "CASE WHEN $4 THEN NULL ELSE now() END"]
    args: list[object] = [tenant_id, name, key_hash, active]
    if expires_at is not None:
        columns.append("expires_at")
        values.append("$5")
        args.append(expires_at)
    conn = await asyncpg.connect(dsn)
    try:
        credential_id: UUID = await conn.fetchval(
            f"INSERT INTO tenant_api_keys ({', '.join(columns)}) VALUES ({', '.join(values)}) RETURNING id",
            *args,
        )
        return credential_id
    finally:
        await conn.close()


async def snapshot(dsn: str) -> list[tuple[object, ...]]:
    """Every tenant and key row, in a stable order, for before and after comparison."""
    conn = await asyncpg.connect(dsn)
    try:
        tenants = await conn.fetch("SELECT * FROM tenants ORDER BY tenant_id")
        keys = await conn.fetch("SELECT * FROM tenant_api_keys ORDER BY id")
        return [tuple(r.values()) for r in tenants] + [tuple(r.values()) for r in keys]
    finally:
        await conn.close()
