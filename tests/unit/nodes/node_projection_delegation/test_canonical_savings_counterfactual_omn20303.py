# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A completion after runtime shutdown keeps the saving's auditable baseline."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelTaskDelegatedSavingsSource,
)
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

RUN = "b631c44a-c8c9-47b6-b7b2-85e2afa5c5c1"
COMPLETED = "onex.evt.omnibase-infra.delegation-completed.v1"
FAILED = "onex.evt.omnimarket.delegate-skill-failed.v1"


def _canonical(*, passed: bool = True, tokens: bool = True) -> dict[str, Any]:
    # Only the metering/outcome fields from the live run, with an explicit test
    # tenant. The outer failed terminal carried no counterfactual or attempts.
    return {
        "correlation_id": RUN,
        "tenant_id": str(PROJECTION_TENANT_UUID),
        "task_type": "summarization",
        "model_used": "Qwen3.8-27B",
        "quality_passed": passed,
        "operational_outcome": "completed" if passed else "inference_failed",
        "content_verdict": "usable" if passed else "not_applicable",
        "cumulative_input_tokens": 727 if tokens else 0,
        "cumulative_output_tokens": 607 if tokens else 0,
        "prompt_tokens": 727 if tokens else 0,
        "completion_tokens": 607 if tokens else 0,
        "cumulative_attempt_cost": 0.0,
        "cost_tier_name": "local_qwen",
        "timestamp": "2026-10-20T03:40:12+00:00",
    }


def _shutdown() -> dict[str, Any]:
    return {
        "correlation_id": RUN,
        "tenant_id": str(PROJECTION_TENANT_UUID),
        "task_type": "summarization",
        "status": "failed",
        "terminal_failure_cause": "runtime_shutdown",
        "emitted_at": "2026-10-20T03:18:05.130539+00:00",
    }


def _baseline(payload: dict[str, Any]) -> dict[str, Any] | None:
    source = ModelTaskDelegatedSavingsSource.from_canonical_payload(
        payload, counterfactual_builder=build_premium_counterfactual
    )
    cf = source.premium_counterfactual
    return cf.model_dump(mode="json") if cf is not None else None


@pytest.mark.unit
@pytest.mark.parametrize("order", ["canonical-only", "shutdown-first", "shutdown-last"])
def test_sync_canonical_saving_keeps_counterfactual(order: str) -> None:
    db = InmemoryDatabaseAdapter()
    seed_tenant_registry(db)
    delegation = HandlerProjectionDelegation()
    savings = HandlerProjectionSavings()
    events = [(COMPLETED, _canonical())]
    if order == "shutdown-first":
        events.insert(0, (FAILED, _shutdown()))
    elif order == "shutdown-last":
        events.append((FAILED, _shutdown()))
    for topic, payload in events:
        event = {**payload, "_event_type": topic, "_db": db}
        delegation.handle(event)
        savings.handle(event)
    rows = db.query("delegation_events", {"correlation_id": RUN})
    saving_rows = db.query("savings_estimates", {"session_id": RUN})
    assert len(rows) == len(saving_rows) == 1
    cf = rows[0]["premium_counterfactual"]
    assert cf == _baseline(_canonical())
    assert float(cf["counterfactual_cost_usd"]) == float(
        saving_rows[0]["cloud_cost_usd"]
    )
    if order != "canonical-only":
        assert rows[0]["terminal_failure_cause"] == "runtime_shutdown"


@pytest.mark.unit
@pytest.mark.parametrize("stored_baseline", [False, True])
@pytest.mark.parametrize("missing_correlation", [False, True])
async def test_async_canonical_keeps_counterfactual_after_shutdown(
    stored_baseline: bool,
    missing_correlation: bool,
) -> None:
    stored: dict[str, Any] = {
        "correlation_id": RUN,
        "tenant_id": str(PROJECTION_TENANT_UUID),
        "terminal_ok": False,
        "terminal_failure_cause": "runtime_shutdown",
        "timestamp": "2026-10-20T03:18:05.130539+00:00",
    }
    if stored_baseline:
        cf = build_premium_counterfactual(prompt_tokens=12, completion_tokens=34)
        assert cf is not None
        stored["premium_counterfactual"] = cf.model_dump(mode="json")
    db = MagicMock(spec=AsyncpgAdapter)
    db.execute = AsyncMock(return_value=[stored])
    mock_tenant_registry(db)
    runner = DelegationProjectionRunner()
    runner._db = db
    runner._write_delegation_row = AsyncMock(return_value=True)
    runner._materialize_budget_state_async = AsyncMock()
    payload = _canonical()
    if missing_correlation:
        payload.pop("correlation_id")
    assert await runner.project_event(
        COMPLETED,
        payload,
        MessageMeta(topic=COMPLETED, partition=0, offset=37014, fallback_id=RUN),
    )
    written = runner._write_delegation_row.call_args.args[0]
    assert written["premium_counterfactual"] == (
        stored["premium_counterfactual"] if stored_baseline else _baseline(_canonical())
    )


@pytest.mark.unit
@pytest.mark.parametrize(("passed", "tokens"), [(False, True), (True, False)])
def test_canonical_without_saving_does_not_invent_counterfactual(
    passed: bool, tokens: bool
) -> None:
    db = InmemoryDatabaseAdapter()
    seed_tenant_registry(db)
    payload = _canonical(passed=passed, tokens=tokens)
    HandlerProjectionDelegation().handle(
        {**payload, "_event_type": COMPLETED, "_db": db}
    )
    assert db.query("delegation_events")[0]["premium_counterfactual"] is None


@pytest.mark.unit
def test_sync_canonical_keeps_an_existing_pinned_counterfactual() -> None:
    db = InmemoryDatabaseAdapter()
    seed_tenant_registry(db)
    cf = build_premium_counterfactual(prompt_tokens=12, completion_tokens=34)
    assert cf is not None
    pinned = cf.model_dump(mode="json")
    db.upsert(
        "delegation_events",
        "correlation_id",
        {
            "correlation_id": RUN,
            "tenant_id": str(PROJECTION_TENANT_UUID),
            "premium_counterfactual": pinned,
        },
    )
    HandlerProjectionDelegation().handle(
        {**_canonical(), "_event_type": COMPLETED, "_db": db}
    )
    assert db.query("delegation_events")[0]["premium_counterfactual"] == pinned
