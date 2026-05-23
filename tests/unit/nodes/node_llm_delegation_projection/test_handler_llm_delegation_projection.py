# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for HandlerLlmDelegationProjection.

Covers:
- Aggregate math: correct sums, counts, averages across multiple events
- Idempotency: UPSERT doesn't create duplicates on same (date, task_type, model_id, model_tier)
- Replay safety: replaying the same terminal event is a no-op (skipped_duplicate=True)
- Freshness state: FRESH default; REPLAYING propagated correctly
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from omnimarket.models.delegation.llm_cost_routing.model_llm_delegation_completed_event import (
    ModelLlmDelegationCompletedEvent,
)
from omnimarket.nodes.node_llm_delegation_projection.handlers.handler_llm_delegation_projection import (
    TABLE,
    HandlerLlmDelegationProjection,
)
from omnimarket.nodes.node_llm_delegation_projection.models.model_delegation_projection import (
    EnumFreshnessState,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter


def _make_event(
    *,
    correlation_id: str = "corr-1",
    causation_id: str = "cause-1",
    request_id: str = "req-1",
    task_type: str = "code-review",
    model_id: str = "qwen3-coder-30b",
    model_tier: str = "local",
    tokens_in: int = 1000,
    tokens_out: int = 500,
    latency_ms: int = 800,
    actual_cost_usd: Decimal = Decimal("0.001"),
    opus_equivalent_cost_usd: Decimal = Decimal("0.010"),
    savings_usd: Decimal = Decimal("0.009"),
    success: bool = True,
    quality_score: float | None = 0.9,
    escalated_to: str | None = None,
    created_at: datetime | None = None,
) -> ModelLlmDelegationCompletedEvent:
    from omnimarket.enums.enum_cost_basis import EnumCostBasis
    from omnimarket.enums.enum_usage_source import EnumUsageSource

    return ModelLlmDelegationCompletedEvent(
        correlation_id=correlation_id,
        causation_id=causation_id,
        request_id=request_id,
        task_type=task_type,
        task_id=None,
        selected_model=model_id,
        model_id=model_id,
        model_tier=model_tier,
        provider="local",
        endpoint_ref="LLM_CODER_URL",
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
        actual_cost_usd=actual_cost_usd,
        opus_equivalent_cost_usd=opus_equivalent_cost_usd,
        savings_usd=savings_usd,
        usage_source=EnumUsageSource.MEASURED,
        cost_basis=EnumCostBasis.ZERO_MARGINAL_API_COST,
        pricing_manifest_version="1.0.0",
        pricing_manifest_hash="abc123",
        output_hash="sha256:output",
        prompt_hash="sha256:prompt",
        routing_policy_hash="sha256:policy",
        policy_hash="sha256:policy",
        registry_hash="sha256:registry",
        success=success,
        quality_score=quality_score,
        escalated_to=escalated_to,
        escalation_reason=None,
        redacted_summary=None,
        created_at=created_at or datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC),
    )


@pytest.fixture
def db() -> InmemoryDatabaseAdapter:
    return InmemoryDatabaseAdapter()


@pytest.fixture
def handler() -> HandlerLlmDelegationProjection:
    return HandlerLlmDelegationProjection()


@pytest.mark.unit
def test_single_event_creates_aggregate_row(
    handler: HandlerLlmDelegationProjection,
    db: InmemoryDatabaseAdapter,
) -> None:
    event = _make_event()
    result = handler.project(
        event,
        db,
        topic="onex.evt.omnimarket.delegation-call-completed.v1",
        partition=0,
        offset=1,
        terminal_event_id="req-1",
    )

    assert result.rows_upserted == 1
    assert result.skipped_duplicate is False

    rows = db.query(TABLE)
    assert len(rows) == 1
    row = rows[0]
    assert row["total_calls"] == 1
    assert row["successful_calls"] == 1
    assert row["escalated_calls"] == 0
    assert row["total_tokens_in"] == 1000
    assert row["total_tokens_out"] == 500
    assert row["total_latency_ms"] == 800
    assert row["projection_date"] == "2026-05-23"
    assert row["model_id"] == "qwen3-coder-30b"
    assert row["model_tier"] == "local"
    assert row["freshness_state"] == "FRESH"
    assert (
        row["projection_cursor"]
        == "onex.evt.omnimarket.delegation-call-completed.v1:0:1"
    )


@pytest.mark.unit
def test_two_events_same_bucket_accumulates(
    handler: HandlerLlmDelegationProjection,
    db: InmemoryDatabaseAdapter,
) -> None:
    event1 = _make_event(
        correlation_id="corr-1", causation_id="cause-1", request_id="req-1"
    )
    event2 = _make_event(
        correlation_id="corr-2",
        causation_id="cause-2",
        request_id="req-2",
        tokens_in=500,
        tokens_out=200,
        latency_ms=400,
        actual_cost_usd=Decimal("0.002"),
        opus_equivalent_cost_usd=Decimal("0.020"),
        savings_usd=Decimal("0.018"),
        quality_score=0.7,
    )

    handler.project(
        event1,
        db,
        topic="onex.evt.omnimarket.delegation-call-completed.v1",
        partition=0,
        offset=1,
        terminal_event_id="req-1",
    )
    handler.project(
        event2,
        db,
        topic="onex.evt.omnimarket.delegation-call-completed.v1",
        partition=0,
        offset=2,
        terminal_event_id="req-2",
    )

    rows = db.query(TABLE)
    assert len(rows) == 1  # same (date, task_type, model_id, model_tier) bucket

    row = rows[0]
    assert row["total_calls"] == 2
    assert row["total_tokens_in"] == 1500
    assert row["total_tokens_out"] == 700
    assert row["total_latency_ms"] == 1200
    assert Decimal(str(row["total_actual_cost_usd"])) == Decimal("0.003")
    assert Decimal(str(row["total_savings_usd"])) == Decimal("0.027")
    # avg_latency_ms = 1200 / 2 = 600
    assert Decimal(str(row["avg_latency_ms"])) == Decimal("600")
    # avg quality = (0.9 * 1 + 0.7) / 2 = 0.8
    assert abs(float(row["avg_quality_score"]) - 0.8) < 1e-9


@pytest.mark.unit
def test_replay_same_terminal_event_is_noop(
    handler: HandlerLlmDelegationProjection,
    db: InmemoryDatabaseAdapter,
) -> None:
    event = _make_event()
    kwargs = {
        "topic": "onex.evt.omnimarket.delegation-call-completed.v1",
        "partition": 0,
        "offset": 1,
        "terminal_event_id": "req-1",
    }

    result1 = handler.project(event, db, **kwargs)
    result2 = handler.project(event, db, **kwargs)

    assert result1.rows_upserted == 1
    assert result1.skipped_duplicate is False

    assert result2.rows_upserted == 0
    assert result2.skipped_duplicate is True

    rows = db.query(TABLE)
    assert len(rows) == 1
    assert rows[0]["total_calls"] == 1  # not double-counted


@pytest.mark.unit
def test_upsert_different_idempotency_keys_same_bucket_accumulates(
    handler: HandlerLlmDelegationProjection,
    db: InmemoryDatabaseAdapter,
) -> None:
    """Two distinct events in the same bucket each increment total_calls."""
    event = _make_event(tokens_in=100)
    handler.project(
        event,
        db,
        topic="onex.evt.omnimarket.delegation-call-completed.v1",
        partition=0,
        offset=1,
        terminal_event_id="req-A",
    )
    handler.project(
        event,
        db,
        topic="onex.evt.omnimarket.delegation-call-completed.v1",
        partition=0,
        offset=2,
        terminal_event_id="req-B",
    )

    rows = db.query(TABLE)
    assert len(rows) == 1
    assert rows[0]["total_calls"] == 2
    assert rows[0]["total_tokens_in"] == 200


@pytest.mark.unit
def test_escalated_event_counted(
    handler: HandlerLlmDelegationProjection,
    db: InmemoryDatabaseAdapter,
) -> None:
    event = _make_event(escalated_to="claude-opus-4", success=True)
    handler.project(
        event,
        db,
        topic="onex.evt.omnimarket.delegation-call-completed.v1",
        partition=0,
        offset=5,
        terminal_event_id="req-esc",
    )

    row = db.query(TABLE)[0]
    assert row["escalated_calls"] == 1
    assert row["successful_calls"] == 1


@pytest.mark.unit
def test_failed_event_not_in_successful_count(
    handler: HandlerLlmDelegationProjection,
    db: InmemoryDatabaseAdapter,
) -> None:
    event = _make_event(success=False, quality_score=None)
    handler.project(
        event,
        db,
        topic="onex.evt.omnimarket.delegation-call-completed.v1",
        partition=0,
        offset=10,
        terminal_event_id="req-fail",
    )

    row = db.query(TABLE)[0]
    assert row["total_calls"] == 1
    assert row["successful_calls"] == 0


@pytest.mark.unit
def test_freshness_state_replaying_propagated(
    handler: HandlerLlmDelegationProjection,
    db: InmemoryDatabaseAdapter,
) -> None:
    event = _make_event()
    result = handler.project(
        event,
        db,
        topic="onex.evt.omnimarket.delegation-call-completed.v1",
        partition=0,
        offset=1,
        terminal_event_id="req-replay",
        freshness_state=EnumFreshnessState.REPLAYING,
    )

    assert result.rows_upserted == 1
    row = db.query(TABLE)[0]
    assert row["freshness_state"] == "REPLAYING"


@pytest.mark.unit
def test_different_model_tiers_produce_separate_rows(
    handler: HandlerLlmDelegationProjection,
    db: InmemoryDatabaseAdapter,
) -> None:
    local_event = _make_event(
        model_tier="local", correlation_id="corr-L", request_id="req-L"
    )
    cloud_event = _make_event(
        model_tier="cloud", correlation_id="corr-C", request_id="req-C"
    )

    handler.project(
        local_event,
        db,
        topic="onex.evt.omnimarket.delegation-call-completed.v1",
        partition=0,
        offset=1,
        terminal_event_id="req-L",
    )
    handler.project(
        cloud_event,
        db,
        topic="onex.evt.omnimarket.delegation-call-completed.v1",
        partition=0,
        offset=2,
        terminal_event_id="req-C",
    )

    rows = db.query(TABLE)
    assert len(rows) == 2
    tiers = {r["model_tier"] for r in rows}
    assert tiers == {"local", "cloud"}


@pytest.mark.unit
def test_handle_shim_accepts_dict_payload(
    handler: HandlerLlmDelegationProjection,
    db: InmemoryDatabaseAdapter,
) -> None:
    from omnimarket.enums.enum_cost_basis import EnumCostBasis
    from omnimarket.enums.enum_usage_source import EnumUsageSource

    payload: dict[str, object] = {
        "_db": db,
        "_topic": "onex.evt.omnimarket.delegation-call-completed.v1",
        "_partition": 0,
        "_offset": 99,
        "_terminal_event_id": "req-shim",
        "correlation_id": "corr-shim",
        "causation_id": "cause-shim",
        "request_id": "req-shim",
        "task_type": "refactor",
        "task_id": None,
        "selected_model": "deepseek-r1-14b",
        "model_id": "deepseek-r1-14b",
        "model_tier": "local",
        "provider": "local",
        "endpoint_ref": "LLM_CODER_FAST_URL",
        "tokens_in": 200,
        "tokens_out": 100,
        "latency_ms": 300,
        "actual_cost_usd": Decimal("0"),
        "opus_equivalent_cost_usd": Decimal("0.005"),
        "savings_usd": Decimal("0.005"),
        "usage_source": EnumUsageSource.MEASURED,
        "cost_basis": EnumCostBasis.ZERO_MARGINAL_API_COST,
        "pricing_manifest_version": "1.0.0",
        "pricing_manifest_hash": "abc",
        "output_hash": "sha256:o",
        "prompt_hash": "sha256:p",
        "routing_policy_hash": "sha256:r",
        "policy_hash": "sha256:r",
        "registry_hash": "sha256:reg",
        "success": True,
        "quality_score": None,
        "escalated_to": None,
        "escalation_reason": None,
        "redacted_summary": None,
        "created_at": datetime(2026, 5, 23, 10, 0, 0, tzinfo=UTC),
    }

    result = handler.handle(payload)
    assert result["rows_upserted"] == 1
    assert result["skipped_duplicate"] is False
    rows = db.query(TABLE)
    assert len(rows) == 1
    assert rows[0]["task_type"] == "refactor"


@pytest.mark.unit
def test_contract_yaml_declares_correct_topics() -> None:
    from pathlib import Path

    import yaml

    contract_path = (
        Path(__file__).resolve().parents[4]
        / "src/omnimarket/nodes/node_llm_delegation_projection/contract.yaml"
    )
    with contract_path.open(encoding="utf-8") as fh:
        contract = yaml.safe_load(fh)

    assert contract["node_type"] == "reducer"
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.delegation-projection-snapshot.v1"
    )
    assert (
        "onex.evt.omnimarket.delegation-call-completed.v1"
        in contract["event_bus"]["subscribe_topics"]
    )
    assert (
        "onex.evt.omnimarket.delegation-projection-snapshot.v1"
        in contract["event_bus"]["publish_topics"]
    )
    assert (
        "onex.evt.omnimarket.delegation-projection-snapshot.v1"
        in contract["externally_consumed_topics"]
    )
    assert (
        contract["handler_routing"]["handlers"][0]["handler"]["module"]
        == contract["handler"]["module"]
    )
    assert (
        contract["handler_routing"]["handlers"][0]["handler"]["name"]
        == contract["handler"]["class"]
    )
    assert contract["metadata"]["transport_type"] == "kafka"
