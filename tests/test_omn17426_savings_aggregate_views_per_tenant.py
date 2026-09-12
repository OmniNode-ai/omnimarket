# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17426: migration 089 against a real Postgres.

WHY A REAL DATABASE

Every claim this migration makes is a claim about SQL semantics: that
``CREATE OR REPLACE VIEW`` accepts the appended column (Postgres refuses a
rename, retype or reorder, so this is the only proof the column contract is
intact), that the re-grouping produces one row per tenant rather than a blend,
that the union's precedence matches onex-api's, and that a tenant with no rows
gets no row rather than a row of zeros. A mock database would accept all four
whether or not they were true.

WHY TWO TENANTS, AND WHY A POSITIVE CONTROL

Every assertion uses TWO tenants with deliberately different numbers. One
tenant cannot distinguish a correctly grouped view from the ungrouped one it
replaces -- both return a single row. And every zero-row assertion carries a
control that returns rows from the same query shape, because an aggregate that
reports nothing is otherwise indistinguishable from one that could not see its
inputs, which is the confusion this whole family of tickets exists to remove.

THE FIXTURE APPLIES BOTH NODES' MIGRATIONS

``projection_cost_savings_overview`` reads ``delegation_events`` as of 089, and
that table belongs to ``node_projection_delegation``. The chain is applied from
disk in the runner's order rather than stood in for, because a hand-written
stand-in would drift the moment the owning node changes its schema.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import asyncpg
import pytest

_NODES = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "nodes"
_MIGRATION_NODES = ("node_projection_delegation", "node_projection_savings")

_CONCURRENT_INDEX = re.compile(r"\bCREATE\s+INDEX\s+CONCURRENTLY\b", re.IGNORECASE)
_PUBLIC_SCHEMA = re.compile(r"\bpublic\.", re.IGNORECASE)

_ENSURE_ROLES = """
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_dashboard') THEN
        CREATE ROLE app_dashboard;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles WHERE rolname = 'tenant_projection_writer'
    ) THEN
        CREATE ROLE tenant_projection_writer WITH NOLOGIN NOSUPERUSER
            NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
END$$;
"""

TENANT_A = uuid.UUID("11111111-1111-4111-8111-111111111111")
TENANT_B = uuid.UUID("22222222-2222-4222-8222-222222222222")
TENANT_EMPTY = uuid.UUID("33333333-3333-4333-8333-333333333333")

#: A's run exists in BOTH sources; B's exists only in delegation_events. B is
#: the case migration 089 makes visible: a real delegation whose saving was not
#: derivable writes no savings_estimates row at all, so it was absent from the
#: page while onex-api returned it from delegation_events.
RUN_A = "aaaaaaaa-0000-4000-8000-00000000000a"
RUN_B = "bbbbbbbb-0000-4000-8000-00000000000b"

_WHEN = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)

OVERVIEW = "projection_cost_savings_overview"
SAVINGS = "projection_delegation_savings"


async def _apply(conn: asyncpg.Connection) -> None:
    for node in _MIGRATION_NODES:
        for migration in sorted(
            (_NODES / node / "migrations").glob("*.sql"), key=lambda p: p.name
        ):
            sql = _CONCURRENT_INDEX.sub("CREATE INDEX", migration.read_text("utf-8"))
            sql = _PUBLIC_SCHEMA.sub("", sql)
            try:
                await conn.execute(sql)
            except Exception as exc:
                raise AssertionError(
                    f"migration {migration.name} failed: {exc}"
                ) from exc


async def _seed(conn: asyncpg.Connection) -> None:
    """Tenant A: one delegation with a matching savings row, priced differently
    in each source so the union's precedence is observable. Tenant B: one
    delegation and no savings row at all."""
    await conn.execute(
        """
        INSERT INTO delegation_events (
            correlation_id, session_id, tenant_id, task_type, delegated_to,
            model_name, quality_gate_passed, cost_usd, cost_savings_usd,
            tokens_input, tokens_output, timestamp, created_at
        ) VALUES
          ($1, $1, $2, 'code_review', 'local', 'qwen2.5-coder', TRUE,
           0.010000, 0.990000, 100, 200, $5, $5),
          ($3, $3, $4, 'escalation', 'local', 'gemini-2.5-flash', FALSE,
           0.020000, 0.330000, 10, 20, $5, $5)
        """,
        RUN_A,
        TENANT_A,
        RUN_B,
        TENANT_B,
        _WHEN,
    )
    # Same correlation as A's delegation, deliberately a DIFFERENT saving.
    await conn.execute(
        """
        INSERT INTO savings_estimates (
            event_timestamp, session_id, model_local, model_cloud_baseline,
            local_cost_usd, cloud_cost_usd, savings_usd, tenant_id,
            task_type, prompt_tokens, completion_tokens
        ) VALUES ($1, $2, 'qwen2.5-coder', 'claude-opus-4.1',
                  $3, $4, $5, $6, 'code_review', 100, 200)
        """,
        _WHEN,
        RUN_A,
        Decimal("0.010000"),
        Decimal("0.780000"),
        Decimal("0.770000"),
        str(TENANT_A),
    )


@pytest.fixture
async def views(postgres_fixture: asyncpg.Connection) -> Any:
    conn = postgres_fixture
    schema = f"omn17426_{uuid.uuid4().hex[:12]}"
    await conn.execute(_ENSURE_ROLES)
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    await conn.execute(f'SET search_path TO "{schema}", public')
    try:
        await _apply(conn)
        await _seed(conn)
        yield conn
    finally:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


async def _row(conn: asyncpg.Connection, view: str, tenant: uuid.UUID) -> Any:
    return await conn.fetchrow(
        f"SELECT * FROM {view} WHERE tenant_id = $1", str(tenant)
    )


def _recent(row: Any) -> list[dict[str, Any]]:
    payload = row["recent_runs"]
    return list(json.loads(payload) if isinstance(payload, str) else payload)


# ---------------------------------------------------------------------------
# 1. The column contract.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("view", [SAVINGS, OVERVIEW])
async def test_tenant_id_is_appended_without_disturbing_the_column_contract(
    views: asyncpg.Connection, view: str
) -> None:
    """``CREATE OR REPLACE VIEW`` is the proof, not the claim: Postgres would
    have refused the whole migration on a rename, a retype or a reorder, so
    reaching this assertion at all means every pre-existing column survived."""
    columns = [
        record["column_name"]
        for record in await views.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = $1 AND table_schema = current_schema() "
            "ORDER BY ordinal_position",
            view,
        )
    ]
    assert columns, f"positive control: {view} exists and has columns"
    assert columns[-1] == "tenant_id", columns
    assert columns.count("tenant_id") == 1


@pytest.mark.integration
async def test_the_overview_reads_as_its_invoker(views: asyncpg.Connection) -> None:
    """Migration 088's remaining case. Without invoker rights the view reads
    with the OWNER's privileges and the owner's row-level-security policies, so
    a tenant filter on the base table is evaluated for whoever happens to own
    the view rather than for the caller."""
    options = await views.fetchval(
        "SELECT c.reloptions FROM pg_class c JOIN pg_namespace n "
        "ON n.oid = c.relnamespace "
        "WHERE c.relname = $1 AND n.nspname = current_schema()",
        OVERVIEW,
    )
    assert options is not None, "the view declares no reloptions at all"
    assert "security_invoker=true" in options, options


# ---------------------------------------------------------------------------
# 2. The grouping.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("view", [SAVINGS, OVERVIEW])
async def test_each_tenant_gets_its_own_row_and_the_numbers_do_not_blend(
    views: asyncpg.Connection, view: str
) -> None:
    all_rows = await views.fetch(f"SELECT tenant_id FROM {view}")
    assert sorted(r["tenant_id"] for r in all_rows) == sorted(
        [str(TENANT_A), str(TENANT_B)]
    )

    a = await _row(views, view, TENANT_A)
    b = await _row(views, view, TENANT_B)
    total = "total_savings_usd" if view == OVERVIEW else "cumulative_savings_usd"
    # A's saving comes from savings_estimates (0.77), B's from delegation_events
    # (0.33). Neither row carries the other's number, and neither carries 1.10.
    assert a[total] == pytest.approx(0.77)
    assert b[total] == pytest.approx(0.33)


@pytest.mark.integration
@pytest.mark.parametrize("view", [SAVINGS, OVERVIEW])
async def test_a_tenant_with_no_rows_gets_no_row_rather_than_a_row_of_zeros(
    views: asyncpg.Connection, view: str
) -> None:
    """A zero is a measurement; an absence is a refusal. Rendering the first as
    the second is how a dashboard reports a fault as a quiet period."""
    assert await _row(views, view, TENANT_EMPTY) is None
    # The control: the same query shape returns a row for a tenant that has one.
    assert await _row(views, view, TENANT_A) is not None


# ---------------------------------------------------------------------------
# 3. The overview's per-run payload -- what the arrival page renders.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_every_recent_run_carries_the_id_and_the_saving_a_reader_matches_on(
    views: asyncpg.Connection,
) -> None:
    runs = _recent(await _row(views, OVERVIEW, TENANT_A))
    assert len(runs) == 1
    run = runs[0]
    # The id onex-api returns as `run_id` and the page links as `View run <id>`.
    assert run["correlation_id"] == RUN_A
    # The figure the page renders in the Saved column, to the cent.
    assert run["cost_savings_usd"] == pytest.approx(0.77)
    assert run["model_name"] == "qwen2.5-coder"
    assert run["cost_usd"] == pytest.approx(0.01)
    assert run["total_tokens"] == 300
    # Pre-existing keys are unchanged in name and meaning: omnidash reads this
    # payload too and is not re-pointed in this change.
    assert run["session_id"] == RUN_A
    assert run["savings_usd"] == pytest.approx(0.77)
    assert "created_at" in run
    assert "token_provenance" in run


@pytest.mark.integration
async def test_a_run_that_banked_no_saving_row_is_still_on_the_page(
    views: asyncpg.Connection,
) -> None:
    """Tenant B's delegation wrote no ``savings_estimates`` row, which is what
    ``_project_delegation_terminal`` does when no counterfactual is derivable.
    Before 089 the overview read that table alone, so the run was invisible on
    the page while ``GET /v1/tenants/me/delegations`` returned it."""
    runs = _recent(await _row(views, OVERVIEW, TENANT_B))
    assert [run["correlation_id"] for run in runs] == [RUN_B]
    assert runs[0]["cost_savings_usd"] == pytest.approx(0.33)
    assert runs[0]["quality_gate_passed"] is False
    # The control: this row exists in delegation_events and NOT in savings.
    assert (
        await views.fetchval(
            "SELECT count(*) FROM savings_estimates WHERE session_id = $1", RUN_B
        )
        == 0
    )


@pytest.mark.integration
async def test_the_saving_matches_the_one_onex_api_returns_for_the_same_run(
    views: asyncpg.Connection,
) -> None:
    """onex-api reads ``COALESCE(se.savings_usd, d.cost_savings_usd)`` joining
    ``se.session_id = d.correlation_id``. The page's cross-check compares its
    rendered figure against that value, so the two must be ONE number derived
    one way -- not two independently plausible ones. Tenant A's sources
    deliberately disagree (0.77 vs 0.99), which is what makes this assertion
    able to fail."""
    api_shaped = await views.fetch(
        """
        SELECT d.correlation_id AS run_id,
               COALESCE(s.savings_usd, d.cost_savings_usd) AS saved_usd
        FROM delegation_events d
        LEFT JOIN LATERAL (
            SELECT se.savings_usd FROM savings_estimates se
            WHERE se.session_id = d.correlation_id AND se.tenant_id = $1
            ORDER BY se.event_timestamp DESC, se.updated_at DESC, se.id DESC
            LIMIT 1
        ) s ON TRUE
        WHERE d.tenant_id = $2
        """,
        str(TENANT_A),
        TENANT_A,
    )
    assert len(api_shaped) == 1, "positive control: the API query returns the run"
    expected = {r["run_id"]: float(r["saved_usd"]) for r in api_shaped}

    runs = _recent(await _row(views, OVERVIEW, TENANT_A))
    rendered = {run["correlation_id"]: float(run["cost_savings_usd"]) for run in runs}
    assert rendered == pytest.approx(expected)
    assert rendered[RUN_A] == pytest.approx(0.77), "the savings row wins, not 0.99"


@pytest.mark.integration
async def test_one_tenants_savings_row_cannot_suppress_anothers_delegation(
    views: asyncpg.Connection,
) -> None:
    """The union's dedup key is (correlation, tenant). Keyed on the correlation
    alone -- which is how the sibling view read it before 089, when neither side
    carried a tenant -- tenant A's savings row would delete tenant B's run from
    B's own page.

    The collision is one-sided and that is why it is reachable at all:
    ``delegation_events.correlation_id`` is UNIQUE, so two tenants cannot hold
    the same delegation, but ``savings_estimates`` keys on
    (session_id, event_timestamp, model_local, model_cloud_baseline) and
    happily holds tenant A's row for a session id that is tenant B's run.
    """
    await views.execute(
        """
        INSERT INTO savings_estimates (
            event_timestamp, session_id, model_local, model_cloud_baseline,
            local_cost_usd, cloud_cost_usd, savings_usd, tenant_id
        ) VALUES ($1, $2, 'qwen2.5-coder', 'claude-opus-4.1',
                  0.010000, 0.020000, 0.010000, $3)
        """,
        _WHEN,
        RUN_B,  # the correlation of tenant B's delegation
        str(TENANT_A),
    )
    b_runs = {
        run["correlation_id"]: run
        for run in _recent(await _row(views, OVERVIEW, TENANT_B))
    }
    assert set(b_runs) == {RUN_B}, "B's own run survives A's same-id savings row"
    # B's own delegation figure, never A's savings figure.
    assert b_runs[RUN_B]["cost_savings_usd"] == pytest.approx(0.33)
    # The control: A really does now hold a row under that id, so the dedup key
    # had something to collide with.
    a_runs = {
        run["correlation_id"]: run
        for run in _recent(await _row(views, OVERVIEW, TENANT_A))
    }
    assert set(a_runs) == {RUN_A, RUN_B}
    assert a_runs[RUN_B]["cost_savings_usd"] == pytest.approx(0.01)


# ---------------------------------------------------------------------------
# 4. The KPIs that stopped being hardcoded zeros.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_token_kpis_are_measured_and_the_unmeasurable_one_says_so(
    views: asyncpg.Connection,
) -> None:
    row = await _row(views, OVERVIEW, TENANT_A)
    assert row["tokens_total"] == 300, "migration 077 hardcoded this to 0"
    assert row["measured_run_count"] == 1
    assert row["zero_token_run_count"] == 0
    # Nothing measures a local/cloud token split, so this stays zero -- and now
    # says why, instead of being indistinguishable from a measured zero.
    assert row["local_token_pct"] == 0
    warnings = row["warnings"]
    warnings = json.loads(warnings) if isinstance(warnings, str) else warnings
    assert any("local_token_pct" in str(w) for w in warnings), warnings
