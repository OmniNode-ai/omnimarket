# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24874 on Postgres: a tenant overlay run priced by its terminal is stored with that cost.

A run on a customer's own key carries the tier name ``tenant_overlay``, which the routing registry declares no cost model
for. The terminal prices it from the catalogue's declared model price; the projection must store that cost (booked
``metered``) rather than the zero its own re-measurement yields, and a tenant overlay run its terminal left unpriced still
stores NULL. The database is the INTEGRATION_POSTGRES_* one CI configures; the module skips without one.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta
from tests.test_omn15909_real_postgres_projection_write_path_gate import (
    _provisioned_runner,
    _real_delegation_completed_payload,
)

_SELECT = (
    "SELECT cost_usd, cost_measurement_source, cost_tier_name FROM delegation_events "
    "WHERE correlation_id = $1"
)
_TENANT = "beta-business-proof"
_TIER = "tenant_overlay"


async def _complete(
    runner: DelegationProjectionRunner,
    *,
    cost: float,
    model: str | None = None,
    endpoint_url: str | None = None,
) -> str:
    cid = str(uuid4())
    payload = _real_delegation_completed_payload(correlation_id=cid, tenant_id=_TENANT)
    payload["cumulative_attempt_cost"] = cost
    payload["final_attempt_cost"] = cost
    payload["cost_tier_name"] = _TIER
    if model is not None:
        payload["model_used"] = model
    if endpoint_url is not None:
        payload["endpoint_url"] = endpoint_url
    assert await runner.project_event(
        runner._topic_delegation_completed,
        payload,
        MessageMeta(partition=0, offset=0, fallback_id=cid),
    )
    return cid


@pytest.mark.integration
async def test_a_priced_tenant_overlay_run_is_stored_with_its_cost() -> None:
    async with _provisioned_runner() as (runner, admin_conn, _schema):
        cid = await _complete(runner, cost=0.000145)
        row = await admin_conn.fetchrow(_SELECT, cid)
        assert row is not None
        assert row["cost_tier_name"] == _TIER
        assert row["cost_usd"] is not None
        assert float(row["cost_usd"]) == pytest.approx(0.000145)
        assert row["cost_measurement_source"] == "metered"


@pytest.mark.integration
async def test_an_unpriced_tenant_overlay_run_is_still_stored_as_null() -> None:
    async with _provisioned_runner() as (runner, admin_conn, _schema):
        cid = await _complete(runner, cost=0.0)
        row = await admin_conn.fetchrow(_SELECT, cid)
        assert row is not None
        assert row["cost_measurement_source"] == "no_cost_model"
        assert row["cost_usd"] is None


@pytest.mark.integration
async def test_a_zero_cost_terminal_on_a_byok_endpoint_is_priced_and_stored_metered() -> (
    None
):
    # omnimarket#6331's case, on Postgres: the terminal arrives with cost 0 and names the catalogue BYOK endpoint (a real
    # terminal carries its endpoint_url, never a backend_id).
    async with _provisioned_runner() as (runner, admin_conn, _schema):
        cid = await _complete(
            runner,
            cost=0.0,
            model="glm-5.3-flash",
            endpoint_url="https://api.z.ai/api/paas/v4/chat/completions",
        )
        row = await admin_conn.fetchrow(_SELECT, cid)
        assert row is not None
        assert row["cost_measurement_source"] == "metered"
        assert row["cost_usd"] is not None
        assert float(row["cost_usd"]) > 0.0
