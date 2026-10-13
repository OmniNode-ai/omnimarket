# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The automation-liveness projection against a real PostgreSQL 16 (OMN-20802).

The hermetic tests run the writer over SQLite. This runs the node's whole
migration chain and the real writer through the real asyncpg adapter into an
owned, localhost-only container (or CI's provisioned database), then reads the
rows back on another connection: a declared process that never emitted is a
row, the run history and alarm episode land with their column types, a replay
changes nothing, and the process row keeps the stored history across a second
writer.
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
from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationLivenessEvent as Kind,
)
from omnimarket.nodes.node_projection_automation_liveness.handlers.handler_automation_liveness_writer import (
    AutomationLivenessProjectionWriter,
)
from omnimarket.projection.runner import MessageMeta, deterministic_correlation_id
from tests.helpers.automation_liveness_stream import (
    ACTIVE_PROCESS,
    SILENT_PROCESS,
    STREAM_ORDER,
    fixture_payload,
    fixture_topic,
)

_ROOT = Path(__file__).resolve().parents[1]
_NODE = _ROOT / "src/omnimarket/nodes/node_projection_automation_liveness"
_DOCKER = shutil.which("docker")
_DATABASE = "omn_automation_liveness"


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
            f"automation-liveness-pg-{uuid4().hex[:12]}",
            "--label",
            "omnimarket.automation-liveness=local-test",
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
) -> AsyncIterator[tuple[asyncpg.Connection, AutomationLivenessProjectionWriter]]:
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
        writer = AutomationLivenessProjectionWriter()
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(adapter, "_pool", pool)
            patch.setattr(writer, "_db", adapter)
            yield admin, writer
    finally:
        await pool.close()
        await admin.close()


def _meta(kind: Kind, offset: int) -> MessageMeta:
    topic = fixture_topic(kind)
    return MessageMeta(
        partition=0,
        offset=offset,
        fallback_id=deterministic_correlation_id(topic, 0, offset),
        topic=topic,
    )


@asynccontextmanager
async def _provisioned(
    port: int,
) -> AsyncIterator[
    tuple[asyncpg.Connection, AutomationLivenessProjectionWriter, AsyncpgAdapter]
]:
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
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(adapter, "_pool", pool)
            yield admin, AutomationLivenessProjectionWriter(), adapter
    finally:
        await pool.close()
        await admin.close()


async def _stream(
    writer: AutomationLivenessProjectionWriter, adapter: AsyncpgAdapter
) -> list[int]:
    counts: list[int] = []
    for offset, kind in enumerate(STREAM_ORDER):
        out: dict[str, Any] = await writer._project(
            adapter, fixture_topic(kind), fixture_payload(kind), _meta(kind, offset)
        )
        counts.append(out["rows_upserted"])
    return counts


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_stream_round_trips_through_a_real_postgres(port: int) -> None:
    async with _provisioned(port) as (admin, writer, adapter):
        counts = await _stream(writer, adapter)
        assert all(count >= 1 for count in counts), counts

        states = {
            r["process_id"]: r
            for r in await admin.fetch(
                "SELECT * FROM omninode_internal.automation_liveness_state"
            )
        }
        silent = states[SILENT_PROCESS]
        assert silent["declared_at"] is not None
        assert silent["last_run_at"] is None
        assert silent["verdict"] is None
        active = states[ACTIVE_PROCESS]
        assert (active["verdict"], active["last_outcome"]) == ("missed", "ok")
        assert active["last_demand_count"] == 4
        assert active["open_episode_id"] is None
        assert active["process_key"] == f"{ACTIVE_PROCESS}@host-a"

        runs = await admin.fetch(
            "SELECT * FROM omninode_internal.automation_run_history"
        )
        assert [r["run_id"] for r in runs] == [
            "example-interval-job:2026-10-09T02:10:00Z"
        ]
        assert runs[0]["finished_at"] is not None

        episodes = await admin.fetch(
            "SELECT * FROM omninode_internal.automation_alarm_episodes"
        )
        assert len(episodes) == 1
        assert episodes[0]["delivery_ref"] == "1760002290.000100"
        assert episodes[0]["delivered_at"] is not None
        assert episodes[0]["cleared_at"] is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_replay_and_a_second_writer_change_no_row(port: int) -> None:
    async with _provisioned(port) as (admin, writer, adapter):
        await _stream(writer, adapter)
        before = [
            tuple(r)
            for r in await admin.fetch(
                "SELECT process_key, last_run_at, verdict, failures_in_window "
                "FROM omninode_internal.automation_liveness_state ORDER BY process_key"
            )
        ]
        replay = await _stream(AutomationLivenessProjectionWriter(), adapter)
        assert replay == [0] * len(STREAM_ORDER)
        after = [
            tuple(r)
            for r in await admin.fetch(
                "SELECT process_key, last_run_at, verdict, failures_in_window "
                "FROM omninode_internal.automation_liveness_state ORDER BY process_key"
            )
        ]
        assert after == before
