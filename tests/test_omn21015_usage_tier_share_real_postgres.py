# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21015 AC4 on real Postgres: migration 0003, the writer's per-call statements, and the key they follow.

Jonah's ruling on OMN-21015 (2026-10-15 20:23Z): expand, then contract, with the writer following the key in force.
0003 adds the tier and a four-column unique index and keeps the three-column primary key, so the writer that runs on
lanes today still writes. While that key is the key, the new writer folds a model's day into one row under
not_tier_routed, as today, and never raises; once the contract migration (OMN-21161) makes the four-column index the
key, it splits by tier. The writer's statements have no Python branch to test without a database, so each case runs
the writer's own write_usage_call on its own throwaway schema. As a superuser the connection bypasses row security,
so this proves the SQL, not the writer role's view of it.

Failure modes:
P1  0003 drops or rewrites a row stored before it, or cannot run twice;
P2  0003 swaps the primary key, so the writer deployed today stops matching a constraint, or adds no four-column
    unique index for the new key to become;
P6  the writer deployed today (omnimarket dev ee9699b0, statements copied below) fails after 0003;
P7  while the three-column key is the key, a model under a second tier that day raises (a unique violation the
    projection runner classes recoverable and re-reads forever), or the call is lost;
P3  once the four-column key is the key, the recount folds two tiers of one model into one row, or the day keeps the
    interim not_tier_routed row beside the split rows, counting its calls twice;
P4  the shares of a day are stale after a call, do not sum to 1, or count another tenant's or day's calls;
P5  a row whose share moved keeps its cursor, so a reader walking by cursor never sees the new share;
P8  0003 leaves the writer role without DELETE, so removing the folded row after the switch is refused.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_projection_usage_by_model_day import (
    HandlerProjectionUsageByModelDay,
)
from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_usage_by_model_day_writer import (
    write_usage_call,
)
from omnimarket.nodes.node_projection_usage_by_model_day.models import (
    ModelUsageCallEvent,
)
from tests.test_omn19978_usage_by_model_day_real_postgres import (
    _MIGRATIONS,
    _connect_or_skip,
)

_SCHEMA = "omn21015_usage_tier_share_test"

# The writer on lanes today: omnimarket dev ee9699b0, handler_usage_by_model_day_writer.py, copied as it runs there.
_DEPLOYED_INSERT_CALL = """
    INSERT INTO public.usage_by_model_day_calls (
        call_id, tenant_id, usage_day, model_id, input_tokens, output_tokens,
        cost_usd, occurred_at, usage_source
    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
    ON CONFLICT (call_id) DO NOTHING
    RETURNING call_id
"""
_DEPLOYED_RECOUNT = """
    INSERT INTO public.usage_by_model_day (
        tenant_id, usage_day, model_id, input_tokens, output_tokens,
        cost_usd, measured_cost_usd, unmeasured_call_count, call_count, updated_at
    )
    SELECT tenant_id, usage_day, model_id, SUM(input_tokens), SUM(output_tokens),
           SUM(cost_usd),
           SUM(cost_usd) FILTER (WHERE usage_source = 'measured'),
           COUNT(*) FILTER (WHERE usage_source <> 'measured'),
           COUNT(*), clock_timestamp()
    FROM public.usage_by_model_day_calls
    WHERE tenant_id = $1 AND usage_day = $2 AND model_id = $3
    GROUP BY tenant_id, usage_day, model_id
    ON CONFLICT (tenant_id, usage_day, model_id) DO UPDATE SET
        input_tokens = EXCLUDED.input_tokens,
        output_tokens = EXCLUDED.output_tokens,
        cost_usd = EXCLUDED.cost_usd,
        measured_cost_usd = EXCLUDED.measured_cost_usd,
        unmeasured_call_count = EXCLUDED.unmeasured_call_count,
        call_count = EXCLUDED.call_count,
        updated_at = EXCLUDED.updated_at,
        projection_cursor = EXCLUDED.projection_cursor
    RETURNING model_id, call_count
"""

# What the contract migration (OMN-21161) does, in this test's schema, so the writer can be read on both sides of it.
_CONTRACT = f"""
    ALTER TABLE {_SCHEMA}.usage_by_model_day DROP CONSTRAINT usage_by_model_day_pkey;
    ALTER TABLE {_SCHEMA}.usage_by_model_day
        ADD CONSTRAINT usage_by_model_day_pkey PRIMARY KEY USING INDEX usage_by_model_day_tier_key;
"""


def _scoped(statement: str) -> str:
    return statement.replace("public.", f"{_SCHEMA}.")


async def _fresh(conn) -> None:
    await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
    for migration in _MIGRATIONS:
        await conn.execute(_scoped(migration.read_text(encoding="utf-8")))


async def _write(
    conn,
    call_id: str,
    model: str,
    tier: str,
    *,
    tenant: str = "t",
    day: str = "2026-10-14",
):
    """The writer's own per-call statements, through write_usage_call, in one transaction on this schema."""
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
    async with conn.transaction():
        return await write_usage_call(conn, delta, schema=_SCHEMA)


async def _day(
    conn, day: str = "2026-10-14", tenant: str = "t"
) -> dict[tuple[str, str], dict]:
    rows = await conn.fetch(
        f"SELECT * FROM {_SCHEMA}.usage_by_model_day WHERE tenant_id = $1 AND usage_day = $2",
        tenant,
        date.fromisoformat(day),
    )
    return {(r["model_id"], r["cost_tier_name"]): dict(r) for r in rows}


async def _key(conn, name: str) -> list[str]:
    return await conn.fetchval(
        "SELECT array_agg(a.attname::text ORDER BY array_position(i.indkey::int2[], a.attnum)) "
        "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey) "
        "WHERE i.indrelid = $1::regclass AND c.relname = $2",
        f"{_SCHEMA}.usage_by_model_day",
        name,
    )


@pytest.mark.integration
async def test_real_postgres_0003_keeps_old_rows_and_both_keys() -> None:
    conn = await _connect_or_skip()
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
        base, measured, tiered = (
            _scoped(m.read_text(encoding="utf-8")) for m in _MIGRATIONS
        )
        await conn.execute(base)
        await conn.execute(measured)
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
        assert (
            await conn.fetchval(
                f"SELECT cost_tier_name FROM {_SCHEMA}.usage_by_model_day_calls"
            )
            == "not_tier_routed"
        )
        # P2: the three-column key stays the key, and the four-column unique index is ready to become it.
        assert await _key(conn, "usage_by_model_day_pkey") == [
            "tenant_id",
            "usage_day",
            "model_id",
        ]
        assert await _key(conn, "usage_by_model_day_tier_key") == [
            "tenant_id",
            "usage_day",
            "model_id",
            "cost_tier_name",
        ]
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()


@pytest.mark.integration
async def test_real_postgres_the_deployed_writer_still_writes_after_0003() -> None:
    conn = await _connect_or_skip()
    try:
        await _fresh(conn)
        day = date(2026, 10, 14)
        async with conn.transaction():  # P6
            inserted = await conn.fetch(
                _scoped(_DEPLOYED_INSERT_CALL),
                "deployed",
                "t",
                day,
                "qwen",
                1,
                1,
                0,
                datetime(2026, 10, 14, 1, tzinfo=UTC),
                "measured",
            )
            recounted = await conn.fetch(_scoped(_DEPLOYED_RECOUNT), "t", day, "qwen")
        assert [r["call_id"] for r in inserted] == ["deployed"]
        assert [(r["model_id"], r["call_count"]) for r in recounted] == [("qwen", 1)]
        assert set(await _day(conn)) == {("qwen", "not_tier_routed")}
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()


@pytest.mark.integration
async def test_real_postgres_under_the_old_key_two_tiers_fold_into_one_row_and_never_raise() -> (
    None
):
    conn = await _connect_or_skip()
    try:
        await _fresh(conn)
        # P7: the second tier of one model on one day is the case that stalled the consumer on an expand-only 0003.
        await _write(conn, "c1", "qwen", "local")
        returned = await _write(conn, "c2", "qwen", "cheap_cloud")
        await _write(conn, "g1", "gemini", "local")
        day = await _day(conn)
        assert set(day) == {("qwen", "not_tier_routed"), ("gemini", "not_tier_routed")}
        assert day[("qwen", "not_tier_routed")]["call_count"] == 2
        assert {(r["model_id"], r["cost_tier_name"]) for r in returned.rows} == {
            ("qwen", "not_tier_routed")
        }
        assert returned.removed == []
        tiers = await conn.fetch(
            f"SELECT call_id, cost_tier_name FROM {_SCHEMA}.usage_by_model_day_calls ORDER BY call_id"
        )
        assert {r["call_id"]: r["cost_tier_name"] for r in tiers} == {
            "c1": "local",
            "c2": "cheap_cloud",
            "g1": "local",
        }
        assert {k: v["call_share"] for k, v in day.items()} == {
            ("qwen", "not_tier_routed"): pytest.approx(2 / 3),
            ("gemini", "not_tier_routed"): pytest.approx(1 / 3),
        }
        assert (
            await _write(conn, "c2", "qwen", "cheap_cloud") is None
        )  # a replay changes nothing
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()


@pytest.mark.integration
async def test_real_postgres_under_the_tier_key_the_writer_splits_by_tier() -> None:
    conn = await _connect_or_skip()
    try:
        await _fresh(conn)
        # Interim: two tiers of qwen folded under not_tier_routed, as the old key requires.
        await _write(conn, "c1", "qwen", "local")
        await _write(conn, "c2", "qwen", "cheap_cloud")
        old = await _day(conn)
        old_max = max(v["projection_cursor"] for v in old.values())
        await conn.execute(_CONTRACT)
        # P3: the first call after the contract migration splits the model's day by tier, and the folded row goes.
        returned = await _write(conn, "c3", "qwen", "local")
        day = await _day(conn)
        assert set(day) == {("qwen", "local"), ("qwen", "cheap_cloud")}
        assert {k: v["call_count"] for k, v in day.items()} == {
            ("qwen", "local"): 2,
            ("qwen", "cheap_cloud"): 1,
        }
        # P4, P5: every share of the day moves, sums to 1, and every row's cursor moves past the old ones.
        assert {k: v["call_share"] for k, v in day.items()} == {
            ("qwen", "local"): pytest.approx(2 / 3),
            ("qwen", "cheap_cloud"): pytest.approx(1 / 3),
        }
        assert sum(v["call_share"] for v in day.values()) == pytest.approx(1.0)
        assert min(v["projection_cursor"] for v in day.values()) > old_max
        assert {(r["model_id"], r["cost_tier_name"]) for r in returned.rows} == set(day)
        # The folded interim row is removed, and reported so the writer can publish its tombstone.
        assert [(r["model_id"], r["cost_tier_name"]) for r in returned.removed] == [
            ("qwen", "not_tier_routed")
        ]
        # Another tenant's and another day's calls never enter this day's shares.
        await _write(conn, "o1", "qwen", "local", tenant="other")
        await _write(conn, "d1", "qwen", "local", day="2026-10-13")
        assert {k: v["call_share"] for k, v in (await _day(conn)).items()} == {
            k: v["call_share"] for k, v in day.items()
        }
        assert (await _day(conn, day="2026-10-13"))[("qwen", "local")][
            "call_share"
        ] == pytest.approx(1.0)
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()


@pytest.mark.integration
async def test_real_postgres_0003_grants_the_writer_delete() -> None:
    conn = await _connect_or_skip()
    try:
        # P8: the role exists on a lane (migration 0001 creates it); a throwaway cluster may lack it.
        await conn.execute(
            "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tenant_projection_writer') "
            "THEN CREATE ROLE tenant_projection_writer NOLOGIN NOSUPERUSER NOBYPASSRLS; END IF; END $$"
        )
        await _fresh(conn)
        assert await conn.fetchval(
            "SELECT has_table_privilege('tenant_projection_writer', $1, 'DELETE')",
            f"{_SCHEMA}.usage_by_model_day",
        )
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()
