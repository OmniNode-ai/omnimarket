# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GET /v1/tenants/me/savings behaviour against the app the runtime mount builds.

Ported from the assertions of the onex-api route's tests: the v1 shape, the data
states, the 503 failure taxonomy, the uuid/text dual binding and the SQL's
tenant predicates. The app is the contract's own mount factory.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.handlers.container.container_tenant_read_dependencies import (
    InMemoryTenantProjectionReader,
)
from omnimarket.handlers.tenant_projection_read.projection_reader import (
    SAVINGS_SQL,
    AnalyticsTenantProjectionReader,
)
from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity
from omnimarket.models.model_tenant_delegation_projection import (
    ModelTenantSavingsAggregate,
)
from omnimarket.protocols.protocol_tenant_delegation_projection_reader import (
    ProjectionMissingError,
    ProjectionNotConfiguredError,
    ProjectionUnavailableError,
)
from tests.nodes.tenant_read_support import (
    KEY_A,
    SLUG_A,
    TENANT_A,
    TypedAnalyticsStub,
    client_for,
    flat,
    mounted_factory,
    seeded_identity,
    tenant_predicate_bindings,
)

pytestmark = pytest.mark.unit
NODE = "node_tenant_savings_read_effect"
PATH = "/v1/tenants/me/savings"
HEADERS = {"x-api-key": KEY_A}


def _app(reader: Any) -> Any:
    return mounted_factory(NODE)(identity=seeded_identity(), reader=reader)


async def _get(reader: Any, headers: dict[str, str] | None = None) -> Any:
    async with client_for(_app(reader)) as client:
        return await client.get(PATH, headers=HEADERS if headers is None else headers)


async def test_requires_auth() -> None:
    reader = InMemoryTenantProjectionReader()
    response = await _get(reader, headers={})
    assert response.status_code == 401
    assert response.json() == {"detail": "tenant_not_provisioned"}
    assert reader.calls == []


async def test_returns_real_aggregate() -> None:
    reader = InMemoryTenantProjectionReader(
        savings={
            TENANT_A: ModelTenantSavingsAggregate(
                total_saved_usd=12.5,
                total_spend_usd=3.25,
                delegation_runs=3,
                savings_latest=datetime(2026, 7, 6, 12, 0, tzinfo=UTC),
                runs_latest=datetime(2026, 7, 6, 11, 0, tzinfo=UTC),
            )
        }
    )
    response = await _get(reader)
    assert response.status_code == 200
    body = response.json()
    assert body["total_saved_usd"] == 12.5
    assert body["total_spend_usd"] == 3.25
    assert body["net_saved_usd"] == 12.5
    assert body["delegation_runs"] == 3
    assert body["currency"] == "USD"
    assert body["data_state"] == "ok"
    assert body["as_of"] == "2026-07-06T12:00:00Z"
    assert set(body) == {
        "total_saved_usd",
        "total_spend_usd",
        "net_saved_usd",
        "delegation_runs",
        "currency",
        "as_of",
        "data_state",
    }
    assert reader.calls == [("read_savings", TENANT_A)]


async def test_as_of_falls_back_to_the_latest_delegation_timestamp() -> None:
    reader = InMemoryTenantProjectionReader(
        savings={
            TENANT_A: ModelTenantSavingsAggregate(
                delegation_runs=1,
                runs_latest=datetime(2026, 7, 6, 11, 0, tzinfo=UTC),
            )
        }
    )
    assert (await _get(reader)).json()["as_of"] == "2026-07-06T11:00:00Z"


async def test_empty_is_insufficient_data_with_a_real_timestamp() -> None:
    response = await _get(InMemoryTenantProjectionReader())
    assert response.status_code == 200
    body = response.json()
    assert body["total_saved_usd"] == 0.0
    assert body["delegation_runs"] == 0
    assert body["data_state"] == "insufficient_data"
    assert body["as_of"]


@pytest.mark.parametrize(
    ("error", "status", "state"),
    [
        (ProjectionMissingError("no such table"), 200, "insufficient_data"),
        (ProjectionNotConfiguredError("unset"), 503, None),
        (ProjectionUnavailableError("down"), 503, None),
    ],
)
async def test_failure_taxonomy(
    error: Exception, status: int, state: str | None
) -> None:
    response = await _get(InMemoryTenantProjectionReader(fail_with=error))
    assert response.status_code == status
    if state:
        assert response.json()["data_state"] == state
    else:
        assert set(response.json()) == {"detail"}


async def test_no_reader_bound_is_503_not_configured() -> None:
    async with client_for(
        mounted_factory(NODE)(identity=seeded_identity(), reader=None)
    ) as client:
        response = await client.get(PATH, headers=HEADERS)
    assert response.status_code == 503
    assert response.json() == {"detail": "delegation analytics source not configured"}


def test_a_slug_can_never_become_the_tenant_identity() -> None:
    """A credential carrying a slug where the UUID belongs builds no identity."""
    with pytest.raises(ValidationError):
        ModelHttpTenantIdentity.model_validate(
            {"tenant_id": SLUG_A, "tenant_slug": SLUG_A, "principal_id": "p"}
        )


# --- the reader's SQL and parameter binding ------------------------------------------


async def test_reader_binds_the_tenant_uuid_under_both_names_and_the_rls_setting() -> (
    None
):
    stub = TypedAnalyticsStub(rows=[])
    await AnalyticsTenantProjectionReader(stub).read_savings(TENANT_A)
    sql, params, rls = stub.calls[0]
    assert params["tenant_uuid"] == TENANT_A
    assert isinstance(params["tenant_uuid"], uuid.UUID)
    assert params["tenant_text"] == str(TENANT_A)
    assert rls == str(TENANT_A)
    assert SLUG_A not in params.values()
    assert sql == SAVINGS_SQL


def test_savings_sql_predicates_use_each_columns_own_parameter() -> None:
    sql = flat(SAVINGS_SQL)
    assert "FROM delegation_events WHERE tenant_id = %(tenant_uuid)s" in sql
    assert "FROM savings_estimates WHERE tenant_id = %(tenant_text)s" in sql
    assert "::text" not in sql
    assert "::uuid" not in sql
    assert {"delegation_events", "savings_estimates"} == {
        table for table, _ in tenant_predicate_bindings(SAVINGS_SQL)
    }


async def test_typed_stub_positive_control_rejects_the_single_str_binding() -> None:
    stub = TypedAnalyticsStub()
    pre_fix = "SELECT 1 FROM delegation_events d WHERE d.tenant_id = %(tenant)s"
    with pytest.raises(ProjectionUnavailableError, match="uuid = text"):
        await stub.run_query(pre_fix, {"tenant": str(TENANT_A)})
    assert await stub.run_query(pre_fix, {"tenant": TENANT_A}) == []
    text_column = "SELECT 1 FROM savings_estimates WHERE tenant_id = %(t)s"
    with pytest.raises(ProjectionUnavailableError, match="text = uuid"):
        await stub.run_query(text_column, {"t": TENANT_A})
    assert await stub.run_query(text_column, {"t": str(TENANT_A)}) == []


async def test_reader_type_checks_against_both_projections() -> None:
    stub = TypedAnalyticsStub(rows=[])
    await AnalyticsTenantProjectionReader(stub).read_savings(TENANT_A)
    assert stub.seen_tables == {"delegation_events", "savings_estimates"}
