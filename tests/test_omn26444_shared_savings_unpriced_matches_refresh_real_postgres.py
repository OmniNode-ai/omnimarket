# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26444: AC1 — on a real Postgres, for each tenant, the shared savings view
public.projection_delegation_savings column unpriced_call_count must equal
runs_unknown_spend from the shared metering refresh over the same
delegation_events rows."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from omnimarket.models.model_metering_summary_refresh_requested import (
    EnumMeteringRefreshReason,
    ModelMeteringSummaryRefreshRequested,
)
from omnimarket.nodes.node_metering_summary_compute import (
    HandlerMeteringSummary,
    ModelMeteringSummaryRequest,
    ModelMeteringWindow,
)
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_metering_summary_store_refresh import (
    refresh_from_store,
)
from tests.test_omn17426_savings_aggregate_views_per_tenant import (
    _CONCURRENT_INDEX,
    _ENSURE_ROLES,
    _MIGRATION_NODES,
    _NODES,
    _PUBLIC_SCHEMA,
    SAVINGS,
)

pytestmark = pytest.mark.integration

try:
    from omnimarket.nodes.node_projection_savings.priced_models import (
        refresh_shared_priced_models,
    )
except ImportError:
    refresh_shared_priced_models = None

UTC = UTC
WHEN = datetime(2026, 10, 15, 12, tzinfo=UTC)

PRICED = "gpt-4o"
FREE = "qwen3.6-35b-a3b"
FREE_UPPER = "Qwen3.6-35B-A3B"
UNLISTED = "definitely-unlisted-model"

_REASON = next(iter(EnumMeteringRefreshReason))
#: The metering refresh reads public.metering_summary, so its node is migrated too.
_NODES_MIGRATED = (*_MIGRATION_NODES, "node_projection_metering_summary")


def _run(
    conn,
    run,
    tenant,
    *,
    model,
    cost,
    tokens_in=10,
    tokens_out=10,
    data_source="real",
    cause=None,
):
    return conn.execute(
        """
        INSERT INTO delegation_events (
            correlation_id, session_id, tenant_id, task_type, delegated_to,
            model_name, quality_gate_passed, cost_usd, cost_savings_usd,
            tokens_input, tokens_output, timestamp, created_at,
            cost_measurement_source, data_source, terminal_failure_cause
        ) VALUES ($1, $1, $2, 'research', $3, $3, TRUE, $4::numeric, 0, $5, $6, $7, $7, 'metered', $8, $9)
        """,
        str(run),
        str(tenant),
        model,
        cost,
        tokens_in,
        tokens_out,
        WHEN,
        data_source,
        cause,
    )


# The four population cases store a NULL cost, so dev's stored-NULL rule counts them and
# the refresh, which leaves them out, does not: each fails until the view filters them.
CASES: dict[str, tuple[uuid.UUID, list[dict]]] = {
    "null_cost_listed": (uuid.uuid4(), [{"model": PRICED, "cost": None}]),
    "unlisted_zero": (uuid.uuid4(), [{"model": UNLISTED, "cost": Decimal("0")}]),
    "free_positive": (uuid.uuid4(), [{"model": FREE, "cost": Decimal("0.01")}]),
    "upper_case_free_zero": (
        uuid.uuid4(),
        [{"model": FREE_UPPER, "cost": Decimal("0")}],
    ),
    "priced_positive": (uuid.uuid4(), [{"model": PRICED, "cost": Decimal("0.02")}]),
    "no_tokens": (
        uuid.uuid4(),
        [{"model": UNLISTED, "cost": None, "tokens_in": 0, "tokens_out": 0}],
    ),
    "returned_to_caller": (
        uuid.uuid4(),
        [{"model": UNLISTED, "cost": None, "cause": "returned_to_caller"}],
    ),
    "fixture": (
        uuid.uuid4(),
        [{"model": UNLISTED, "cost": None, "data_source": "fixture"}],
    ),
    "experiment": (
        uuid.uuid4(),
        [{"model": UNLISTED, "cost": None, "data_source": "experiment"}],
    ),
    # No NULL data_source case: delegation_events.data_source is NOT NULL DEFAULT 'real'
    # (node_projection_delegation 0049, 0064), so the shared lanes never hold one.
    "mixed": (
        uuid.uuid4(),
        [
            {"model": PRICED, "cost": Decimal("0.02")},
            {"model": UNLISTED, "cost": Decimal("0")},
            {"model": PRICED, "cost": None},
        ],
    ),
}
OTHER_TENANT = uuid.uuid4()
OTHER_RUNS = [
    {"model": UNLISTED, "cost": Decimal("0")},
    {"model": UNLISTED, "cost": Decimal("0")},
]


@pytest.fixture
async def store(postgres_fixture):
    conn = postgres_fixture
    schema = f"omn26444_{uuid.uuid4().hex[:12]}"
    await conn.execute(_ENSURE_ROLES)
    await conn.execute(f'CREATE SCHEMA "{schema}"')
    await conn.execute(f'SET search_path TO "{schema}", public')
    for node in _NODES_MIGRATED:
        for path in sorted(
            (_NODES / node / "migrations").glob("*.sql"), key=lambda p: p.name
        ):
            name = path.name
            sql = path.read_text()
            sql = _CONCURRENT_INDEX.sub("CREATE INDEX", sql)
            sql = _PUBLIC_SCHEMA.sub("", sql)
            try:
                await conn.execute(sql)
            except Exception as exc:
                raise AssertionError(f"migration {name} failed: {exc}") from exc
    for tenant, runs in CASES.values():
        for kwargs in runs:
            await _run(conn, uuid.uuid4(), tenant, **kwargs)
    for kwargs in OTHER_RUNS:
        await _run(conn, uuid.uuid4(), OTHER_TENANT, **kwargs)
    if refresh_shared_priced_models is not None:
        await refresh_shared_priced_models(conn)
    try:
        yield conn
    finally:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


class _Reads:
    def __init__(self, conn):
        self._conn = conn

    async def execute(self, query, *params, tenant=None):
        # The refresh names public.<table>; this test builds them in its own schema.
        return [
            dict(r)
            for r in await self._conn.fetch(_PUBLIC_SCHEMA.sub("", query), *params)
        ]


async def _refresh(tenant: uuid.UUID, conn) -> int:
    captured: dict = {}

    async def capture(plan):
        captured["plan"] = plan
        return {"rows_upserted": 0}

    req = ModelMeteringSummaryRefreshRequested(
        refresh_id=str(uuid.uuid4()),
        reason=_REASON,
        tenant_ids=(str(tenant),),
        days=None,
        requested_at=WHEN,
    )
    await refresh_from_store(
        req, reads_for=lambda _name: _Reads(conn), fold_and_store=capture
    )
    plan = captured["plan"]
    return (
        HandlerMeteringSummary()
        .handle(
            ModelMeteringSummaryRequest(
                window=ModelMeteringWindow(label="all", start=None, end=plan.as_of),
                records=plan.records,
                baseline=plan.baseline,
            )
        )
        .runs_unknown_spend
    )


async def _view_count(conn, tenant: uuid.UUID) -> int:
    row = await conn.fetchrow(
        f"SELECT unpriced_call_count FROM {SAVINGS} WHERE tenant_id = $1",
        str(tenant),
    )
    assert row is not None, f"no view row for tenant {tenant}"
    return int(row["unpriced_call_count"])


@pytest.mark.parametrize("case", list(CASES))
async def test_view_unpriced_equals_refresh_unknown_spend(case, store):
    tenant, _ = CASES[case]
    view = await _view_count(store, tenant)
    refresh = await _refresh(tenant, store)
    assert view == refresh, f"case={case} view={view} refresh={refresh}"


async def test_other_tenant_not_counted(store):
    other_view = await _view_count(store, OTHER_TENANT)
    other_refresh = await _refresh(OTHER_TENANT, store)
    assert other_view == other_refresh == 2, (
        f"OTHER view={other_view} refresh={other_refresh}"
    )
    mixed_tenant, _ = CASES["mixed"]
    mixed_view = await _view_count(store, mixed_tenant)
    mixed_refresh = await _refresh(mixed_tenant, store)
    assert mixed_view == mixed_refresh, (
        f"mixed view={mixed_view} refresh={mixed_refresh}; OTHER's runs leaked into mixed"
    )


async def test_view_has_no_per_row_anti_join(store):
    rows = await store.fetch("EXPLAIN (FORMAT TEXT) SELECT * FROM " + SAVINGS)
    plan = "\n".join(r["QUERY PLAN"] for r in rows)
    assert "Nested Loop Anti Join" not in plan, "view uses a per-row anti join"
