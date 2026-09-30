# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19978: real-Postgres proof of the usage-by-model-day writer SQL.

The writer's insert-then-recount statements have no Python branch, so only a real
database proves them (the OMN-15905 class). SKIPS without a reachable database and
uses its own throwaway schema, like the topic-activity and hook-event proofs.
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote_plus

import asyncpg
import pytest

from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_projection_usage_by_model_day import (
    HandlerProjectionUsageByModelDay,
)
from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_usage_by_model_day_writer import (
    _INSERT_CALL,
    _RECOUNT_AGGREGATE,
)
from tests.test_omn19978_usage_by_model_day import CALLS, _event

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_usage_by_model_day"
    / "migrations/0000_create_usage_by_model_day.sql"
)
_SCHEMA = "omn19978_usage_by_model_day_test"


async def _connect_or_skip() -> asyncpg.Connection:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip("INTEGRATION_POSTGRES_PASSWORD not set -- skipping usage DB proof")
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    dsn = f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
    try:
        return await asyncpg.connect(dsn)
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover
        pytest.skip(f"no reachable Postgres for usage write-path proof: {exc}")


def _scoped(statement: str) -> str:
    return statement.replace("public.", f"{_SCHEMA}.")


async def _apply_all(conn: asyncpg.Connection) -> None:
    fold = HandlerProjectionUsageByModelDay()
    for call in CALLS:
        delta = fold.handle(_event(call))
        day = date.fromisoformat(delta.usage_day)
        inserted = await conn.fetch(
            _scoped(_INSERT_CALL),
            delta.call_id,
            delta.tenant_id,
            day,
            delta.model_id,
            delta.input_tokens,
            delta.output_tokens,
            delta.cost_usd,
            delta.occurred_at,
        )
        if inserted:
            await conn.fetch(
                _scoped(_RECOUNT_AGGREGATE), delta.tenant_id, day, delta.model_id
            )


@pytest.mark.integration
async def test_real_postgres_ten_calls_give_four_rows_and_a_replay_changes_nothing() -> (
    None
):
    conn = await _connect_or_skip()
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
        await conn.execute(
            _MIGRATION.read_text(encoding="utf-8").replace("public.", f"{_SCHEMA}.")
        )
        await _apply_all(conn)
        rows = await conn.fetch(
            f"SELECT * FROM {_SCHEMA}.usage_by_model_day ORDER BY usage_day, model_id"
        )
        assert len(rows) == 4
        assert sum(r["input_tokens"] for r in rows) == sum(c[3] for c in CALLS)
        assert sum(r["output_tokens"] for r in rows) == sum(c[4] for c in CALLS)
        assert sum(r["cost_usd"] for r in rows) == sum(Decimal(c[5]) for c in CALLS)
        assert sum(r["call_count"] for r in rows) == 10
        cursors = sorted(r["projection_cursor"] for r in rows)
        await _apply_all(conn)  # replay: every call already present
        again = await conn.fetch(
            f"SELECT * FROM {_SCHEMA}.usage_by_model_day ORDER BY usage_day, model_id"
        )
        assert [dict(r) for r in again] == [dict(r) for r in rows]
        assert sorted(r["projection_cursor"] for r in again) == cursors
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()
