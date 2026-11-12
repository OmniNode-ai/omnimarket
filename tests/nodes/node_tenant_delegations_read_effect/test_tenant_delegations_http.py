# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GET /v1/tenants/me/delegations behaviour against the app the runtime mount builds.

Ported from the assertions of the onex-api route's tests: the envelope mapping,
the bounded limit, the per-run cost and savings sources, the savings-absent
fallback and the empty envelope, and the uuid/text binding at both query sites.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from omnimarket.handlers.container.container_tenant_read_dependencies import (
    InMemoryTenantProjectionReader,
)
from omnimarket.handlers.tenant_projection_read.projection_reader import (
    RECENT_DELEGATIONS_FALLBACK_SQL,
    RECENT_DELEGATIONS_SQL,
    AnalyticsTenantProjectionReader,
)
from omnimarket.models.model_tenant_delegation_projection import ModelDelegationRunRow
from omnimarket.protocols.protocol_tenant_delegation_projection_reader import (
    ProjectionMissingError,
    ProjectionNotConfiguredError,
    ProjectionUnavailableError,
)
from tests.nodes.tenant_read_support import (
    KEY_A,
    TENANT_A,
    TypedAnalyticsStub,
    client_for,
    flat,
    mounted_factory,
    seeded_identity,
)

pytestmark = pytest.mark.unit
NODE = "node_tenant_delegations_read_effect"
PATH = "/v1/tenants/me/delegations"
HEADERS = {"x-api-key": KEY_A}


def _row(
    i: int, *, passed: bool = True, cost: float = 0.01, saved: float = 0.42
) -> Any:
    return ModelDelegationRunRow(
        run_id=f"corr-{i}",
        task_type="summarize",
        model="qwen3-35b",
        quality_gate_passed=passed,
        cost_usd=cost,
        saved_usd=saved,
        occurred_at=datetime(2026, 7, 6, 10 - i, 0, tzinfo=UTC),
    )


async def _get(
    reader: Any, url: str = PATH, headers: dict[str, str] | None = None
) -> Any:
    app = mounted_factory(NODE)(identity=seeded_identity(), reader=reader)
    async with client_for(app) as client:
        return await client.get(url, headers=HEADERS if headers is None else headers)


async def test_maps_rows_into_the_envelope_newest_first() -> None:
    rows = (
        _row(0),
        ModelDelegationRunRow(
            run_id="corr-1",
            task_type="classify",
            model="qwen3-27b",
            quality_gate_passed=False,
            occurred_at=datetime(2026, 7, 6, 9, 0, tzinfo=UTC),
        ),
    )
    reader = InMemoryTenantProjectionReader(runs={TENANT_A: rows})
    response = await _get(reader, f"{PATH}?limit=2")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"generated_at", "count", "data_state", "runs"}
    assert body["count"] == 2
    assert body["data_state"] == "ok"
    assert body["generated_at"]
    first, second = body["runs"]
    assert first == {
        "run_id": "corr-0",
        "task_type": "summarize",
        "model": "qwen3-35b",
        "status": "passed",
        "cost_usd": 0.01,
        "saved_usd": 0.42,
        "occurred_at": "2026-07-06T10:00:00Z",
    }
    # Nullable cost and savings are zero (the contract requires numbers).
    assert (second["status"], second["cost_usd"], second["saved_usd"]) == (
        "failed",
        0.0,
        0.0,
    )


async def test_default_limit_is_five_and_the_list_is_bounded() -> None:
    reader = InMemoryTenantProjectionReader(
        runs={TENANT_A: tuple(_row(i % 9) for i in range(9))}
    )
    assert (await _get(reader)).json()["count"] == 5
    assert (await _get(reader, f"{PATH}?limit=7")).json()["count"] == 7


@pytest.mark.parametrize("limit", ["9999", "0", "-1", "abc"])
async def test_limit_out_of_bounds_is_422_after_authentication(limit: str) -> None:
    reader = InMemoryTenantProjectionReader(runs={TENANT_A: (_row(0),)})
    response = await _get(reader, f"{PATH}?limit={limit}")
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["query", "limit"]
    assert reader.calls == []


async def test_an_unauthenticated_out_of_bounds_limit_is_a_401_not_a_422() -> None:
    response = await _get(
        InMemoryTenantProjectionReader(), f"{PATH}?limit=9999", headers={}
    )
    assert response.status_code == 401


async def test_missing_primary_projection_is_an_empty_envelope() -> None:
    reader = InMemoryTenantProjectionReader(fail_with=ProjectionMissingError("absent"))
    body = (await _get(reader)).json()
    assert (body["count"], body["runs"], body["data_state"]) == (
        0,
        [],
        "insufficient_data",
    )


@pytest.mark.parametrize(
    "error", [ProjectionNotConfiguredError("unset"), ProjectionUnavailableError("down")]
)
async def test_not_configured_or_unavailable_is_503(error: Exception) -> None:
    response = await _get(InMemoryTenantProjectionReader(fail_with=error))
    assert response.status_code == 503
    assert set(response.json()) == {"detail"}


# --- the reader's SQL, parameter binding and fallback ----------------------------------


def test_query_takes_cost_and_savings_from_the_canonical_per_run_sources() -> None:
    sql = flat(RECENT_DELEGATIONS_SQL)
    assert "d.delegated_to AS model" in sql
    assert "d.model_name" not in sql
    assert "d.cost_usd AS cost_usd" in sql
    assert "COALESCE(s.savings_usd, d.cost_savings_usd) AS saved_usd" in sql
    assert "LEFT JOIN LATERAL" in sql
    assert "se.session_id = d.correlation_id" in sql
    # Scoped by the resolved parameter, not a column-to-column comparison across
    # the two projections' diverging tenant_id types.
    assert "se.tenant_id = d.tenant_id" not in sql
    assert "se.tenant_id = %(tenant_text)s" in sql
    assert "d.tenant_id = %(tenant_uuid)s" in sql
    assert "ORDER BY se.event_timestamp DESC" in sql
    assert "LIMIT 1" in sql
    assert "SUM(" not in sql


async def test_reader_binds_uuid_text_and_limit_and_the_rls_setting() -> None:
    stub = TypedAnalyticsStub(
        rows=[
            {
                "run_id": "corr-live",
                "task_type": "summarization",
                "model": "gemini-2.5-flash",
                "quality_gate_passed": True,
                "cost_usd": 0.000098,
                "saved_usd": 0.000702,
                "occurred_at": datetime(2026, 7, 27, 17, 0, tzinfo=UTC),
            }
        ]
    )
    rows = await AnalyticsTenantProjectionReader(
        stub, uncounted_data_sources=("experiment",)
    ).read_recent_delegations(TENANT_A, 5)
    assert len(stub.calls) == 1
    _sql, params, rls = stub.calls[0]
    assert params == {"tenant_uuid": TENANT_A, "tenant_text": str(TENANT_A), "limit": 5}
    assert rls == str(TENANT_A)
    assert (rows[0].cost_usd, rows[0].saved_usd) == (0.000098, 0.000702)
    assert stub.seen_tables == {"delegation_events", "savings_estimates"}


class _MissingSavingsThenStub(TypedAnalyticsStub):
    """The first (savings-joined) statement finds its projection absent."""

    async def run_query(
        self, sql: str, params: Any = None, *, tenant_id: Any = None
    ) -> Any:
        if "savings_estimates" in sql:
            self.calls.append((sql, params or {}, tenant_id))
            raise ProjectionMissingError('relation "savings_estimates" does not exist')
        return await super().run_query(sql, params, tenant_id=tenant_id)


async def test_a_missing_savings_projection_falls_back_to_delegation_truth() -> None:
    stub = _MissingSavingsThenStub(
        rows=[{"run_id": "corr-live", "cost_usd": 0.000096, "saved_usd": 0.0}]
    )
    rows = await AnalyticsTenantProjectionReader(
        stub, uncounted_data_sources=("experiment",)
    ).read_recent_delegations(TENANT_A, 5)
    assert [c[0] for c in stub.calls] == [
        RECENT_DELEGATIONS_SQL,
        RECENT_DELEGATIONS_FALLBACK_SQL,
    ]
    fallback = flat(RECENT_DELEGATIONS_FALLBACK_SQL)
    assert "savings_estimates" not in fallback
    assert "d.cost_usd" in fallback
    assert "d.cost_savings_usd" in fallback
    assert rows[0].cost_usd == 0.000096
    # The fallback is a second binding site: it binds the uuid column as uuid.
    assert stub.seen_tables == {"delegation_events"}


async def test_a_missing_delegation_projection_propagates_for_the_empty_envelope() -> (
    None
):
    class AllMissing(TypedAnalyticsStub):
        async def run_query(
            self, sql: str, params: Any = None, *, tenant_id: Any = None
        ) -> Any:
            raise ProjectionMissingError("absent")

    with pytest.raises(ProjectionMissingError):
        await AnalyticsTenantProjectionReader(
            AllMissing(), uncounted_data_sources=("experiment",)
        ).read_recent_delegations(TENANT_A, 5)
