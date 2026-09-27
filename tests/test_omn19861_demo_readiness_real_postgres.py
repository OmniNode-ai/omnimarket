# SPDX-License-Identifier: MIT
"""Real-Postgres ordering and constraint proof for the demo readiness writer."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import quote_plus
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.events.demo_readiness import (
    EnumDemoDashboardConfiguration,
    EnumDemoRehearsalStatus,
    ModelRehearsalBundle,
)
from omnimarket.nodes.node_demo_rehearsal.handlers.handler_demo_rehearsal import (
    ModelDemoRehearsalResult,
)
from omnimarket.nodes.node_projection_demo_readiness.handlers.handler_demo_readiness_writer import (
    _UPSERT,
    DemoReadinessProjectionWriter,
)
from omnimarket.projection.runner import MessageMeta

pytestmark = pytest.mark.integration
SCHEMA = "omn19861_demo_readiness_write_path_test"
MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_demo_readiness/migrations/0000_create_demo_readiness_latest.sql"
)
OBSERVED = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


async def _connect_or_skip() -> asyncpg.Connection:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip("INTEGRATION_POSTGRES_PASSWORD not set")
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    dsn = f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
    try:
        return await asyncpg.connect(dsn)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"no reachable Postgres for demo-readiness proof: {exc}")


def _scoped(sql: str) -> str:
    return sql.replace("omninode_internal.", f"{SCHEMA}.")


async def _upsert(
    conn: asyncpg.Connection,
    *,
    observed_at: datetime,
    event_id: object,
    status: str,
) -> list[asyncpg.Record]:
    return await conn.fetch(
        _scoped(_UPSERT),
        "demo_rehearsal",
        "rehearsal-1",
        status,
        "UNCONFIGURED" if status == "UNCONFIGURED" else "CONFIGURED",
        observed_at,
        event_id,
        "/evidence/rehearsal_bundle.json",
        False,
        1 if status == "UNCONFIGURED" else 0,
        None,
        None,
        None,
    )


@pytest.mark.integration
async def test_real_postgres_newer_wins_stale_and_duplicate_refused() -> None:
    conn = await _connect_or_skip()
    try:
        await conn.execute(f"CREATE SCHEMA {SCHEMA}")
        await conn.execute(_scoped(MIGRATION.read_text(encoding="utf-8")))
        older_id = uuid4()
        newer_id = uuid4()
        first = await _upsert(
            conn, observed_at=OBSERVED, event_id=older_id, status="UNCONFIGURED"
        )
        assert len(first) == 1
        assert isinstance(first[0]["observed_at"], datetime)
        assert first[0]["failure_count"] == 1
        assert await _upsert(
            conn,
            observed_at=OBSERVED + timedelta(minutes=1),
            event_id=newer_id,
            status="GREEN",
        )
        assert (
            await _upsert(
                conn, observed_at=OBSERVED, event_id=older_id, status="UNCONFIGURED"
            )
            == []
        )
        assert (
            await _upsert(
                conn,
                observed_at=OBSERVED + timedelta(minutes=1),
                event_id=newer_id,
                status="GREEN",
            )
            == []
        )
        tie_breaking_id = UUID(int=(1 << 128) - 1)
        tied = await _upsert(
            conn,
            observed_at=OBSERVED + timedelta(minutes=1),
            event_id=tie_breaking_id,
            status="DEGRADED",
        )
        assert len(tied) == 1
        assert (
            await _upsert(
                conn,
                observed_at=OBSERVED + timedelta(minutes=1),
                event_id=newer_id,
                status="GREEN",
            )
            == []
        )
        stored = await conn.fetchrow(
            f"SELECT status, projection_cursor FROM {SCHEMA}.demo_readiness_latest"
        )
        assert stored is not None
        assert stored["status"] == "DEGRADED"
        assert stored["projection_cursor"] > first[0]["projection_cursor"]
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await conn.close()


@pytest.mark.integration
async def test_real_postgres_runtime_payload_to_snapshot_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The typed producer result reaches SQL and the keyed snapshot publisher."""
    conn = await _connect_or_skip()
    try:
        await conn.execute(f"CREATE SCHEMA {SCHEMA}")
        await conn.execute(_scoped(MIGRATION.read_text(encoding="utf-8")))

        class _Db:
            async def execute(self, sql: str, *args: object) -> list[asyncpg.Record]:
                return await conn.fetch(_scoped(sql), *args)

        writer = DemoReadinessProjectionWriter()
        writer._db = _Db()  # type: ignore[assignment]
        publisher = AsyncMock(
            spec=DemoReadinessProjectionWriter.publish_snapshot_delta,
            return_value=True,
        )
        monkeypatch.setattr(writer, "publish_snapshot_delta", publisher)

        def _payload(at: datetime, event_id: object) -> dict[str, object]:
            result = ModelDemoRehearsalResult(
                node_id="demo_rehearsal",
                run_id="rehearsal-real-pg",
                bundle_path="/evidence/rehearsal_bundle.json",
                overall_status=EnumDemoRehearsalStatus.GREEN.value,
                failure_count=0,
                dashboard_configuration=EnumDemoDashboardConfiguration.CONFIGURED,
                rehearsal_bundle=ModelRehearsalBundle(
                    rehearsal_id="rehearsal-real-pg",
                    timestamp_utc=at,
                    overall_status=EnumDemoRehearsalStatus.GREEN,
                ),
                dry_run=False,
            )
            return {**result.model_dump(mode="json"), "_envelope_id": str(event_id)}

        topic = "onex.evt.omnimarket.demo-rehearsed.v1"
        event_id = uuid4()
        first = await writer._project_event(
            topic,
            _payload(OBSERVED, event_id),
            MessageMeta(partition=0, offset=10, fallback_id="", topic=topic),
        )
        assert first is not None
        assert first["status"] == "GREEN"
        assert first["projection_cursor"] > 0
        assert publisher.await_count == 1
        assert publisher.await_args.kwargs["row"]["source_event_id"] == str(event_id)
        assert (
            await writer._project_event(
                topic,
                _payload(OBSERVED, event_id),
                MessageMeta(partition=0, offset=10, fallback_id="", topic=topic),
            )
            is None
        )
        # A duplicate retries the publish, but does not allocate a second row cursor.
        assert publisher.await_count == 2
        stored = await conn.fetchrow(
            f"SELECT projection_cursor FROM {SCHEMA}.demo_readiness_latest"
        )
        assert stored is not None
        assert stored["projection_cursor"] == first["projection_cursor"]
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await conn.close()
