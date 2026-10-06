# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The liveness verdict projection against a real PostgreSQL 16.

The hermetic tests bind the writer's SQL to a fake. This runs the node's whole
migration chain and the real writer through the real asyncpg adapter into an
owned, localhost-only container (or CI's provisioned database), then reads the
rows back: one row per probed event, a redelivery converging, a later probe
distinct, a throttled tick writing nothing, and a DEAD verdict stored unhealthy
with its Slack error token intact.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_alert_channel_liveness.handlers.handler_alert_channel_liveness_writer import (
    AlertChannelLivenessProjectionWriter,
)
from omnimarket.projection.runner import MessageMeta, deterministic_correlation_id

_ROOT = Path(__file__).resolve().parents[1]
_NODE = _ROOT / "src/omnimarket/nodes/node_projection_alert_channel_liveness"
_DOCKER = shutil.which("docker")
_TOPIC = "onex.evt.omnimarket.alert-channel-liveness-checked.v1"
_DATABASE = "omn_alert_channel_liveness"


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
            f"alert-liveness-pg-{uuid4().hex[:12]}",
            "--label",
            "omnimarket.alert-channel-liveness=local-test",
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
) -> AsyncIterator[tuple[asyncpg.Connection, AlertChannelLivenessProjectionWriter]]:
    admin = await asyncpg.connect(**_kwargs(port))
    pool = await asyncpg.create_pool(**_kwargs(port), min_size=1, max_size=2)
    try:
        await admin.execute("DROP SCHEMA IF EXISTS omninode_internal CASCADE")
        await admin.execute("CREATE SCHEMA omninode_internal")
        await admin.execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
            "'omninode_runtime') THEN CREATE ROLE omninode_runtime NOLOGIN; END IF; END$$"
        )
        for migration in sorted((_NODE / "migrations").glob("*.sql")):
            await admin.execute(migration.read_text(encoding="utf-8"))
        adapter = AsyncpgAdapter(dsn="postgresql://unused")
        writer = AlertChannelLivenessProjectionWriter()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(adapter, "_pool", pool)
            patch.setattr(writer, "_db", adapter)
            yield admin, writer
    finally:
        await pool.close()
        await admin.close()


def _meta(offset: int) -> MessageMeta:
    return MessageMeta(
        partition=0,
        offset=offset,
        fallback_id=deterministic_correlation_id(_TOPIC, 0, offset),
        topic=_TOPIC,
    )


def _result(status: str, reason: str, slack_error: str | None = None) -> dict[str, Any]:
    return {
        "probed": True,
        "verdict": {"status": status, "reason": reason, "slack_error": slack_error},
        "probe_interval_seconds": 900,
        "failure_surfaced": status != "LIVE",
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_verdicts_round_trip_through_a_real_postgres(port: int) -> None:
    async with _provisioned(port) as (admin, writer):
        live = await writer._project_verdict(_result("LIVE", "channel ok"), _meta(1))
        dead = await writer._project_verdict(
            _result("DEAD", "bot not in channel", "not_in_channel"), _meta(2)
        )
        again = await writer._project_verdict(
            _result("DEAD", "bot not in channel", "not_in_channel"), _meta(2)
        )
        throttled = await writer._project_verdict(
            {"probed": False, "probe_interval_seconds": 900, "failure_surfaced": False},
            _meta(3),
        )

        assert live is not None
        assert dead is not None
        assert again is not None
        assert throttled is None

        rows = await admin.fetch(
            "SELECT status, healthy, slack_error, source_topic "
            "FROM omninode_internal.alert_channel_liveness_verdicts ORDER BY projection_cursor"
        )
        assert [(r["status"], r["healthy"], r["slack_error"]) for r in rows] == [
            ("LIVE", True, None),
            ("DEAD", False, "not_in_channel"),
        ]
        assert {r["source_topic"] for r in rows} == {_TOPIC}
        assert again["correlation_id"] == dead["correlation_id"]
