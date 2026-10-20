# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20834 AC2 on Postgres: an unpriced generation run is stored as NULL.

0008 declared generation_events.cost_inference_usd NOT NULL DEFAULT 0, so a run
on a model the pricing surface reports as UNKNOWN could only be stored as 0.
Failure modes this module is written against:

- F1 the column refuses NULL, so the unpriced run cannot be stored at all;
- F2 the writer stores 0 for a run whose terminal names a null cost;
- F3 a priced run's stored cost changes.

The database is the INTEGRATION_POSTGRES_* one CI configures; the module skips
without one.
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
)

_SELECT = "SELECT cost_inference_usd FROM generation_events WHERE correlation_id = $1"
_COLUMN = (
    "SELECT is_nullable, column_default FROM information_schema.columns"
    " WHERE table_schema = current_schema() AND table_name = 'generation_events'"
    " AND column_name = 'cost_inference_usd'"
)


async def _generation(
    runner: DelegationProjectionRunner, *, model_id: str, cost: float | None
) -> str:
    cid = str(uuid4())
    assert await runner.project_event(
        runner._topic_generation,
        {
            "correlation_id": cid,
            "task_description": "generate a node",
            "provider": "cloud",
            "model_id": model_id,
            "endpoint_class": "cheap_cloud",
            "attempt_count": 1,
            "total_latency_e2e_ms": 10,
            "contract_passed": True,
            "cost_inference_usd": cost,
        },
        MessageMeta(partition=0, offset=0, fallback_id=cid),
    )
    return cid


@pytest.mark.integration
async def test_the_cost_column_accepts_null_and_invents_no_default() -> None:
    async with _provisioned_runner() as (_runner, admin_conn, _schema):
        column = await admin_conn.fetchrow(_COLUMN)
        assert column is not None
        assert column["is_nullable"] == "YES"
        assert column["column_default"] is None


@pytest.mark.integration
async def test_an_unpriced_run_is_stored_as_null_and_a_priced_run_unchanged() -> None:
    async with _provisioned_runner() as (runner, admin_conn, _schema):
        unpriced = await _generation(
            runner, model_id="omn20834-model-not-in-pricing", cost=None
        )
        priced = await _generation(runner, model_id="gemini-2.5-flash", cost=0.0142)
        free = await _generation(runner, model_id="Qwen3.6-35B-A3B", cost=0.0)

        unpriced_row = await admin_conn.fetchrow(_SELECT, unpriced)
        priced_row = await admin_conn.fetchrow(_SELECT, priced)
        free_row = await admin_conn.fetchrow(_SELECT, free)
        assert unpriced_row is not None
        assert priced_row is not None
        assert free_row is not None
        assert unpriced_row["cost_inference_usd"] is None
        assert float(priced_row["cost_inference_usd"]) == 0.0142
        assert free_row["cost_inference_usd"] is not None
        assert float(free_row["cost_inference_usd"]) == 0.0
