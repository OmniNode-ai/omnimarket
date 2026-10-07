# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real migration proof: stored provenance, not token presence, owns totals.

Failure modes: token-bearing estimates inflate measured totals; a measured zero
token run disappears; missing provenance is silently measured; model subtotals
disagree with headlines; tenants blend; excluded runs disappear from recent
rows; replacement changes columns/privileges; replay changes the result.
Preservation failure discovered at the Infra vendor boundary: replacing the
overview must not reset OMN-20320's measured local-token share to a literal zero,
and estimated tiered tokens must not move that measured share. Implementation
was paused to establish this integration regression before correcting it.
The caller must supply a disposable integration database. No database is started
by this suite, and a skipped connection is not evidence.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import asyncpg
import pytest
import yaml

from tests.test_omn17426_savings_aggregate_views_per_tenant import (
    _CONCURRENT_INDEX,
    _ENSURE_ROLES,
    _MIGRATION_NODES,
    _NODES,
    _PUBLIC_SCHEMA,
)

pytestmark = pytest.mark.integration
TENANT_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
TENANT_B = uuid.UUID("22222222-2222-4222-8222-222222222222")
WHEN = datetime(2026, 10, 3, 12, tzinfo=UTC)
VIEW = "projection_cost_savings_overview"
FORWARD = (
    _NODES
    / "node_projection_savings/migrations"
    / "094_savings_overview_measured_provenance.sql"
)
ORIGINAL_COLUMNS = [
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
]


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def _insert(
    conn: asyncpg.Connection,
    run: str,
    basis: str | None,
    *,
    tenant: uuid.UUID = TENANT_A,
    tokens: int = 100,
    cost: int = 1,
) -> None:
    await conn.execute(
        """INSERT INTO savings_estimates (
            event_timestamp, session_id, model_local, model_cloud_baseline,
            local_cost_usd, cloud_cost_usd, savings_usd, tenant_id,
            task_type, prompt_tokens, completion_tokens, usage_source
        ) VALUES ($1, $2, 'served-model', 'served-baseline',
                  $3, $4, $5, $6, 'code_review', $7, $7, $8)""",
        WHEN,
        run,
        Decimal(cost),
        Decimal(cost * 3),
        Decimal(cost * 2),
        str(tenant),
        tokens,
        basis,
    )


async def _row(conn: asyncpg.Connection, tenant: uuid.UUID = TENANT_A) -> Any:
    row = await conn.fetchrow(f"SELECT * FROM {VIEW} WHERE tenant_id = $1", str(tenant))
    assert row is not None, "positive control: the seeded tenant has a view row"
    return row


@pytest.fixture
async def views(postgres_fixture: asyncpg.Connection) -> Any:
    conn = postgres_fixture
    schema = f"omn20008_{uuid.uuid4().hex}"
    await conn.execute(_ENSURE_ROLES)
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    await conn.execute(f'SET search_path TO "{schema}", public')
    try:
        # Apply the same real chain as the existing integration harness in one
        # database round trip. This matters when the lab is reached over SSH;
        # executing every DDL file separately dominates the regression run.
        statements = [
            _PUBLIC_SCHEMA.sub(
                "", _CONCURRENT_INDEX.sub("CREATE INDEX", migration.read_text("utf-8"))
            )
            for node in _MIGRATION_NODES
            for migration in sorted((_NODES / node / "migrations").glob("*.sql"))
        ]
        await conn.execute("\n\n".join(statements))
        await _insert(conn, "measured", "measured")
        await _insert(conn, "other-tenant", "measured", tenant=TENANT_B, cost=7)
        yield conn
    finally:
        await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.mark.parametrize("basis", ["estimated", "unknown", None])
async def test_unmeasured_additions_do_not_move_any_measured_monetary_total(
    views: asyncpg.Connection,
    basis: str | None,
) -> None:
    before = await _row(views)
    await _insert(views, "excluded", basis, tokens=300, cost=9)
    after = await _row(views)
    for field in ["total_cost_usd", "total_baseline_cost_usd", "total_savings_usd"]:
        assert after[field] == before[field], (basis, field)
    assert before["total_cost_usd"] == 1, "positive control: measured cost exists"
    assert before["total_savings_usd"] == 2
    assert after["savings_rate"] == before["savings_rate"]
    assert after["tokens_total"] == before["tokens_total"]
    assert after["measured_run_count"] == 1
    assert after["estimated_run_count"] == (1 if basis == "estimated" else 0)
    assert after["unknown_run_count"] == (0 if basis == "estimated" else 1)
    assert (await _row(views, TENANT_B))["total_cost_usd"] == 7


@pytest.mark.parametrize(
    ("basis", "tokens", "measured", "estimated", "unknown"),
    [
        ("measured", 0, 2, 0, 0),
        ("estimated", 300, 1, 1, 0),
        ("unknown", 300, 1, 0, 1),
        (None, 300, 1, 0, 1),
    ],
)
async def test_run_counts_follow_stored_provenance_not_token_presence(
    views: asyncpg.Connection,
    basis: str | None,
    tokens: int,
    measured: int,
    estimated: int,
    unknown: int,
) -> None:
    await _insert(views, "count-case", basis, tokens=tokens)
    row = await _row(views)
    assert row["measured_run_count"] == measured
    assert row["estimated_run_count"] == estimated
    assert row["unknown_run_count"] == unknown
    assert row["zero_token_run_count"] == (1 if tokens == 0 else 0)


async def test_model_monetary_totals_exclude_estimated_and_unknown_runs(
    views: asyncpg.Connection,
) -> None:
    await _insert(views, "estimate", "estimated", cost=9)
    await _insert(views, "unknown", None, cost=19)
    row = await _row(views)
    models = _json(row["rows"])
    assert len(models) == 1
    assert models[0]["task_count"] == 3, "excluded runs still exist"
    assert models[0]["cost_usd"] == 1
    assert models[0]["baseline_cost_usd"] == 3
    assert models[0]["savings_usd"] == 2
    assert models[0]["tokens_total"] == 200


async def test_no_measured_runs_are_missing_money_not_a_measured_zero(
    views: asyncpg.Connection,
) -> None:
    await views.execute(
        "DELETE FROM savings_estimates WHERE tenant_id = $1", str(TENANT_A)
    )
    await _insert(views, "only-estimated", "estimated", cost=9)
    row = await _row(views)
    assert row["measured_run_count"] == 0
    for field in ["total_cost_usd", "total_baseline_cost_usd", "total_savings_usd"]:
        assert row[field] is None, field
    assert row["savings_rate"] is None
    assert _json(row["rows"])[0]["cost_usd"] is None
    assert (await _row(views, TENANT_B))["total_cost_usd"] == 7


async def test_an_explicitly_measured_zero_cost_stays_a_measured_zero(
    views: asyncpg.Connection,
) -> None:
    await views.execute(
        "DELETE FROM savings_estimates WHERE tenant_id = $1", str(TENANT_A)
    )
    await _insert(views, "free-measured", "measured", tokens=0, cost=0)
    row = await _row(views)
    assert row["measured_run_count"] == 1
    assert row["zero_token_run_count"] == 1
    assert row["total_cost_usd"] == 0
    assert row["total_savings_usd"] == 0


@pytest.mark.parametrize("basis", ["estimated", "unknown", None])
async def test_a_matched_estimate_cannot_borrow_the_events_measured_label(
    views: asyncpg.Connection,
    basis: str | None,
) -> None:
    await views.execute(
        """INSERT INTO delegation_events (
            correlation_id, session_id, tenant_id, task_type, delegated_to,
            model_name, quality_gate_passed, cost_usd, cost_savings_usd,
            tokens_input, tokens_output, timestamp, created_at, cost_measurement_source
        ) VALUES ('matched', 'matched', $1, 'code_review', 'local',
                  'served-model', TRUE, 2, 4, 100, 100, $2, $2, 'metered')""",
        TENANT_A,
        WHEN,
    )
    await _insert(views, "matched", basis, cost=9)
    row = await _row(views)
    assert row["total_cost_usd"] == 1
    assert row["measured_run_count"] == 1
    runs = {r["session_id"]: r for r in _json(row["recent_runs"])}
    assert runs["matched"]["cost_usd"] == 9, "the savings source owns this cost"
    assert runs["matched"]["token_provenance"] == (basis or "unknown")
    assert runs["matched"]["quality_gate_passed"] is True


async def test_recent_runs_retain_exact_provenance_and_all_excluded_rows(
    views: asyncpg.Connection,
) -> None:
    for basis in ["estimated", "unknown", None]:
        await _insert(views, f"recent-{basis}", basis)
    runs = _json((await _row(views))["recent_runs"])
    assert {r["session_id"]: r["token_provenance"] for r in runs} == {
        "measured": "measured",
        "recent-estimated": "estimated",
        "recent-unknown": "unknown",
        "recent-None": "unknown",
    }


async def test_columns_are_append_only_and_the_view_keeps_invoker_rights(
    views: asyncpg.Connection,
) -> None:
    columns = [
        r["column_name"]
        for r in await views.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = $1 AND table_schema = current_schema() "
            "ORDER BY ordinal_position",
            VIEW,
        )
    ]
    assert columns == [*ORIGINAL_COLUMNS, "estimated_run_count", "unknown_run_count"]
    options = await views.fetchval(
        "SELECT c.reloptions FROM pg_class c JOIN pg_namespace n "
        "ON c.relnamespace = n.oid WHERE c.relname = $1 "
        "AND n.nspname = current_schema()",
        VIEW,
    )
    assert "security_invoker=true" in options


async def test_forward_view_replacement_is_repeatable_and_preserves_grants(
    views: asyncpg.Connection,
) -> None:
    assert FORWARD.is_file(), "append-only migration is missing"
    before = dict(await _row(views))
    await views.execute(_PUBLIC_SCHEMA.sub("", FORWARD.read_text("utf-8")))
    assert dict(await _row(views)) == before
    assert (
        await views.fetchval(
            "SELECT has_table_privilege('app_dashboard', $1, 'SELECT')", VIEW
        )
        is True
    )


async def test_empty_tenant_is_not_an_invented_zero_row(
    views: asyncpg.Connection,
) -> None:
    assert (
        await views.fetchrow(
            f"SELECT * FROM {VIEW} WHERE tenant_id = $1", str(uuid.uuid4())
        )
        is None
    )
    assert (await _row(views))["provisioned"] is True


async def test_served_contract_exports_both_excluded_counts(
    views: asyncpg.Connection,
) -> None:
    contract = yaml.safe_load(
        (_NODES / "node_projection_savings/contract.yaml").read_text("utf-8")
    )

    # Resolve from the actual contract tree, not a guessed section name.
    def exposures(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, dict):
            return ([value] if value.get("table") == VIEW else []) + [
                found for child in value.values() for found in exposures(child)
            ]
        if isinstance(value, list):
            return [found for child in value for found in exposures(child)]
        return []

    matches = exposures(contract)
    assert len(matches) == 1
    assert {"estimated_run_count", "unknown_run_count"} <= set(matches[0]["columns"])
    row = await _row(views)
    assert row["estimated_run_count"] == row["unknown_run_count"] == 0


async def test_existing_local_token_share_survives_and_excludes_estimates(
    views: asyncpg.Connection,
) -> None:
    for run, tier, tokens, basis in [
        ("local-tier", "local", 300, "metered"),
        ("cloud-tier", "claude", 100, "metered"),
    ]:
        await views.execute(
            """INSERT INTO delegation_events (
                correlation_id, session_id, tenant_id, cost_tier_name,
                tokens_input, tokens_output, cost_usd, cost_savings_usd,
                timestamp, created_at, cost_measurement_source
            ) VALUES ($1, $1, $2, $3, $4, 0, 0, 0, $5, $5, $6)""",
            run,
            TENANT_A,
            tier,
            tokens,
            WHEN,
            basis,
        )
    assert (await _row(views))["local_token_pct"] == pytest.approx(0.75)
    await views.execute(
        """INSERT INTO delegation_events (
            correlation_id, session_id, tenant_id, cost_tier_name,
            tokens_input, tokens_output, cost_usd, cost_savings_usd,
            timestamp, created_at, cost_measurement_source
        ) VALUES ('local-estimate', 'local-estimate', $1, 'local',
                  400, 0, 0, 0, $2, $2, 'manifest_compute')""",
        TENANT_A,
        WHEN,
    )
    assert (await _row(views))["local_token_pct"] == pytest.approx(0.75)
