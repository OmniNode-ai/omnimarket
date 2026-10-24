# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real SQL population proof, using only the caller's disposable CI database.

Failure modes: unmeasured tokens/runs disappear; unmeasured money leaks into
totals; an estimate replaces a run's tokens; savings-only runs disappear;
tenants blend; tied estimates pick an arbitrary row; replacement drops column
order, invoker rights or grants. A skipped database is not a RED/GREEN receipt.
The suite starts no database. Run in hosted CI, never against the dev database.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
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
)

pytestmark = pytest.mark.integration
TENANT = uuid.UUID("11111111-1111-4111-8111-111111111111")
OTHER = uuid.UUID("22222222-2222-4222-8222-222222222222")
WHEN = datetime(2026, 10, 20, 12, tzinfo=UTC)
OVERVIEW = "projection_cost_savings_overview"
EVIDENCE = "projection_delegation_savings"


@pytest.fixture
async def views(postgres_fixture: asyncpg.Connection) -> Any:
    conn = postgres_fixture
    schema = f"omn21206_{uuid.uuid4().hex}"
    await conn.execute(_ENSURE_ROLES)
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    await conn.execute(f'SET search_path TO "{schema}", public')
    try:
        statements = [
            _PUBLIC_SCHEMA.sub(
                "", _CONCURRENT_INDEX.sub("CREATE INDEX", migration.read_text("utf-8"))
            )
            for node in _MIGRATION_NODES
            for migration in sorted((_NODES / node / "migrations").glob("*.sql"))
        ]
        await conn.execute("\n\n".join(statements))
        yield conn
    finally:
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


async def event(
    conn: asyncpg.Connection,
    run: str,
    basis: str,
    tokens: int,
    cost: int,
    tenant: uuid.UUID = TENANT,
) -> None:
    await conn.execute(
        """INSERT INTO delegation_events (
            correlation_id, session_id, tenant_id, task_type, model_name,
            cost_usd, cost_savings_usd, tokens_input, tokens_output,
            timestamp, created_at, cost_measurement_source
        ) VALUES ($1, $1, $2, 'document', 'served-model', $3, $4,
                  $5, 0, $6, $6, $7)""",
        run,
        tenant,
        Decimal(cost),
        Decimal(cost * 2),
        tokens,
        WHEN,
        basis,
    )


async def estimate(
    conn: asyncpg.Connection,
    run: str,
    tokens: int,
    cost: int,
    *,
    row_id: int = 1,
    model: str = "served-model",
    created: datetime = WHEN,
    updated: datetime = WHEN,
    tenant: uuid.UUID = TENANT,
) -> None:
    await conn.execute(
        """INSERT INTO savings_estimates (
            id, event_timestamp, created_at, updated_at, session_id,
            model_local, model_cloud_baseline, local_cost_usd,
            cloud_cost_usd, savings_usd, tenant_id, prompt_tokens,
            completion_tokens, usage_source
        ) VALUES ($1, $2, $10, $11, $3, $4, 'baseline', $5, $6, $7,
                  $8, $9, 0, 'measured')""",
        uuid.UUID(int=row_id),
        WHEN,
        run,
        model,
        Decimal(cost),
        Decimal(cost * 3),
        Decimal(cost * 2),
        str(tenant),
        tokens,
        created,
        updated,
    )


async def row(conn: asyncpg.Connection, view: str, tenant: uuid.UUID = TENANT) -> Any:
    result = await conn.fetchrow(
        f"SELECT * FROM {view} WHERE tenant_id=$1", str(tenant)
    )
    assert result is not None, "positive control: the seeded tenant is visible"
    return result


async def population(conn: asyncpg.Connection) -> None:
    await event(conn, "matched", "metered", 14, 1)
    await estimate(conn, "matched", 910, 1)
    await event(conn, "estimated", "manifest_compute", 22, 4)
    await event(conn, "unknown", "", 33, 7)
    await estimate(conn, "savings-only", 44, 2, row_id=2)
    await event(conn, "other-tenant", "metered", 500, 9, OTHER)


async def test_all_runs_and_tokens_but_only_measured_money(
    views: asyncpg.Connection,
) -> None:
    await population(views)
    overview = await row(views, OVERVIEW)
    evidence = await row(views, EVIDENCE)
    assert overview["tokens_total"] == 113
    assert overview["run_count"] == evidence["session_count"] == 4
    assert overview["total_cost_usd"] == evidence["cumulative_local_cost_usd"] == 3
    assert overview["total_savings_usd"] == evidence["cumulative_savings_usd"] == 6
    for result in (overview, evidence):
        assert result["measured_run_count"] == 2
        assert result["estimated_run_count"] == 1
        assert result["unknown_run_count"] == 1
    assert (await row(views, OVERVIEW, OTHER))["tokens_total"] == 500
    assert (await row(views, EVIDENCE, OTHER))["cumulative_savings_usd"] == 18


@pytest.mark.parametrize("savings_only_count", [0, 1, 2])
async def test_tokens_equal_events_plus_savings_only_rows(
    views: asyncpg.Connection,
    savings_only_count: int,
) -> None:
    await population(views)
    # Keep the event total constant while ensuring output tokens contribute.
    await views.execute(
        "UPDATE delegation_events SET tokens_input=19, tokens_output=3 "
        "WHERE tenant_id=$1 AND correlation_id='estimated'",
        TENANT,
    )
    if savings_only_count == 0:
        await views.execute(
            "DELETE FROM savings_estimates WHERE tenant_id=$1 "
            "AND session_id='savings-only'",
            str(TENANT),
        )
    else:
        # Counting raw estimates, or selecting by id alone, adds the wrong 900.
        await estimate(
            views,
            "savings-only",
            900,
            2,
            row_id=3,
            model="older-estimate",
            created=WHEN - timedelta(days=1),
            updated=WHEN - timedelta(days=1),
        )
    if savings_only_count == 2:
        # Zero tokens still contributes one combined run.
        await estimate(views, "zero-token-only", 0, 1, row_id=4)
    # A higher-id estimate with the same key in another tenant must not win.
    await estimate(
        views,
        "savings-only",
        999,
        1,
        row_id=99,
        model="other-tenant-estimate",
        tenant=OTHER,
    )
    usage = await views.fetchval(
        "SELECT total_tokens FROM projection_delegation_token_usage WHERE tenant_id=$1",
        TENANT,
    )
    events = await views.fetchrow(
        "SELECT count(*) AS runs, sum(tokens_input+tokens_output) AS tokens "
        "FROM delegation_events WHERE tenant_id=$1",
        TENANT,
    )
    assert events is not None
    assert events["runs"] == 3
    assert events["tokens"] == usage == 69
    only = await views.fetchrow(
        """WITH latest AS (
            SELECT DISTINCT ON (session_id) session_id,
                COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0) AS tokens
            FROM savings_estimates WHERE tenant_id=$1::text
              AND session_id IS NOT NULL
            ORDER BY session_id,
                COALESCE(updated_at,created_at,event_timestamp) DESC, id DESC
        ), missing AS (
            SELECT session_id AS run_key FROM latest
            EXCEPT
            SELECT COALESCE(NULLIF(correlation_id,''),NULLIF(session_id,''),id::text)
            FROM delegation_events WHERE tenant_id=$1::uuid
        )
        SELECT count(*) AS runs, COALESCE(sum(tokens),0) AS tokens
        FROM latest JOIN missing ON missing.run_key=latest.session_id""",
        str(TENANT),
    )
    assert only is not None
    assert only["runs"] == savings_only_count
    assert only["tokens"] == (44 if savings_only_count else 0)
    overview = await row(views, OVERVIEW)
    evidence = await row(views, EVIDENCE)
    assert overview["tokens_total"] - usage == only["tokens"]
    assert overview["run_count"] - events["runs"] == only["runs"]
    assert evidence["session_count"] == events["runs"] + only["runs"]
    assert (await row(views, OVERVIEW, OTHER))["tokens_total"] == 1499
    sessions = evidence["sessions"]
    if isinstance(sessions, str):
        sessions = json.loads(sessions)
    assert len(sessions) == events["runs"] + only["runs"]
    matched = next(s for s in sessions if s["session_id"] == "matched")
    assert (matched["prompt_tokens"], matched["completion_tokens"]) == (14, 0)
    completed = next(s for s in sessions if s["session_id"] == "estimated")
    assert (completed["prompt_tokens"], completed["completion_tokens"]) == (19, 3)


@pytest.mark.parametrize(
    ("view", "cost_field", "savings_field"),
    [
        (OVERVIEW, "total_cost_usd", "total_savings_usd"),
        (EVIDENCE, "cumulative_local_cost_usd", "cumulative_savings_usd"),
    ],
)
async def test_each_view_excludes_unmeasured_money_independently(
    views: asyncpg.Connection,
    view: str,
    cost_field: str,
    savings_field: str,
) -> None:
    await population(views)
    result = await row(views, view)
    # This runs independently of the token test so a bad Overview token sum
    # cannot hide the Evidence money defect in the RED receipt.
    assert result[cost_field] == 3
    assert result[savings_field] == 6


async def test_tied_estimates_choose_highest_primary_key_every_transaction(
    views: asyncpg.Connection,
) -> None:
    # Different model names allow both real rows under the existing unique
    # index. Both timestamps tie: an ORDER BY partition key cannot choose one.
    await estimate(views, "tied", 900, 1, row_id=10, model="low-id")
    await estimate(views, "tied", 800, 3, row_id=20, model="high-id")
    await event(views, "tied", "metered", 50, 1)
    values = set()
    for _ in range(20):
        async with views.transaction():
            overview = await row(views, OVERVIEW)
            evidence = await row(views, EVIDENCE)
            values.add(
                (overview["total_savings_usd"], evidence["cumulative_savings_usd"])
            )
    assert values == {(6, 6)}, "a repeatable but wrong low-id choice also fails"
    assert overview["tokens_total"] == 50


async def test_earlier_created_but_later_updated_row_wins_in_both_views(
    views: asyncpg.Connection,
) -> None:
    await estimate(
        views,
        "updated-later",
        300,
        3,
        row_id=10,
        model="earlier-created",
        created=WHEN - timedelta(hours=2),
        updated=WHEN + timedelta(hours=1),
    )
    await estimate(
        views,
        "updated-later",
        100,
        1,
        row_id=20,
        model="later-created",
        created=WHEN - timedelta(hours=1),
        updated=WHEN,
    )
    # Updated state outranks both later creation and a higher primary key.
    overview = await row(views, OVERVIEW)
    evidence = await row(views, EVIDENCE)
    assert overview["total_savings_usd"] == 6
    assert evidence["cumulative_savings_usd"] == 6
    assert overview["tokens_total"] == 300
    sessions = evidence["sessions"]
    if isinstance(sessions, str):
        sessions = json.loads(sessions)
    assert [(s["model_name"], s["prompt_tokens"]) for s in sessions] == [
        ("earlier-created", 300),
    ]


async def test_new_columns_append_and_invoker_rights_survive(
    views: asyncpg.Connection,
) -> None:
    expected = {
        OVERVIEW: [
            "window",
            "total_cost_usd",
            "total_baseline_cost_usd",
            "total_savings_usd",
            "savings_rate",
            "tokens_total",
            "tokens_to_compliance",
            "local_token_pct",
            "captured_at",
            "rows",
            "recent_runs",
            "measured_run_count",
            "zero_token_run_count",
            "warnings",
            "provisioned",
            "latest_projection_updated_at",
            "tenant_id",
            "estimated_run_count",
            "unknown_run_count",
            "run_count",
            "tokens_input_total",
            "tokens_output_total",
        ],
        EVIDENCE: [
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
            "tenant_id",
            "unpriced_call_count",
            "measured_run_count",
            "estimated_run_count",
            "unknown_run_count",
        ],
    }
    for view, names in expected.items():
        columns = await views.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name=$1 "
            "ORDER BY ordinal_position",
            view,
        )
        assert [column["column_name"] for column in columns] == names
        options = await views.fetchval(
            "SELECT reloptions FROM pg_class WHERE oid=$1::regclass",
            view,
        )
        assert "security_invoker=true" in options
        assert await views.fetchval(
            "SELECT has_table_privilege('app_dashboard', $1, 'SELECT')",
            view,
        )
