# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22652: the live events writer names the schema its contract declares.

On the dev lane the writer was folded into the runtime on 2026-11-03 and its
``INSERT INTO live_events`` carried the bare table name. A bare name resolves
through the login's default ``search_path`` to ``public.live_events``, where
the runtime login holds no privilege, while the same login holds INSERT on
``omninode_internal.live_events`` -- the relation the contract declares. Every
write was refused with ``permission denied for table live_events`` and the
event was dead-lettered after six refusals.

Only a real database proves the privilege shape, so this builds a throwaway
database holding both relations and one login that may INSERT only into the
internal one (the grant shape read from ``omnidash_analytics`` on 2026-11-06),
and runs the unchanged runner against it. Harness idiom as
``tests/test_omn15909_real_postgres_projection_write_path_gate.py``: it SKIPS
without a reachable server and creates and drops its own database and login.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_projection_live_events.handlers.handler_live_events import (
    HandlerLiveEventsProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta

_TOPIC = "onex.evt.platform.node-heartbeat.v1"
_LOGIN_PASSWORD = "omn22652-throwaway"

_TABLE_DDL = """
CREATE TABLE {relation} (
  id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  event_id       TEXT        UNIQUE NOT NULL,
  type           TEXT        NOT NULL DEFAULT 'ACTION',
  timestamp      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  source         TEXT        NOT NULL DEFAULT 'platform',
  topic          TEXT        NOT NULL DEFAULT '',
  summary        TEXT        NOT NULL DEFAULT '',
  payload        TEXT        NOT NULL DEFAULT '{{}}',
  correlation_id TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


def _dsn(user: str, password: str, database: str) -> str:
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    return (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}"
        f"/{database}"
    )


class _Server:
    """One disposable database and runtime login on the integration server."""

    def __init__(self) -> None:
        suffix = uuid4().hex[:10]
        self.database = f"omn22652_{suffix}"
        self.login = f"omn22652_runtime_{suffix}"
        self.admin_user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
        self.admin_password = os.environ.get(
            "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
        )

    def admin_dsn(self, database: str) -> str:
        return _dsn(self.admin_user, self.admin_password, database)

    def login_dsn(self) -> str:
        return _dsn(self.login, _LOGIN_PASSWORD, self.database)


@pytest.fixture
async def server() -> AsyncIterator[_Server]:
    """Both relations exist; the runtime login may write only the internal one."""
    srv = _Server()
    if not srv.admin_password:
        pytest.skip(
            "POSTGRES_PASSWORD not set -- skipping OMN-22652 real-Postgres test"
        )
    try:
        root = await asyncpg.connect(srv.admin_dsn("postgres"))
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no reachable Postgres for OMN-22652: {exc}")
    try:
        await root.execute(
            f"CREATE ROLE \"{srv.login}\" LOGIN PASSWORD '{_LOGIN_PASSWORD}'"
        )
        await root.execute(f'CREATE DATABASE "{srv.database}"')
        db = await asyncpg.connect(srv.admin_dsn(srv.database))
        try:
            await db.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
            await db.execute("CREATE SCHEMA omninode_internal")
            await db.execute(_TABLE_DDL.format(relation="public.live_events"))
            await db.execute(
                _TABLE_DDL.format(relation="omninode_internal.live_events")
            )
            await db.execute(f'GRANT USAGE ON SCHEMA public TO "{srv.login}"')
            await db.execute(
                f'GRANT USAGE ON SCHEMA omninode_internal TO "{srv.login}"'
            )
            await db.execute(
                "GRANT SELECT, INSERT, UPDATE ON omninode_internal.live_events "
                f'TO "{srv.login}"'
            )
        finally:
            await db.close()
        yield srv
    finally:
        await root.execute(f'DROP DATABASE IF EXISTS "{srv.database}" WITH (FORCE)')
        await root.execute(f'DROP ROLE IF EXISTS "{srv.login}"')
        await root.close()


async def _count(srv: _Server, relation: str) -> int:
    conn = await asyncpg.connect(srv.admin_dsn(srv.database))
    try:
        return int(await conn.fetchval(f"SELECT count(*) FROM {relation}"))
    finally:
        await conn.close()


@pytest.mark.integration
async def test_runner_writes_the_row_to_the_declared_schema(
    server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = HandlerLiveEventsProjectionRunner()
    monkeypatch.setattr(runner, "_ensure_producer", AsyncMock(return_value=None))
    runner.bind_projection_database_url(server.login_dsn())
    await runner.db.connect()
    try:
        projected = await runner.project_event(
            _TOPIC,
            {
                "event_id": str(uuid4()),
                "timestamp": datetime.now(UTC).isoformat(),
                "summary": "OMN-22652 heartbeat",
            },
            MessageMeta(partition=0, offset=0, fallback_id="omn22652"),
        )
    finally:
        await runner.db.close()

    assert projected is True
    assert await _count(server, "omninode_internal.live_events") == 1
    assert await _count(server, "public.live_events") == 0


@pytest.mark.integration
async def test_unqualified_statement_is_refused_control(server: _Server) -> None:
    """The failing control: the old bare statement, same database and login."""
    conn = await asyncpg.connect(server.login_dsn())
    try:
        with pytest.raises(
            asyncpg.InsufficientPrivilegeError,
            match="permission denied for table live_events",
        ):
            await conn.execute(
                "INSERT INTO live_events (event_id, type, timestamp, source, topic,"
                " summary, payload, correlation_id) VALUES ($1, 'ACTION', now(),"
                " 'platform', $2, '', '{}', NULL)",
                str(uuid4()),
                _TOPIC,
            )
    finally:
        await conn.close()
