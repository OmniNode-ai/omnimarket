# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22675: the savings view counts unpriced calls in one pass, with the same answer.

Measured 2026-11-06 on the dev lane: reading ``projection_delegation_savings``
took 31.8 s, past the projection API's 15 s bound, so the exposure answered 503
``projection_database_unavailable``. 30.1 s of it was migration 098's
``unpriced`` CTE: a NOT EXISTS probe of the ``savings_sessions`` CTE for every
unpriced ``delegation_events`` row, planned as a Nested Loop Anti Join (73,368
CTE rows scanned per probe, 1,701 probes).

Migration 101 reads the same count from the flags ``combined_sessions`` already
carries from its one hash left join. Two things have to hold:

- the answer is the answer 098 gave, for every column the view serves, on a
  population that has the cases the probe distinguished (an unpriced call a
  savings estimate prices, one nothing prices, a priced call, a free local
  call, a savings estimate with no event, the house tenant spelled both ways);
- the view no longer holds the per-row probe.

The first is checked against the 098 definition itself, created beside the new
view under another name in the same schema, so the oracle is the old SQL and
not numbers typed here. The second is checked on the stored view definition,
and the same check run on the 098 definition must report the probe: a check
that cannot see the old shape proves nothing about the new one.
"""

from __future__ import annotations

import re
import uuid
from decimal import Decimal
from typing import Any

import asyncpg
import pytest

from tests.test_omn17426_savings_aggregate_views_per_tenant import (
    _CONCURRENT_INDEX,
    _ENSURE_ROLES,
    _MIGRATION_NODES,
    _NODES,
    _PUBLIC_SCHEMA,
    HOUSE_SLUG,
    HOUSE_UUID,
    SAVINGS,
)
from tests.test_omn20717_savings_unpriced_call_count_real_postgres import WHEN, _call

pytestmark = pytest.mark.integration

_SAVINGS_MIGRATIONS = _NODES / "node_projection_savings" / "migrations"
_OLD = _SAVINGS_MIGRATIONS / "098_delegation_savings_measured_population.sql"
_NEW = _SAVINGS_MIGRATIONS / "101_delegation_savings_unpriced_single_pass.sql"
ORACLE = "projection_delegation_savings_098"

MIXED = uuid.UUID("a1111111-1111-4111-8111-111111111111")
ALL_UNPRICED = uuid.UUID("a2222222-2222-4222-8222-222222222222")
PRICED_ONLY = uuid.UUID("a3333333-3333-4333-8333-333333333333")
ESTIMATE_ONLY = uuid.UUID("a4444444-4444-4444-8444-444444444444")
PRICED_BY_ESTIMATE = uuid.UUID("a5555555-5555-4555-8555-555555555555")

#: Every column the view serves, so a changed value anywhere fails the oracle.
_COLUMNS = (
    "tenant_id",
    "cumulative_savings_usd",
    "cumulative_local_cost_usd",
    "cumulative_cloud_cost_usd",
    "baseline_model",
    "pricing_manifest_version",
    "session_count",
    "sessions",
    "captured_at",
    "provisioned",
    "latest_projection_updated_at",
    "cumulative_counterfactual_baseline_usd",
    "unpriced_call_count",
    "measured_run_count",
    "estimated_run_count",
    "unknown_run_count",
)

# A correlated probe of the savings CTE: ``EXISTS ( SELECT 1 FROM savings_sessions``.
_PER_ROW_SAVINGS_PROBE = re.compile(
    r"EXISTS\s*\(\s*SELECT\s+1\s+FROM\s+savings_sessions", re.IGNORECASE
)


def _prepared(path: Any, *, view_name: str | None = None) -> str:
    sql = _CONCURRENT_INDEX.sub("CREATE INDEX", path.read_text("utf-8"))
    sql = _PUBLIC_SCHEMA.sub("", sql)
    if view_name is not None:
        sql = sql.replace(SAVINGS, view_name)
    return sql


async def _apply_until_new(conn: asyncpg.Connection) -> None:
    """Every migration of both nodes up to, and not including, 101."""
    for node in _MIGRATION_NODES:
        for migration in sorted(
            (_NODES / node / "migrations").glob("*.sql"), key=lambda p: p.name
        ):
            if migration.name == _NEW.name:
                continue
            try:
                await conn.execute(_prepared(migration))
            except Exception as exc:
                raise AssertionError(
                    f"migration {migration.name} failed: {exc}"
                ) from exc


@pytest.fixture
async def both_views(postgres_fixture: asyncpg.Connection) -> Any:
    conn = postgres_fixture
    schema = f"omn22675_{uuid.uuid4().hex[:12]}"
    await conn.execute(_ENSURE_ROLES)
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    await conn.execute(f'SET search_path TO "{schema}", public')
    try:
        await _apply_until_new(conn)
        # The 098 definition, kept beside the new view as the oracle.
        await conn.execute(_prepared(_OLD, view_name=ORACLE))
        old_definition = await conn.fetchval(
            "SELECT pg_get_viewdef($1::regclass, true)", SAVINGS
        )
        await conn.execute(_prepared(_NEW))
        new_definition = await conn.fetchval(
            "SELECT pg_get_viewdef($1::regclass, true)", SAVINGS
        )
        await _seed(conn)
        yield conn, old_definition, new_definition
    finally:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


async def _estimate(
    conn: asyncpg.Connection, session: str, tenant: str, *, local_cost: Decimal
) -> None:
    await conn.execute(
        """
        INSERT INTO savings_estimates (
            event_timestamp, session_id, model_local, model_cloud_baseline,
            local_cost_usd, cloud_cost_usd, savings_usd, tenant_id,
            task_type, prompt_tokens, completion_tokens, usage_source
        ) VALUES ($1, $2, 'qwen', 'baseline', $3, $3 + 0.06, 0.06, $4, 'research',
                  10, 10, 'measured')
        """,
        WHEN,
        session,
        local_cost,
        tenant,
    )


async def _seed(conn: asyncpg.Connection) -> None:
    # Mixed: priced, unpriced and a free local call in one tenant.
    await _call(conn, "m-priced", MIXED, Decimal("0.25"), "metered", Decimal("0.5"))
    await _call(conn, "m-unpriced-1", MIXED, None, "no_cost_model")
    await _call(conn, "m-unpriced-2", MIXED, None, "no_cost_model")
    await _call(conn, "m-free", MIXED, Decimal(0), "free_local")
    # Nothing priced at all.
    await _call(conn, "u-1", ALL_UNPRICED, None, "no_cost_model")
    await _call(conn, "u-2", ALL_UNPRICED, None, "no_cost_model")
    # Nothing unpriced.
    await _call(conn, "p-1", PRICED_ONLY, Decimal("0.1"), "metered")
    # Unpriced calls a savings estimate prices: not unpriced on this page.
    await _call(conn, "e-1", PRICED_BY_ESTIMATE, None, "no_cost_model")
    await _call(conn, "e-2", PRICED_BY_ESTIMATE, None, "no_cost_model")
    await _call(conn, "e-3", PRICED_BY_ESTIMATE, None, "no_cost_model")
    await _estimate(conn, "e-1", str(PRICED_BY_ESTIMATE), local_cost=Decimal("0.03"))
    await _estimate(conn, "e-2", str(PRICED_BY_ESTIMATE), local_cost=Decimal("0.04"))
    # A savings estimate whose run has no delegation event.
    await _estimate(
        conn, "only-estimate", str(ESTIMATE_ONLY), local_cost=Decimal("0.5")
    )
    # The house tenant: the event spells it as the uuid, the estimate as the slug.
    await _call(conn, "h-1", HOUSE_UUID, None, "no_cost_model")
    await _call(conn, "h-2", HOUSE_UUID, None, "no_cost_model")
    await _estimate(conn, "h-1", HOUSE_SLUG, local_cost=Decimal("0.07"))


async def _rows(conn: asyncpg.Connection, view: str) -> dict[str, dict[str, Any]]:
    rows = await conn.fetch(f"SELECT * FROM {view} ORDER BY tenant_id")
    return {row["tenant_id"]: {c: row[c] for c in _COLUMNS} for row in rows}


async def test_the_new_view_answers_what_098_answered_for_every_tenant(
    both_views: Any,
) -> None:
    conn, _old, _new = both_views
    old = await _rows(conn, ORACLE)
    new = await _rows(conn, SAVINGS)
    seeded = {
        str(t)
        for t in (
            MIXED,
            ALL_UNPRICED,
            PRICED_ONLY,
            ESTIMATE_ONLY,
            PRICED_BY_ESTIMATE,
            HOUSE_UUID,
        )
    }
    assert seeded <= set(old), "positive control: the oracle has every seeded tenant"
    assert old.keys() == new.keys()
    for tenant, old_row in old.items():
        assert new[tenant] == old_row, f"tenant {tenant} changed"


async def test_the_counts_the_oracle_gives_are_the_ones_the_cases_were_built_for(
    both_views: Any,
) -> None:
    # Guards the equality above from passing on a population that cannot tell
    # a wrong count from a right one: every case has a distinct expected count.
    conn, _old, _new = both_views
    new = await _rows(conn, SAVINGS)
    counts = {t: r["unpriced_call_count"] for t, r in new.items()}
    assert counts[str(MIXED)] == 2
    assert counts[str(ALL_UNPRICED)] == 2
    assert counts[str(PRICED_ONLY)] == 0
    assert counts[str(PRICED_BY_ESTIMATE)] == 1
    assert counts[str(ESTIMATE_ONLY)] == 0
    assert counts[str(HOUSE_UUID)] == 1


async def test_the_new_view_holds_no_per_row_probe_of_the_savings_sessions(
    both_views: Any,
) -> None:
    _conn, _old, new_definition = both_views
    assert not _PER_ROW_SAVINGS_PROBE.search(new_definition)
    assert not re.search(r"\bunpriced\s+AS\s*\(", new_definition, re.IGNORECASE)


async def test_the_same_check_reports_the_probe_in_the_098_definition(
    both_views: Any,
) -> None:
    # The failing control: the check above must be able to see the old shape.
    _conn, old_definition, _new = both_views
    assert _PER_ROW_SAVINGS_PROBE.search(old_definition)
    assert re.search(r"\bunpriced\s+AS\s*\(", old_definition, re.IGNORECASE)


async def test_the_column_contract_and_invoker_rights_are_unchanged(
    both_views: Any,
) -> None:
    conn, _old, _new = both_views
    schema = await conn.fetchval("SELECT current_schema()")

    async def columns(view: str) -> list[tuple[str, str]]:
        return [
            (r["column_name"], r["data_type"])
            for r in await conn.fetch(
                "SELECT column_name, data_type FROM information_schema.columns"
                " WHERE table_schema = $1 AND table_name = $2"
                " ORDER BY ordinal_position",
                schema,
                view,
            )
        ]

    assert await columns(SAVINGS) == await columns(ORACLE)
    options = await conn.fetchval(
        "SELECT c.reloptions FROM pg_class c JOIN pg_namespace n"
        " ON n.oid = c.relnamespace WHERE n.nspname = $1 AND c.relname = $2",
        schema,
        SAVINGS,
    )
    assert "security_invoker=true" in (options or [])
