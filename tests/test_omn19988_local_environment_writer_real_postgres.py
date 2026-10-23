# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real writer proof: asyncpg types, replay/late no-ops and stale sibling writes.

Runs the real writer as the integration DSN's user in a disposable schema, with
the node's own migrations and no test-local grants. Row-level security under a
non-superuser writer role is proven by
tests/test_omn19988_local_environment_migrations_real_postgres.py.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote_plus
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.handlers.handler_local_environment_judgement import (
    load_local_alert_rules,
)
from omnimarket.models.model_local_environment_status import (
    ModelLocalEnvironmentStatus,
)
from omnimarket.nodes.node_projection_local_environment.handlers import (
    handler_local_environment_writer as writer_module,
)
from omnimarket.nodes.node_projection_local_environment.handlers.handler_projection_local_environment import (
    HandlerProjectionLocalEnvironment,
)
from omnimarket.nodes.node_projection_local_environment.models import (
    ModelLocalEnvironmentPlanRequest,
)
from omnimarket.projection.runner import MessageMeta

_MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_local_environment/migrations"
)
_TENANT = "6c24401e-0000-4000-8000-00000000000a"
_AT = datetime(2026, 10, 18, 6, tzinfo=UTC)


def _dsn_or_skip() -> str:
    dsn = os.environ.get("INTEGRATION_POSTGRES_DSN")
    if dsn:
        return dsn
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip("INTEGRATION_POSTGRES DSN/password not set")
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = quote_plus(os.environ.get("INTEGRATION_POSTGRES_USER", "postgres"))
    database = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{user}:{quote_plus(password)}@{host}:{port}/{database}"


@pytest.mark.integration
async def test_real_postgres_writer_types_replay_late_and_stale_siblings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = _dsn_or_skip()
    conn = await asyncpg.connect(dsn)
    schema = f"omn19988_writer_{uuid4().hex}"
    pool: asyncpg.Pool | None = None
    published: list[dict[str, Any]] = []

    async def capture(exposure: Any, **kwargs: Any) -> bool:
        published.append({"topic": exposure.topic, **kwargs})
        return True

    try:
        await conn.execute(f"CREATE SCHEMA {schema}")
        for migration in sorted(_MIGRATIONS.glob("*.sql")):
            await conn.execute(
                migration.read_text()
                .replace("public.", f"{schema}.")
                .replace("ON SCHEMA public ", f"ON SCHEMA {schema} ")
            )
        # The writer confirms the tenant against tenant_registry_mirror, which
        # it names unqualified; the disposable schema carries its own mirror
        # row for the tenant, as the bundle's registry seed step records it.
        await conn.execute(
            f"CREATE TABLE {schema}.tenant_registry_mirror ("
            "tenant_slug TEXT PRIMARY KEY, tenant_uuid UUID NOT NULL, "
            "status TEXT NOT NULL, observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
        )
        await conn.execute(
            f"INSERT INTO {schema}.tenant_registry_mirror "
            "(tenant_slug, tenant_uuid, status) VALUES ($1, $2, 'active')",
            "omn19988-writer",
            UUID(_TENANT),
        )
        pool = await asyncpg.create_pool(
            dsn,
            min_size=1,
            max_size=1,
            server_settings={"search_path": f"{schema}, public"},
        )
        assert pool is not None
        # Scope only relation names to the disposable schema. All parameters,
        # transactions, planning and asyncpg execution use the real writer.
        for name in ("_SELECT_MODE_ROWS", "_UPSERT_ROW"):
            monkeypatch.setattr(
                writer_module,
                name,
                getattr(writer_module, name).replace("public.", f"{schema}."),
            )
        writer = writer_module.LocalEnvironmentProjectionWriter()
        monkeypatch.setattr(writer, "_db", SimpleNamespace(pool=pool))
        monkeypatch.setattr(writer, "publish_snapshot_delta", capture)
        topic = writer.subscribe_topics[0]
        meta = MessageMeta(partition=0, offset=7, fallback_id="proof", topic=topic)

        def report(service: str, seconds: int) -> dict[str, Any]:
            return {
                "tenant_id": _TENANT,
                "mode": "native",
                "service": service,
                "observed_state": "running",
                "reason": "healthy",
                "observed_at": (_AT + timedelta(seconds=seconds)).isoformat(),
                "versions": {"omnimarket": "0.4.317"},
                "store_kind": "sqlite",
                "store_location": "environment.sqlite",
                "access_mode": "loopback-token",
                "token_minted_at": _AT.isoformat(),
                "rotate_hint": "restart dashboard",
            }

        async def rows() -> list[dict[str, Any]]:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.tenant_id', $1, true)", _TENANT
                )
                return [
                    dict(r)
                    for r in await conn.fetch(
                        f"SELECT * FROM {schema}.local_environment ORDER BY service"
                    )
                ]

        first = report("postgres", 0)
        assert await writer.project_event(topic, first, meta)
        initial = await rows()
        assert len(initial) == 1
        row = initial[0]
        for column in ("last_seen", "last_change", "updated_at", "token_minted_at"):
            assert isinstance(row[column], datetime)
            assert row[column].tzinfo is not None
        assert row["last_seen"] == row["token_minted_at"] == _AT
        assert json.loads(row["versions"]) == {"omnimarket": "0.4.317"}
        assert isinstance(row["projection_cursor"], int)
        types = {
            r["column_name"]: r["data_type"]
            for r in await conn.fetch(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = $1 AND table_name = 'local_environment'",
                schema,
            )
        }
        assert types["versions"] == "jsonb"
        assert types["projection_cursor"] == "bigint"
        for column in (
            "last_seen",
            "last_change",
            "alert_since",
            "token_minted_at",
            "updated_at",
        ):
            assert types[column] == "timestamp with time zone"

        original_delta = published[0]
        published.clear()
        assert await writer.project_event(topic, first, meta)
        assert published == [original_delta]
        published.clear()
        assert await writer.project_event(topic, report("postgres", -10), meta)
        assert await rows() == initial
        assert published == []

        assert await writer.project_event(topic, report("local-runtime", 75), meta)
        final = await rows()
        assert [(r["service"], r["verdict"], r["alert_code"]) for r in final] == [
            ("local-runtime", "running", None),
            ("postgres", "stale", "HEALTH_UNKNOWN"),
        ]
        assert final[1]["last_seen"] == _AT
        assert final[1]["alert_since"] == _AT + timedelta(
            seconds=load_local_alert_rules().freshness_deadline_seconds
        )
        assert final[1]["projection_cursor"] > row["projection_cursor"]
        # A reader paging by cursor sees both rows this report changed, so each
        # carries a cursor newer than any the table held before it.
        assert min(r["projection_cursor"] for r in final) > max(
            r["projection_cursor"] for r in initial
        )
        assert sorted(p["row"]["service"] for p in published) == [
            "local-runtime",
            "postgres",
        ]
        original_deltas = {p["row"]["service"]: p for p in published}
        published.clear()
        assert await writer.project_event(topic, report("local-runtime", 75), meta)
        assert await rows() == final
        assert len(published) == 2
        assert {p["row"]["service"]: p for p in published} == original_deltas
        published.clear()
        assert await writer.project_event(topic, report("postgres", -1), meta)
        assert await rows() == final
        assert published == []
        # The typed plan request accepts rows exactly as Postgres returns them
        # (jsonb text, timestamptz), and a replay planned against them plans nothing.
        replay = HandlerProjectionLocalEnvironment().handle(
            ModelLocalEnvironmentPlanRequest(
                report=ModelLocalEnvironmentStatus.model_validate(
                    report("local-runtime", 75)
                ),
                stored_rows=tuple(final),
                rules=load_local_alert_rules(),
            )
        )
        assert replay.rows == ()
    finally:
        if pool is not None:
            await pool.close()
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()
