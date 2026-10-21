# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A harness saving must retain its baseline in both delegation writers."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.nodes.node_projection_savings.handlers.handler_projection_savings import (
    HandlerProjectionSavings,
)
from omnimarket.pricing import build_premium_counterfactual
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.runner import MessageMeta
from tests.helpers.tenant_registry import (
    PROJECTION_TENANT_UUID,
    mock_tenant_registry,
    seed_tenant_registry,
)
from tests.test_omn21208_harness_terminal_savings_row import (
    TOPIC,
    _expected_saving,
    _harness_terminal,
)


def live_harness_terminal() -> dict[str, Any]:
    # Metering shape of live run 9bfb4098-38cb-4ac5-bf64-224dd2001541:
    # savings_estimates claimed .028766; delegation_events had zero and no CF.
    payload = _harness_terminal(tenant_id=str(PROJECTION_TENANT_UUID))
    payload["metrics"].update(input_tokens=13363, output_tokens=204, total_tokens=13567)
    return payload


@pytest.mark.unit
@pytest.mark.parametrize("writer", ["sync", "async"])
@pytest.mark.parametrize("actual_cost", [Decimal("0"), Decimal("0.01")])
async def test_harness_saving_keeps_counterfactual(
    writer: str, actual_cost: Decimal
) -> None:
    payload = live_harness_terminal()
    payload["metrics"]["cost_usd"] = float(actual_cost)
    db = InmemoryDatabaseAdapter()
    seed_tenant_registry(db)
    HandlerProjectionSavings().handle({**payload, "_event_type": TOPIC, "_db": db})
    saving = db.query("savings_estimates")[0]
    if writer == "sync":
        handler = HandlerProjectionDelegation()
        # Redelivery must leave exactly one row in each projection.
        for _ in range(2):
            handler.handle({**payload, "_event_type": TOPIC, "_db": db})
        rows = db.query("delegation_events")
        assert len(rows) == 1
        row = rows[0]
    else:
        adapter = MagicMock(spec=AsyncpgAdapter)
        adapter.execute = AsyncMock(return_value=[])
        mock_tenant_registry(adapter)
        runner = DelegationProjectionRunner()
        runner._db = adapter
        runner._write_delegation_row = AsyncMock(return_value=True)
        runner._materialize_budget_state_async = AsyncMock()
        assert await runner.project_event(
            TOPIC,
            payload,
            MessageMeta(
                topic=TOPIC,
                partition=0,
                offset=1,
                fallback_id=payload["correlation_id"],
            ),
        )
        row = runner._write_delegation_row.call_args.args[0]
    cf = row["premium_counterfactual"]
    assert cf is not None, "the saving has no auditable baseline"
    expected = _expected_saving(13363, 204, actual_cost)
    assert Decimal(str(row["cost_savings_usd"])) == expected
    assert Decimal(cf["counterfactual_cost_usd"]) == Decimal(
        str(saving["cloud_cost_usd"])
    )
    assert Decimal(str(saving["savings_usd"])) == expected
    assert (cf["tokens_in"], cf["tokens_out"]) == (13363, 204)


@pytest.mark.unit
@pytest.mark.parametrize(
    "control", ["failed", "zero-tokens", "unresolved", "reported", "no-saving"]
)
def test_harness_controls_do_not_invent_counterfactual(control: str) -> None:
    payload = live_harness_terminal()
    if control == "failed":
        payload.update(status="failed", quality_gate_passed=False)
    elif control == "zero-tokens":
        payload["metrics"].update(input_tokens=0, output_tokens=0, total_tokens=0)
    elif control == "unresolved":
        payload["baseline_state"] = "BASELINE_UNRESOLVED"
    elif control == "no-saving":
        payload["metrics"]["cost_usd"] = 1
    else:
        payload["metrics"]["cost_savings_usd"] = 0
    db = InmemoryDatabaseAdapter()
    seed_tenant_registry(db)
    HandlerProjectionDelegation().handle({**payload, "_event_type": TOPIC, "_db": db})
    assert db.query("delegation_events")[0]["premium_counterfactual"] is None


@pytest.mark.unit
def test_harness_terminal_keeps_stored_baseline() -> None:
    payload = live_harness_terminal()
    cf = build_premium_counterfactual(prompt_tokens=12, completion_tokens=34)
    assert cf is not None
    pinned = cf.model_dump(mode="json")
    db = InmemoryDatabaseAdapter()
    seed_tenant_registry(db)
    db.upsert(
        "delegation_events",
        "correlation_id",
        {
            "correlation_id": payload["correlation_id"],
            "tenant_id": str(PROJECTION_TENANT_UUID),
            "premium_counterfactual": pinned,
            "cost_savings_usd": cf.counterfactual_cost_usd,
        },
    )
    HandlerProjectionDelegation().handle({**payload, "_event_type": TOPIC, "_db": db})
    row = db.query("delegation_events")[0]
    assert row["premium_counterfactual"] == pinned
    assert Decimal(str(row["cost_savings_usd"])) == cf.counterfactual_cost_usd
