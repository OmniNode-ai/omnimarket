# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21208: both savings totals include a harness run, on real Postgres.

One local terminal that reports its own saving and one harness terminal that
reports none go through the REAL ``SavingsProjectionRunner`` write path against
a migrated schema. ``cost.savings-overview.v1`` (``total_savings_usd``) and
``delegation.savings.v1`` (``cumulative_savings_usd``) must each equal the sum
of both runs' savings. Before the fix the harness run wrote no row, so both
totals equalled the local run's saving alone.

Provisioned by the OMN-20303 helper on the INTEGRATION_POSTGRES_* DSN, in a
throwaway schema; SKIPS (never ERRORs) without a reachable database.
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_UUID
from tests.test_omn20303_real_postgres_savings_one_row_per_run import (
    _provisioned_runner,
)
from tests.test_omn21208_harness_terminal_savings_row import (
    TOKENS_IN,
    TOKENS_OUT,
    TOPIC,
    _expected_saving,
    _harness_terminal,
)

LOCAL_SAVING = Decimal("0.25")
SIX_PLACES = Decimal("0.000001")


def _local_terminal() -> dict[str, Any]:
    payload = _harness_terminal(
        provider="local-heavy-reasoning",
        model_name="Qwen3.8-27B",
        delegated_to="Qwen3.8-27B",
    )
    payload["metrics"] = {
        **payload["metrics"],
        "cost_savings_usd": float(LOCAL_SAVING),
    }
    return payload


@pytest.mark.integration
async def test_real_postgres_both_totals_count_the_harness_run() -> None:
    if not os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD")
    ):
        pytest.skip("no INTEGRATION_POSTGRES_PASSWORD -- skipping OMN-21208 test")
    harness = _harness_terminal()
    local = _local_terminal()
    async with _provisioned_runner() as (runner, admin_conn):
        for offset, data in enumerate((local, harness)):
            meta = MessageMeta(
                partition=0, offset=offset, fallback_id=str(uuid4()), topic=TOPIC
            )
            assert await runner.project_event(TOPIC, dict(data), meta)

        rows = await admin_conn.fetch(
            "SELECT session_id, savings_usd, savings_method, usage_source "
            "FROM savings_estimates ORDER BY savings_usd"
        )
        overview = await admin_conn.fetch(
            "SELECT total_savings_usd FROM projection_cost_savings_overview "
            "WHERE tenant_id = $1",
            str(HOUSE_TENANT_UUID),
        )
        delegation = await admin_conn.fetch(
            "SELECT cumulative_savings_usd FROM projection_delegation_savings "
            "WHERE tenant_id = $1",
            str(HOUSE_TENANT_UUID),
        )

    harness_saving = _expected_saving(TOKENS_IN, TOKENS_OUT, Decimal("0")).quantize(
        SIX_PLACES
    )
    expected_total = LOCAL_SAVING + harness_saving
    assert {str(row["session_id"]) for row in rows} == {
        str(local["correlation_id"]),
        str(harness["correlation_id"]),
    }, "the harness run has its own savings row"
    # Counted by the measured-only totals for the same reason as the local row:
    # recorded tokens and recorded cost at the pinned baseline price.
    assert {(row["savings_method"], row["usage_source"]) for row in rows} == {
        ("measured", "measured")
    }
    assert len(overview) == 1
    assert (
        Decimal(str(overview[0]["total_savings_usd"])).quantize(SIX_PLACES)
        == expected_total
    )
    assert len(delegation) == 1
    assert (
        Decimal(str(delegation[0]["cumulative_savings_usd"])).quantize(SIX_PLACES)
        == expected_total
    )
