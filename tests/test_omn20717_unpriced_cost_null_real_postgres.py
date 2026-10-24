# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20717 AC2 on Postgres: an unpriced call is stored and read back as NULL.

Ruling 2026-10-14: AC2 ("an unpriced call returns null, never 0") holds on
Postgres too. Failure modes this module is written against:

- F1 the column refuses NULL (0007's NOT NULL), so an unpriced call cannot be
  stored as anything but 0;
- F2 the writer stores 0.0 for a call no tier cost model priced;
- F3 a row created by the verdict first, which names no cost, takes the 0
  DEFAULT and so reads as a priced free call;
- F4 the backfill runs under FORCE ROW LEVEL SECURITY as a non-superuser owner
  and silently matches no row;
- F5 the backfill rewrites a zero it cannot prove was unpriced (a free local
  call, or a row with no pricing source);
- F6 the aggregate the dashboards read prices an unpriced call at 0;
- F7 a tenant none of whose calls was priced reads a total of 0.0 instead of
  null (a total that defaults to 0 passes every mixed-tenant case);
- F8 the backfill's helper policy outlives the migration, or the migration
  changes the tenant policy, the table's forced row-level security or the
  dashboard's read grant.

A priced call and a free local call keep their costs. The database is the
INTEGRATION_POSTGRES_* one CI configures; the module skips without one.
"""

from __future__ import annotations

import contextlib
import json
from uuid import uuid4

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _APP_DASHBOARD_ROLE_SQL,
    _MIGRATIONS_DIR,
    _connect_or_skip,
    _live_migration_files,
    _provisioned_runner,
    _real_delegation_completed_payload,
    _real_quality_gate_result_payload,
    _test_schema_safe_sql,
)

_MIGRATION = _MIGRATIONS_DIR / "0061_delegation_events_cost_usd_nullable.sql"
_SELECT = (
    "SELECT cost_usd, cost_measurement_source FROM delegation_events "
    "WHERE correlation_id = $1"
)
_POLICIES = (
    "SELECT policyname, permissive, roles::text[] AS roles, cmd, qual, with_check"
    " FROM pg_policies WHERE schemaname = current_schema()"
    " AND tablename = 'delegation_events' ORDER BY policyname"
)
_TENANT = "beta-business-proof"
_UNREGISTERED_TIER = "omn20717-no-cost-model-tier"


async def _complete(
    runner: DelegationProjectionRunner, *, cost: float, tier: str
) -> str:
    cid = str(uuid4())
    payload = _real_delegation_completed_payload(correlation_id=cid, tenant_id=_TENANT)
    payload["cumulative_attempt_cost"] = cost
    payload["cost_tier_name"] = tier
    assert await runner.project_event(
        runner._topic_delegation_completed,
        payload,
        MessageMeta(partition=0, offset=0, fallback_id=cid),
    )
    return cid


@pytest.mark.integration
async def test_an_unpriced_call_is_stored_and_read_back_as_null() -> None:
    async with _provisioned_runner() as (runner, admin_conn, _schema):
        cid = await _complete(runner, cost=0.0, tier=_UNREGISTERED_TIER)
        row = await admin_conn.fetchrow(_SELECT, cid)
        assert row is not None
        assert row["cost_measurement_source"] == "no_cost_model"
        assert row["cost_usd"] is None


@pytest.mark.integration
async def test_a_priced_call_and_a_free_local_call_keep_their_costs() -> None:
    async with _provisioned_runner() as (runner, admin_conn, _schema):
        priced = await _complete(runner, cost=0.0142, tier="cheap_cloud")
        free = await _complete(runner, cost=0.0, tier="local")
        priced_row = await admin_conn.fetchrow(_SELECT, priced)
        free_row = await admin_conn.fetchrow(_SELECT, free)
        assert priced_row is not None
        assert free_row is not None
        assert priced_row["cost_usd"] is not None
        assert float(priced_row["cost_usd"]) > 0
        assert free_row["cost_measurement_source"] == "free_local"
        assert free_row["cost_usd"] is not None
        assert float(free_row["cost_usd"]) == 0


@pytest.mark.integration
async def test_a_verdict_on_an_unpriced_row_invents_no_cost() -> None:
    # OMN-19448 rule 1: the verdict never creates the row, so the only row it
    # touches is its terminal's; it must leave that row's NULL cost NULL.
    async with _provisioned_runner() as (runner, admin_conn, _schema):
        cid = await _complete(runner, cost=0.0, tier=_UNREGISTERED_TIER)
        assert await runner.project_event(
            runner._topic_quality_gate_result,
            _real_quality_gate_result_payload(correlation_id=cid, passed=True),
            MessageMeta(partition=0, offset=1, fallback_id=cid),
        )
        row = await admin_conn.fetchrow(_SELECT, cid)
        assert row is not None
        assert row["cost_usd"] is None


@pytest.mark.integration
async def test_the_backfill_nulls_only_unpriced_zero_rows_as_a_non_superuser_owner() -> (
    None
):
    """F4 and F5: 0061 applied by the table's non-superuser owner under FORCE RLS."""
    admin_conn = await _connect_or_skip()
    schema = f"omn20717_{uuid4().hex[:16]}"
    owner = f"omn20717_owner_{uuid4().hex[:8]}"
    try:
        await admin_conn.execute(f"CREATE SCHEMA {schema}")
        await admin_conn.execute(f"SET search_path TO {schema}, public")
        await admin_conn.execute(_APP_DASHBOARD_ROLE_SQL)
        for path in _live_migration_files():
            if path.name == _MIGRATION.name:
                continue
            await admin_conn.execute(_test_schema_safe_sql(path.read_text()))
        ids = {key: str(uuid4()) for key in ("unpriced", "free", "no_source", "priced")}
        seeded = {
            "unpriced": ("0", "no_cost_model"),
            "free": ("0", "free_local"),
            "no_source": ("0", ""),
            "priced": ("0.0142", "metered"),
        }
        for key, (cost, source) in seeded.items():
            await admin_conn.execute(
                "INSERT INTO delegation_events (correlation_id, tenant_id, timestamp,"
                " task_type, delegated_to, model_name, delegated_by,"
                " quality_gate_passed, cost_usd, cost_measurement_source)"
                " VALUES ($1, gen_random_uuid(), now(), 'research', 'm', 'm',"
                " 'omn20717-test', true, $2::numeric, $3)",
                ids[key],
                cost,
                source,
            )
        await admin_conn.execute(f"CREATE ROLE {owner} NOLOGIN NOSUPERUSER NOBYPASSRLS")
        await admin_conn.execute(f"GRANT USAGE, CREATE ON SCHEMA {schema} TO {owner}")
        await admin_conn.execute(f"ALTER TABLE delegation_events OWNER TO {owner}")
        assert await admin_conn.fetchval(
            "SELECT relforcerowsecurity FROM pg_class"
            " WHERE oid = 'delegation_events'::regclass"
        )
        policies_before = await admin_conn.fetch(_POLICIES)
        assert policies_before, "the tenant policy must exist before 0061"
        await admin_conn.execute(f"SET ROLE {owner}")
        await admin_conn.execute(_MIGRATION.read_text())
        await admin_conn.execute("RESET ROLE")
        # F8: the backfill's helper policy is dropped before the block ends, so
        # the policies afterwards are exactly the ones before, and the
        # dashboard's read grant is untouched.
        assert [dict(r) for r in await admin_conn.fetch(_POLICIES)] == [
            dict(r) for r in policies_before
        ]
        assert await admin_conn.fetchval(
            "SELECT has_table_privilege('app_dashboard', 'delegation_events', 'SELECT')"
        )
        costs = {
            key: await admin_conn.fetchval(
                "SELECT cost_usd FROM delegation_events WHERE correlation_id = $1",
                cid,
            )
            for key, cid in ids.items()
        }
        assert costs["unpriced"] is None
        assert costs["free"] is not None
        assert float(costs["free"]) == 0
        assert costs["no_source"] is not None
        assert float(costs["no_source"]) == 0
        assert costs["priced"] is not None
        assert float(costs["priced"]) > 0
        assert await admin_conn.fetchval(
            "SELECT relforcerowsecurity FROM pg_class"
            " WHERE oid = 'delegation_events'::regclass"
        ), "FORCE ROW LEVEL SECURITY must be restored"
    finally:
        with contextlib.suppress(Exception):
            await admin_conn.execute("RESET ROLE")
            await admin_conn.execute("SET search_path TO public")
            await admin_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            await admin_conn.execute(f"DROP ROLE IF EXISTS {owner}")
        await admin_conn.close()


@pytest.mark.integration
async def test_the_token_usage_view_sums_priced_calls_and_counts_unpriced_ones() -> (
    None
):
    """F6: the aggregate the dashboards read must not price an unpriced call at 0."""
    async with _provisioned_runner() as (runner, admin_conn, _schema):
        await _complete(runner, cost=0.0142, tier="cheap_cloud")
        await _complete(runner, cost=0.0, tier="local")
        cid = str(uuid4())
        payload = _real_delegation_completed_payload(
            correlation_id=cid, tenant_id=_TENANT
        )
        payload.update(
            cumulative_attempt_cost=0.0,
            cost_tier_name=_UNREGISTERED_TIER,
            model_used="omn20717-unpriced-model",
        )
        assert await runner.project_event(
            runner._topic_delegation_completed,
            payload,
            MessageMeta(partition=0, offset=0, fallback_id=cid),
        )
        view = await admin_conn.fetchrow(
            "SELECT total_estimated_cost_usd, unpriced_call_count, by_model"
            " FROM projection_delegation_token_usage"
        )
        assert view is not None
        assert view["total_estimated_cost_usd"] == pytest.approx(0.0142)
        assert view["unpriced_call_count"] == 1
        by_model = {row["model_id"]: row for row in json.loads(view["by_model"])}
        unpriced = by_model["omn20717-unpriced-model"]
        assert unpriced["estimated_cost_usd"] is None
        assert unpriced["unpriced_call_count"] == 1
        assert by_model["glm-5.2"]["unpriced_call_count"] == 0


@pytest.mark.integration
async def test_a_tenant_with_only_unpriced_calls_reads_a_null_total() -> None:
    """F7: a total over no priced call is unknown, so it is null, never 0.0.

    A priced call beside the unpriced one would hide a total that defaults to
    0 (the mixed tenant above still sums to its priced cost), so this tenant
    has none.
    """
    async with _provisioned_runner() as (_runner, admin_conn, _schema):
        tenant = uuid4()
        await admin_conn.execute(
            "INSERT INTO delegation_events (correlation_id, tenant_id, timestamp,"
            " task_type, delegated_to, model_name, delegated_by,"
            " quality_gate_passed, cost_usd, cost_measurement_source)"
            " VALUES ($1, $2, now(), 'research', 'm', 'omn20717-unpriced-only',"
            " 'omn20717-test', true, NULL, 'no_cost_model')",
            str(uuid4()),
            tenant,
        )
        view = await admin_conn.fetchrow(
            "SELECT total_estimated_cost_usd, unpriced_call_count, by_model"
            " FROM projection_delegation_token_usage WHERE tenant_id = $1",
            tenant,
        )
        assert view is not None
        assert view["total_estimated_cost_usd"] is None
        assert view["unpriced_call_count"] == 1
        (model,) = json.loads(view["by_model"])
        assert model["estimated_cost_usd"] is None


@pytest.mark.integration
@pytest.mark.parametrize(
    ("cost_measured", "expected"),
    [(False, None), (True, 0), (None, 0)],
    ids=["unpriced-null", "measured-free-zero", "unreported-taken-as-given"],
)
async def test_the_delegate_skill_terminal_stores_an_unpriced_cost_as_null(
    cost_measured: bool | None, expected: int | None
) -> None:
    """F2 on the delegate-skill terminal path, which carries no pricing source."""
    from tests.unit.projection.test_delegation_backend_id_and_host_omn20162 import (
        _attempt,
        _skill_terminal,
    )

    payload = _skill_terminal([_attempt(backend_id="b", host="h201", passed=True)])
    payload["tenant_id"] = _TENANT
    payload["metrics"]["cost_measured"] = cost_measured
    cid = str(payload["correlation_id"])
    async with _provisioned_runner() as (runner, admin_conn, _schema):
        assert await runner.project_event(
            runner._topic_delegate_skill_completed,
            payload,
            MessageMeta(partition=0, offset=0, fallback_id=cid),
        )
        cost = await admin_conn.fetchval(
            "SELECT cost_usd FROM delegation_events WHERE correlation_id = $1", cid
        )
        if expected is None:
            assert cost is None
        else:
            assert cost is not None
            assert float(cost) == expected
