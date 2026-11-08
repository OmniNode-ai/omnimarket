# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local alerts writer against a real PostgreSQL 16 (OMN-19991).

The hermetic writer tests bind the SQL to an in-memory stand-in. This runs the
node's migration chain and the real writer through the real asyncpg adapter into
an owned, localhost-only container (or CI's provisioned database), then reads
the rows back: a raise and its clear leave one row, a replay changes none, and
an older transition writes nothing.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_local_alerts.handlers.handler_local_alerts_writer import (
    HandlerLocalAlertsProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta
from tests.test_omn19991_local_alerts_writer import (
    T0,
    TENANT,
    TRANSITION_TOPIC,
    _transition_payload,
)

_NODE = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_local_alerts"
)
_DOCKER = shutil.which("docker")
_DATABASE = "omn_local_alerts"


def _kwargs(port: int) -> dict[str, Any]:
    if os.environ.get("INTEGRATION_POSTGRES_PASSWORD"):
        return {
            "user": os.environ.get("INTEGRATION_POSTGRES_USER", "postgres"),
            "database": os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra"),
            "host": os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost"),
            "port": port,
            "password": os.environ["INTEGRATION_POSTGRES_PASSWORD"],
            "ssl": False,
        }
    return {
        "user": "postgres",
        "database": _DATABASE,
        "host": "127.0.0.1",
        "port": port,
        "ssl": False,
    }


async def _accepts_sql(port: int) -> bool:
    try:
        connection = await asyncpg.connect(**_kwargs(port), timeout=1)
    except (OSError, asyncpg.PostgresError):
        return False
    try:
        return await connection.fetchval("SELECT 1") == 1
    finally:
        await connection.close()


@pytest.fixture(scope="module")
def port() -> Iterator[int]:
    """CI's provisioned PostgreSQL 16, else an owned localhost-only container."""
    if os.environ.get("INTEGRATION_POSTGRES_PASSWORD"):
        yield int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
        return
    if _DOCKER is None:
        pytest.skip("docker unavailable")
    created = subprocess.run(
        [
            _DOCKER,
            "run",
            "--detach",
            "--rm",
            "--name",
            f"local-alerts-pg-{uuid4().hex[:12]}",
            "--env",
            "POSTGRES_HOST_AUTH_METHOD=trust",
            "--env",
            f"POSTGRES_DB={_DATABASE}",
            "--publish",
            "127.0.0.1::5432",
            "postgres:16-alpine",
        ],
        check=True,
        capture_output=True,
    )
    container = created.stdout.decode().strip()
    try:
        published = subprocess.run(
            [_DOCKER, "port", container, "5432/tcp"],
            check=True,
            capture_output=True,
        ).stdout.decode()
        chosen = int(published.strip().splitlines()[0].rsplit(":", maxsplit=1)[1])
        for _ in range(150):
            if asyncio.run(_accepts_sql(chosen)):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("owned PostgreSQL 16 did not accept SELECT 1")
        yield chosen
    finally:
        subprocess.run(
            [_DOCKER, "rm", "--force", container], check=False, capture_output=True
        )


@asynccontextmanager
async def _provisioned(
    port: int,
) -> AsyncIterator[tuple[asyncpg.Connection, HandlerLocalAlertsProjectionRunner]]:
    admin = await asyncpg.connect(**_kwargs(port))
    pool = await asyncpg.create_pool(**_kwargs(port), min_size=1, max_size=2)
    try:
        await admin.execute("DROP TABLE IF EXISTS public.local_alerts CASCADE")
        for role in ("tenant_projection_writer", "app_dashboard"):
            await admin.execute(
                f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
                f"'{role}') THEN CREATE ROLE {role} NOLOGIN; END IF; END$$"
            )
        for migration in sorted((_NODE / "migrations").glob("*.sql")):
            await admin.execute(migration.read_text(encoding="utf-8"))
        adapter = AsyncpgAdapter(dsn="postgresql://unused")
        writer = HandlerLocalAlertsProjectionRunner()
        published: list[dict[str, Any]] = []

        async def _capture(exposure: Any, **kwargs: Any) -> bool:
            published.append(kwargs)
            return True

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(adapter, "_pool", pool)
            patch.setattr(writer, "_db", adapter)
            patch.setattr(writer, "publish_snapshot_delta", _capture)
            yield admin, writer
    finally:
        await pool.close()
        await admin.close()


async def _fold(
    writer: HandlerLocalAlertsProjectionRunner, payload: dict[str, Any]
) -> None:
    meta = MessageMeta(partition=0, offset=1, fallback_id="t", topic=TRANSITION_TOPIC)
    await writer.project_event(TRANSITION_TOPIC, dict(payload), meta)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_raise_and_its_clear_leave_one_row_in_a_real_postgres(
    port: int,
) -> None:
    async with _provisioned(port) as (admin, writer):
        raised = _transition_payload()
        cleared = _transition_payload(
            transition="cleared",
            occurred_at=(T0 + timedelta(seconds=30)).isoformat(),
        )
        await _fold(writer, raised)
        await _fold(writer, cleared)
        await _fold(writer, raised)  # an older transition: writes nothing
        await _fold(writer, cleared)  # a replay: writes nothing

        rows = await admin.fetch(
            "SELECT tenant_id, code, subject, state, cleared_at "
            "FROM public.local_alerts"
        )
        assert [
            (r["tenant_id"], r["code"], r["subject"], r["state"]) for r in rows
        ] == [(TENANT, "SERVICE_DOWN", "compose/postgres", "cleared")]
        assert rows[0]["cleared_at"] == T0 + timedelta(seconds=30)
