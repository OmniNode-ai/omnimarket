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
import json
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
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.nodes.node_projection_savings.handlers.handler_savings import (
    SavingsProjectionRunner,
)
from omnimarket.pricing import build_premium_counterfactual
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_UUID
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _live_migration_files as _delegation_migration_files,
)
from tests.test_omn18147_delegation_completed_using_rls import (
    local_postgres as local_postgres,
)
from tests.test_omn21015_run_savings_real_postgres import (
    integration_postgres_dsn as integration_postgres_dsn,
)
from tests.test_omn21015_run_savings_real_postgres import postgres as postgres
from tests.unit.nodes.node_projection_delegation.test_canonical_savings_counterfactual_omn20303 import (
    COMPLETED,
    FAILED,
    _canonical,
    _shutdown,
)

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
  BEGIN
    CREATE ROLE tenant_projection_writer WITH
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
async def _provisioned_runner(
    dsn: str | None = None,
) -> AsyncIterator[tuple[SavingsProjectionRunner, asyncpg.Connection]]:
    admin_conn = await (asyncpg.connect(dsn) if dsn else _connect_or_skip())
    runner_dsn = dsn or _base_dsn()
    schema = f"omn20303_{uuid4().hex[:16]}"
    pool: asyncpg.Pool | None = None
    try:
        await admin_conn.execute(f"CREATE SCHEMA {schema}")
        await admin_conn.execute(f"SET search_path TO {schema}, public")
        await admin_conn.execute(_APP_DASHBOARD_ROLE_SQL)
        # The savings views and the run-identity read join `delegation_events`,
        # which the delegation node owns: migrate it first, as production does.
        for migration_path in (
            *_delegation_migration_files(),
            *sorted(_MIGRATIONS_DIR.glob("*.sql")),
        ):
            # Migrations name `public.` explicitly; the throwaway schema is
            # first on the search_path, so strip it to land the tables there.
            sql = (
                migration_path.read_text(encoding="utf-8")
                .replace("CREATE INDEX CONCURRENTLY", "CREATE INDEX")
                .replace("public.", "")
            )
            await admin_conn.execute(sql)

        pool = await asyncpg.create_pool(
            runner_dsn,
            min_size=1,
            max_size=3,
            server_settings={"search_path": f"{schema},public"},
        )
        adapter = AsyncpgAdapter(dsn=runner_dsn)
        adapter._pool = pool

        runner = SavingsProjectionRunner(publish_fn=_noop_publish)

        async def _house_tenant(_data: dict[str, object]) -> str:
            return str(HOUSE_TENANT_UUID)

        runner._resolve_row_tenant = _house_tenant  # type: ignore[assignment]
        runner._db = adapter
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


@pytest.mark.integration
@pytest.mark.parametrize(
    "order",
    ["canonical-only", "shutdown-first", "shutdown-last", "pinned-before-completion"],
)
async def test_real_postgres_canonical_counterfactual_survives_shutdown(
    integration_postgres_dsn: str,
    order: str,
) -> None:
    """The actual asyncpg INSERT/UPDATE retains the saving's baseline and types.

    INTEGRATION_POSTGRES_DSN names the owned PostgreSQL 16 container supplied
    by the existing fixture, never a shared deployment's application database.
    Both real projection writers use the same migrated, disposable schema.
    """
    async with _provisioned_runner(integration_postgres_dsn) as (savings, conn):
        await conn.execute(
            "CREATE TABLE tenant_registry_mirror "
            "(tenant_slug text PRIMARY KEY, tenant_uuid uuid UNIQUE NOT NULL)"
        )
        await conn.execute(
            "INSERT INTO tenant_registry_mirror VALUES ($1, $2)",
            "counterfactual-test",
            HOUSE_TENANT_UUID,
        )
        delegation = DelegationProjectionRunner(publish_fn=_noop_publish)
        delegation._db = savings.db
        completion = _canonical()
        shutdown = _shutdown()
        for payload in (completion, shutdown):
            payload["tenant_id"] = str(HOUSE_TENANT_UUID)
        correlation = str(completion["correlation_id"])
        events = [(COMPLETED, completion)]
        if order in {"shutdown-first", "pinned-before-completion"}:
            events.insert(0, (FAILED, shutdown))
        elif order == "shutdown-last":
            events.append((FAILED, shutdown))
        original = None
        if order == "pinned-before-completion":
            cf = build_premium_counterfactual(prompt_tokens=12, completion_tokens=34)
            assert cf is not None
            original = cf.model_dump(mode="json")
        for offset, (topic, payload) in enumerate(events):
            meta = MessageMeta(
                topic=topic,
                partition=0,
                offset=offset,
                fallback_id=correlation,
            )
            assert await delegation.project_event(topic, dict(payload), meta)
            assert await savings.project_event(topic, dict(payload), meta)
            if original is not None and offset == 0:
                await conn.execute(
                    "UPDATE delegation_events SET premium_counterfactual=$1::jsonb "
                    "WHERE correlation_id=$2",
                    json.dumps(original),
                    correlation,
                )
        rows = await conn.fetch(
            "SELECT d.premium_counterfactual, d.terminal_failure_cause, "
            "d.timestamp, s.cloud_cost_usd, s.event_timestamp "
            "FROM delegation_events d JOIN savings_estimates s "
            "ON s.session_id=d.correlation_id AND s.tenant_id=d.tenant_id::text "
            "WHERE d.correlation_id=$1",
            correlation,
        )
        assert len(rows) == 1
        baseline = json.loads(rows[0]["premium_counterfactual"])
        assert isinstance(rows[0]["timestamp"], datetime)
        assert isinstance(rows[0]["event_timestamp"], datetime)
        if original is not None:
            assert baseline == original
        else:
            assert baseline["tokens_in"] == 727
            assert baseline["tokens_out"] == 607
            assert (
                Decimal(baseline["counterfactual_cost_usd"])
                == rows[0]["cloud_cost_usd"]
            )
        if order != "canonical-only":
            assert rows[0]["terminal_failure_cause"] == "runtime_shutdown"


@pytest.mark.integration
async def test_real_postgres_harness_saving_keeps_counterfactual(
    integration_postgres_dsn: str,
) -> None:
    """Live harness shape retains the same baseline through both real writers."""
    from tests.unit.nodes.node_projection_delegation.test_harness_savings_counterfactual_omn20303 import (
        live_harness_terminal,
    )

    payload = live_harness_terminal()
    payload["tenant_id"] = str(HOUSE_TENANT_UUID)
    correlation = str(payload["correlation_id"])
    async with _provisioned_runner(integration_postgres_dsn) as (savings, conn):
        await conn.execute(
            "CREATE TABLE tenant_registry_mirror "
            "(tenant_slug text PRIMARY KEY, tenant_uuid uuid UNIQUE NOT NULL)"
        )
        await conn.execute(
            "INSERT INTO tenant_registry_mirror VALUES ($1, $2)",
            "harness-counterfactual-test",
            HOUSE_TENANT_UUID,
        )
        delegation = DelegationProjectionRunner(publish_fn=_noop_publish)
        delegation._db = savings.db
        for offset in range(2):
            meta = MessageMeta(
                topic=DELEGATE_SKILL_TOPIC,
                partition=0,
                offset=offset,
                fallback_id=correlation,
            )
            assert await delegation.project_event(
                DELEGATE_SKILL_TOPIC, dict(payload), meta
            )
            assert await savings.project_event(
                DELEGATE_SKILL_TOPIC, dict(payload), meta
            )
        rows = await conn.fetch(
            "SELECT d.premium_counterfactual, d.cost_savings_usd, "
            "s.cloud_cost_usd, s.local_cost_usd, s.savings_usd "
            "FROM delegation_events d JOIN savings_estimates s "
            "ON s.session_id=d.correlation_id AND s.tenant_id=d.tenant_id::text "
            "WHERE d.correlation_id=$1",
            correlation,
        )
    assert len(rows) == 1
    baseline = json.loads(rows[0]["premium_counterfactual"])
    assert (baseline["tokens_in"], baseline["tokens_out"]) == (13363, 204)
    assert Decimal(baseline["counterfactual_cost_usd"]) == rows[0]["cloud_cost_usd"]
    assert rows[0]["cost_savings_usd"] == rows[0]["savings_usd"]
    assert (
        rows[0]["savings_usd"] == rows[0]["cloud_cost_usd"] - rows[0]["local_cost_usd"]
    )
