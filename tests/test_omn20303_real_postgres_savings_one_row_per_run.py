# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20303: one delegation run keeps ONE savings_estimates row on real Postgres.

The unit suite in ``node_projection_savings/tests/test_savings_one_row_per_run.py``
proves the identity fold against an asyncpg double. This module drives the same
two terminals through the REAL ``SavingsProjectionRunner`` write path against a
migrated Postgres schema, so the run-identity read and the upsert are decided by
the table's real columns and its real unique key
``(session_id, event_timestamp, model_local, model_cloud_baseline)``.

The tenant resolution is pinned to the house tenant UUID: the tenant registry is
another node's table and is not what this test proves. SKIPS (never ERRORs)
without a reachable database and provisions its own throwaway schema.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_savings.handlers.handler_savings import (
    SavingsProjectionRunner,
)
from omnimarket.pricing import build_premium_counterfactual
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_UUID

_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_savings"
    / "migrations"
)

# Mirror of omnibase_infra migration 094 (see
# tests/test_writer_tenant_isolation_omn14898.py): the savings RLS migration
# raises when this cluster-wide role is absent.
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

RUN = "727c0456-1758-421a-8600-25da6410f2cf"
DELEGATE_SKILL_TOPIC = "onex.evt.omnimarket.delegate-skill-completed.v1"
CANONICAL_TOPIC = "onex.evt.omnibase-infra.delegation-completed.v1"
FIRST = "2026-10-01T12:23:58.700000+00:00"
SECOND = "2026-10-01T12:23:59.100000+00:00"


def _base_dsn() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


async def _connect_or_skip() -> asyncpg.Connection:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "POSTGRES_PASSWORD not set -- skipping OMN-20303 real-Postgres test"
        )
    try:
        return await asyncpg.connect(_base_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for OMN-20303 test: {exc}")


def _cf_cost() -> float:
    cf = build_premium_counterfactual(prompt_tokens=319, completion_tokens=154)
    assert cf is not None
    return float(cf.counterfactual_cost_usd)


def _delegate_skill_payload(timestamp: str) -> dict[str, object]:
    return {
        "status": "completed",
        "correlation_id": RUN,
        "task_type": "summarization",
        "provider": "local",
        "model_name": "Qwen3.8-27B",
        "quality_gate_passed": True,
        "emitted_at": timestamp,
        "metrics": {
            "input_tokens": 319,
            "output_tokens": 154,
            "total_tokens": 473,
            "cost_usd": 0.0,
            "cost_savings_usd": _cf_cost(),
            "latency_ms": 900,
        },
    }


def _canonical_payload(timestamp: str) -> dict[str, object]:
    return {
        "correlation_id": RUN,
        "task_type": "summarization",
        "model_used": "Qwen3.8-27B",
        "quality_passed": True,
        "cumulative_attempt_cost": 0.0,
        "final_attempt_cost": 0.0,
        "cumulative_input_tokens": 319,
        "cumulative_output_tokens": 154,
        "prompt_tokens": 319,
        "completion_tokens": 154,
        "timestamp": timestamp,
    }


@asynccontextmanager
async def _provisioned_runner() -> AsyncIterator[
    tuple[SavingsProjectionRunner, asyncpg.Connection]
]:
    admin_conn = await _connect_or_skip()
    schema = f"omn20303_{uuid4().hex[:16]}"
    pool: asyncpg.Pool | None = None
    try:
        await admin_conn.execute(f"CREATE SCHEMA {schema}")
        await admin_conn.execute(f"SET search_path TO {schema}, public")
        await admin_conn.execute(_APP_DASHBOARD_ROLE_SQL)
        for migration_path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            sql = migration_path.read_text(encoding="utf-8").replace(
                "CREATE INDEX CONCURRENTLY", "CREATE INDEX"
            )
            await admin_conn.execute(sql)

        pool = await asyncpg.create_pool(
            _base_dsn(),
            min_size=1,
            max_size=3,
            server_settings={"search_path": f"{schema},public"},
        )
        adapter = AsyncpgAdapter(dsn=_base_dsn())
        adapter._pool = pool  # type: ignore[attr-defined]

        runner = SavingsProjectionRunner(publish_fn=_noop_publish)

        async def _house_tenant(_data: dict[str, object]) -> str:
            return str(HOUSE_TENANT_UUID)

        runner._resolve_row_tenant = _house_tenant  # type: ignore[assignment]
        runner._db = adapter  # type: ignore[assignment]
        yield runner, admin_conn
    finally:
        if pool is not None:
            with contextlib.suppress(Exception):
                await pool.close()
        with contextlib.suppress(Exception):
            await admin_conn.execute("SET search_path TO public")
            await admin_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin_conn.close()


async def _noop_publish(*_args: object, **_kwargs: object) -> None:
    return None


@pytest.mark.integration
@pytest.mark.parametrize(
    ("first", "second"),
    [
        (
            (DELEGATE_SKILL_TOPIC, _delegate_skill_payload(FIRST)),
            (CANONICAL_TOPIC, _canonical_payload(SECOND)),
        ),
        (
            (CANONICAL_TOPIC, _canonical_payload(FIRST)),
            (DELEGATE_SKILL_TOPIC, _delegate_skill_payload(SECOND)),
        ),
    ],
    ids=["delegate_skill_then_canonical", "canonical_then_delegate_skill"],
)
async def test_real_postgres_two_terminals_of_one_run_keep_one_row(
    first: tuple[str, dict[str, object]], second: tuple[str, dict[str, object]]
) -> None:
    async with _provisioned_runner() as (runner, admin_conn):
        for offset, (topic, data) in enumerate((first, second)):
            meta = MessageMeta(partition=0, offset=offset, fallback_id=RUN, topic=topic)
            assert await runner.project_event(topic, dict(data), meta)

        rows = await admin_conn.fetch(
            "SELECT event_timestamp, savings_usd FROM savings_estimates "
            "WHERE session_id = $1",
            RUN,
        )

    assert len(rows) == 1
    assert rows[0]["event_timestamp"] == datetime(2026, 10, 1, 12, 23, 58, tzinfo=UTC)
    assert rows[0]["savings_usd"] == Decimal(str(round(_cf_cost(), 6)))
