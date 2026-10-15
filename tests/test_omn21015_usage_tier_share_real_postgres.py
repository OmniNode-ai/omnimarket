# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21015 AC4 on real Postgres: migration 0003 and the writer's tier recount and per-day shares.

The writer's statements have no Python branch, so only a real database proves them. It connects through the
OMN-19978 proof's _connect_or_skip, which reads the INTEGRATION_POSTGRES_* DSN, SKIPS without a reachable database,
and uses its own throwaway schema. It connects as whatever role the environment
gives; as a superuser that role bypasses row security, so this proves the SQL, not the writer role's view of it.

Failure modes:
P1  0003 drops or rewrites a row stored before it, or cannot run twice;
P2  0003 leaves the three-column key, so one model under two tiers on one day cannot be two rows;
P3  the writer's recount folds two tiers of one model into one row;
P4  the shares of a day are stale after a call, do not sum to 1, or count another tenant's or day's calls;
P5  a row whose share moved keeps its cursor, so a reader walking by cursor never sees the new share;
P6  the unsent read returns a row already marked sent (the snapshot cache drops it, and a run of drops marks the
    exposure stale), misses a row not marked sent (a publish that stopped part-way never heals), or a new call
    leaves a re-stamped row marked sent (its new share never goes out).
"""

from __future__ import annotations

from datetime import date

import pytest

from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_projection_usage_by_model_day import (
    HandlerProjectionUsageByModelDay,
)
from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_usage_by_model_day_writer import (
    _INSERT_CALL,
    _MARK_SENT,
    _READ_UNSENT,
    _RECOUNT_AGGREGATE,
    _RESHARE_DAY,
)
from omnimarket.nodes.node_projection_usage_by_model_day.models import (
    ModelUsageCallEvent,
)
from tests.test_omn19978_usage_by_model_day_real_postgres import (
    _MIGRATIONS,
    _connect_or_skip,
)

_SCHEMA = "omn21015_usage_tier_share_test"


def _scoped(statement: str) -> str:
    return statement.replace("public.", f"{_SCHEMA}.")


async def _write(
    conn,
    call_id: str,
    model: str,
    tier: str,
    *,
    tenant: str = "t",
    day: str = "2026-10-14",
) -> list:
    """The writer's statements, in its order, on one connection and transaction."""
    delta = HandlerProjectionUsageByModelDay().handle(
        ModelUsageCallEvent.model_validate(
            {
                "call_id": call_id,
                "model_name": model,
                "tenant_id": tenant,
                "cost_tier_name": tier,
                "timestamp": f"{day}T05:00:00Z",
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "estimated_cost_usd": 0.0,
                "usage_source": "measured",
            }
        )
    )
    usage_day = date.fromisoformat(delta.usage_day)
    async with conn.transaction():
        inserted = await conn.fetch(
            _scoped(_INSERT_CALL),
            delta.call_id,
            delta.tenant_id,
            usage_day,
            delta.model_id,
            delta.input_tokens,
            delta.output_tokens,
            delta.cost_usd,
            delta.occurred_at,
            delta.cost_tier_name,
            delta.usage_source.value,
        )
        if not inserted:
            return list(
                await conn.fetch(_scoped(_READ_UNSENT), delta.tenant_id, usage_day)
            )
        await conn.execute(
            _scoped(_RECOUNT_AGGREGATE),
            delta.tenant_id,
            usage_day,
            delta.model_id,
            delta.cost_tier_name,
        )
        return list(await conn.fetch(_scoped(_RESHARE_DAY), delta.tenant_id, usage_day))


async def _day(
    conn, day: str = "2026-10-14", tenant: str = "t"
) -> dict[tuple[str, str], dict]:
    rows = await conn.fetch(
        f"SELECT * FROM {_SCHEMA}.usage_by_model_day WHERE tenant_id = $1 AND usage_day = $2",
        tenant,
        date.fromisoformat(day),
    )
    return {(r["model_id"], r["cost_tier_name"]): dict(r) for r in rows}


@pytest.mark.integration
async def test_real_postgres_0003_keeps_old_rows_and_the_writer_shares_out_each_day() -> (
    None
):
    conn = await _connect_or_skip()
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
        base, measured, tiered = (
            _scoped(m.read_text(encoding="utf-8")) for m in _MIGRATIONS
        )
        await conn.execute(base)
        await conn.execute(measured)
        # Rows stored before 0003: a call and two aggregates of one day.
        await conn.execute(
            f"INSERT INTO {_SCHEMA}.usage_by_model_day_calls (call_id, tenant_id, usage_day, model_id, input_tokens, "
            "output_tokens, cost_usd, occurred_at) VALUES ('old', 't', '2026-10-14', 'qwen', 1, 1, 0, '2026-10-14T01:00Z')"
        )
        await conn.execute(
            f"INSERT INTO {_SCHEMA}.usage_by_model_day (tenant_id, usage_day, model_id, input_tokens, output_tokens, "
            "cost_usd, call_count) VALUES ('t', '2026-10-14', 'qwen', 3, 3, 0, 3), ('t', '2026-10-14', 'gemini', 1, 1, 0, 1)"
        )
        before = {
            r["model_id"]: r["projection_cursor"]
            for r in await conn.fetch(
                f"SELECT model_id, projection_cursor FROM {_SCHEMA}.usage_by_model_day"
            )
        }
        for _ in range(2):  # P1: re-runnable
            await conn.execute(tiered)
        kept = await _day(conn)
        assert set(kept) == {("qwen", "not_tier_routed"), ("gemini", "not_tier_routed")}
        assert {k[0]: v["call_count"] for k, v in kept.items()} == {
            "qwen": 3,
            "gemini": 1,
        }
        assert {k: v["call_share"] for k, v in kept.items()} == dict.fromkeys(kept)
        assert {k[0]: v["projection_cursor"] for k, v in kept.items()} == before
        old_call = await conn.fetchval(
            f"SELECT cost_tier_name FROM {_SCHEMA}.usage_by_model_day_calls"
        )
        assert old_call == "not_tier_routed"
        key = await conn.fetchval(
            "SELECT array_agg(a.attname::text ORDER BY array_position(c.conkey, a.attnum)) FROM pg_constraint c "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey) "
            "WHERE c.conrelid = $1::regclass AND c.contype = 'p'",
            f"{_SCHEMA}.usage_by_model_day",
        )
        assert key == ["tenant_id", "usage_day", "model_id", "cost_tier_name"]  # P2

        # P3, P4, P5: one model under a new tier on the same day is a new row; every share of the day moves.
        old_max = max(v["projection_cursor"] for v in kept.values())
        returned = await _write(conn, "c1", "qwen", "local")
        day = await _day(conn)
        assert {k: v["call_share"] for k, v in day.items()} == {
            ("qwen", "not_tier_routed"): pytest.approx(3 / 5),
            ("gemini", "not_tier_routed"): pytest.approx(1 / 5),
            ("qwen", "local"): pytest.approx(1 / 5),
        }
        assert sum(v["call_share"] for v in day.values()) == pytest.approx(1.0)
        assert min(v["projection_cursor"] for v in day.values()) > old_max
        assert {(r["model_id"], r["cost_tier_name"]) for r in returned} == set(day)

        # Another tenant's and another day's calls never enter this day's shares; a replay changes nothing.
        await _write(conn, "o1", "qwen", "local", tenant="other")
        await _write(conn, "d1", "qwen", "local", day="2026-10-13")
        stored = await _day(conn)
        assert {k: v["call_share"] for k, v in stored.items()} == {
            k: v["call_share"] for k, v in day.items()
        }

        def keyed(rows) -> dict[tuple[str, str], tuple]:
            return {
                (r["model_id"], r["cost_tier_name"]): (
                    r["call_share"],
                    r["projection_cursor"],
                )
                for r in rows
            }

        # P6: nothing is marked sent yet, so a replay sends the whole day as stored.
        replayed = await _write(conn, "c1", "qwen", "local")
        assert keyed(replayed) == {
            k: (v["call_share"], v["projection_cursor"]) for k, v in stored.items()
        }
        # A publish that sent one row and failed: only that row is marked, the replay sends the other two.
        first = stored[("qwen", "not_tier_routed")]["projection_cursor"]
        await conn.execute(_scoped(_MARK_SENT), "t", date(2026, 10, 14), [first])
        assert set(keyed(await _write(conn, "c1", "qwen", "local"))) == {
            ("gemini", "not_tier_routed"),
            ("qwen", "local"),
        }
        # The whole day sent: a duplicate sends nothing.
        await conn.execute(
            _scoped(_MARK_SENT),
            "t",
            date(2026, 10, 14),
            [v["projection_cursor"] for v in stored.values()],
        )
        assert await _write(conn, "c1", "qwen", "local") == []
        # A new call re-stamps every row of the day, so every row is unsent again.
        await _write(conn, "c2", "gemini", "not_tier_routed")
        assert set(keyed(await _write(conn, "c2", "gemini", "not_tier_routed"))) == set(
            await _day(conn)
        )
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()
