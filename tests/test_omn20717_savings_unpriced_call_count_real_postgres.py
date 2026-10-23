# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20717: the delegation savings aggregate counts unpriced calls.

Ruling (2026-10-14): a sum of ``delegation_events.cost_usd`` treats NULL as
unknown, not zero, and shows how many calls were unpriced next to the total.

``projection_delegation_savings`` already adds an unpriced call in as 0, which
leaves every total equal to the sum of the priced calls. What it could not do
was say so: a tenant whose calls were all unpriced read a local cost of 0.0
with nothing beside it. Migration 096 appends ``unpriced_call_count``. The
totals keep their values and stay numbers, because the pages read them as
numbers.

Each case seeds its own tenant, so one wrong count cannot hide behind another.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import asyncpg
import pytest
import yaml

from tests.test_omn17426_savings_aggregate_views_per_tenant import (
    _ENSURE_ROLES,
    _NODES,
    SAVINGS,
    _apply,
)

pytestmark = pytest.mark.integration

MIXED = uuid.UUID("11111111-1111-4111-8111-111111111111")
ALL_UNPRICED = uuid.UUID("22222222-2222-4222-8222-222222222222")
ALL_PRICED = uuid.UUID("33333333-3333-4333-8333-333333333333")
ESTIMATED = uuid.UUID("44444444-4444-4444-8444-444444444444")
WHEN = datetime(2026, 10, 15, 12, tzinfo=UTC)

#: The columns the view served before 096, in order. 096 may only append.
EXISTING_COLUMNS = [
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
]


async def _call(
    conn: asyncpg.Connection,
    run: str,
    tenant: uuid.UUID,
    cost: Decimal | None,
    source: str,
    saving: Decimal = Decimal(0),
) -> None:
    await conn.execute(
        """
        INSERT INTO delegation_events (
            correlation_id, session_id, tenant_id, task_type, delegated_to,
            model_name, quality_gate_passed, cost_usd, cost_savings_usd,
            tokens_input, tokens_output, timestamp, created_at,
            cost_measurement_source
        ) VALUES ($1, $1, $2, 'research', 'local', 'qwen', TRUE, $3::numeric,
                  $4, 10, 10, $5, $5, $6)
        """,
        run,
        tenant,
        cost,
        saving,
        WHEN,
        source,
    )


@pytest.fixture
async def views(postgres_fixture: asyncpg.Connection) -> Any:
    conn = postgres_fixture
    schema = f"omn20717s_{uuid.uuid4().hex[:12]}"
    await conn.execute(_ENSURE_ROLES)
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    await conn.execute(f'SET search_path TO "{schema}", public')
    try:
        await _apply(conn)
        await _call(
            conn, "mixed-priced", MIXED, Decimal("0.25"), "metered", Decimal("0.5")
        )
        await _call(conn, "mixed-unpriced", MIXED, None, "no_cost_model")
        await _call(conn, "unpriced-only", ALL_UNPRICED, None, "no_cost_model")
        await _call(conn, "priced-only", ALL_PRICED, Decimal("0.1"), "metered")
        await _call(conn, "free-local", ALL_PRICED, Decimal(0), "free_local")
        # An unpriced call that a savings estimate does price is not unpriced
        # on this page: the estimate's cost is the one the view serves.
        await _call(conn, "estimated", ESTIMATED, None, "no_cost_model")
        await conn.execute(
            """
            INSERT INTO savings_estimates (
                event_timestamp, session_id, model_local, model_cloud_baseline,
                local_cost_usd, cloud_cost_usd, savings_usd, tenant_id,
                task_type, prompt_tokens, completion_tokens, usage_source
            ) VALUES ($1, 'estimated', 'qwen', 'baseline', 0.03, 0.09, 0.06,
                      $2, 'research', 10, 10, 'measured')
            """,
            WHEN,
            str(ESTIMATED),
        )
        yield conn
    finally:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


async def _row(conn: asyncpg.Connection, tenant: uuid.UUID) -> Any:
    row = await conn.fetchrow(
        f"SELECT * FROM {SAVINGS} WHERE tenant_id = $1", str(tenant)
    )
    assert row is not None, f"positive control: tenant {tenant} has a row"
    return row


async def test_a_mixed_tenant_counts_the_unpriced_call_and_keeps_its_totals(
    views: asyncpg.Connection,
) -> None:
    row = await _row(views, MIXED)
    assert row["unpriced_call_count"] == 1
    assert row["session_count"] == 2
    assert row["cumulative_local_cost_usd"] == pytest.approx(0.25)
    assert row["cumulative_cloud_cost_usd"] == pytest.approx(0.75)
    assert row["cumulative_savings_usd"] == pytest.approx(0.5)


async def test_a_tenant_with_nothing_priced_says_so_beside_its_zero(
    views: asyncpg.Connection,
) -> None:
    row = await _row(views, ALL_UNPRICED)
    assert row["unpriced_call_count"] == 1
    # Still a number: the pages read it as one. The count is what says the
    # zero is not a measured zero.
    assert row["cumulative_local_cost_usd"] == 0.0


async def test_priced_calls_and_a_measured_free_call_count_none(
    views: asyncpg.Connection,
) -> None:
    row = await _row(views, ALL_PRICED)
    assert row["unpriced_call_count"] == 0
    assert row["cumulative_local_cost_usd"] == pytest.approx(0.1)


async def test_a_call_a_savings_estimate_prices_is_not_counted(
    views: asyncpg.Connection,
) -> None:
    row = await _row(views, ESTIMATED)
    assert row["cumulative_local_cost_usd"] == pytest.approx(0.03)
    assert row["unpriced_call_count"] == 0


async def test_the_count_is_appended_and_the_view_still_reads_as_its_invoker(
    views: asyncpg.Connection,
) -> None:
    schema = await views.fetchval("SELECT current_schema()")
    columns = [
        r["column_name"]
        for r in await views.fetch(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = $1 AND table_name = $2 ORDER BY ordinal_position",
            schema,
            SAVINGS,
        )
    ]
    assert columns == [
        *EXISTING_COLUMNS,
        "unpriced_call_count",
        "measured_run_count",
        "estimated_run_count",
        "unknown_run_count",
    ]
    options = await views.fetchval(
        "SELECT c.reloptions FROM pg_class c JOIN pg_namespace n"
        " ON n.oid = c.relnamespace WHERE n.nspname = $1 AND c.relname = $2",
        schema,
        SAVINGS,
    )
    assert "security_invoker=true" in (options or [])


def test_the_savings_exposure_serves_the_count() -> None:
    contract = yaml.safe_load(
        (_NODES / "node_projection_savings/contract.yaml").read_text("utf-8")
    )

    def exposures(value: Any) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        if isinstance(value, dict):
            if value.get("table") == SAVINGS and "columns" in value:
                found.append(value)
            for item in value.values():
                found.extend(exposures(item))
        elif isinstance(value, list):
            for item in value:
                found.extend(exposures(item))
        return found

    served = exposures(contract)
    assert served, "positive control: the savings exposure is declared"
    for exposure in served:
        assert "unpriced_call_count" in exposure["columns"]
