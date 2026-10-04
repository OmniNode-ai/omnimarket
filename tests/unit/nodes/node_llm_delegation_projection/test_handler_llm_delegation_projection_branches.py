# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Boundary and replay cases for the daily delegation projection."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from omnimarket.enums.enum_cost_basis import EnumCostBasis
from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.events.topics import DELEGATION_CALL_COMPLETED_TOPIC_V1
from omnimarket.models.delegation.llm_cost_routing.model_llm_delegation_completed_event import (
    ModelLlmDelegationCompletedEvent,
)
from omnimarket.nodes.node_llm_delegation_projection.handlers.handler_llm_delegation_projection import (
    TABLE,
    HandlerLlmDelegationProjection,
    _avg,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

pytestmark = pytest.mark.unit


@pytest.fixture
def db() -> InmemoryDatabaseAdapter:
    return InmemoryDatabaseAdapter()


@pytest.fixture
def handler() -> HandlerLlmDelegationProjection:
    return HandlerLlmDelegationProjection()


@pytest.fixture
def payload() -> dict[str, object]:
    """Complete wire payload; runtime metadata is supplied by each test."""
    event = ModelLlmDelegationCompletedEvent(
        correlation_id="corr-boundary",
        causation_id="cause-boundary",
        request_id="req-boundary",
        task_type="document",
        task_id=None,
        selected_model="test-model",
        model_id="test-model",
        model_tier="local",
        provider="local",
        endpoint_ref="LLM_CODER_URL",
        tokens_in=100,
        tokens_out=50,
        latency_ms=200,
        actual_cost_usd=Decimal("0.001"),
        opus_equivalent_cost_usd=Decimal("0.010"),
        savings_usd=Decimal("0.009"),
        usage_source=EnumUsageSource.MEASURED,
        cost_basis=EnumCostBasis.ZERO_MARGINAL_API_COST,
        pricing_manifest_version="1.0.0",
        pricing_manifest_hash="sha256:pricing",
        output_hash="sha256:output",
        prompt_hash="sha256:prompt",
        routing_policy_hash="sha256:policy",
        policy_hash="sha256:policy",
        registry_hash="sha256:registry",
        success=True,
        quality_score=0.8,
        escalated_to=None,
        escalation_reason=None,
        redacted_summary=None,
        created_at=datetime(2026, 5, 23, 12, tzinfo=UTC),
    )
    return event.model_dump(mode="json")


@pytest.mark.parametrize(
    "missing_field",
    [
        "actual_cost_usd",
        "opus_equivalent_cost_usd",
        "savings_usd",
        "tokens_in",
        "tokens_out",
        "selected_model",
        "model_id",
        "model_tier",
    ],
)
def test_missing_accounting_or_model_field_rejected_without_write(
    handler: HandlerLlmDelegationProjection,
    db: InmemoryDatabaseAdapter,
    payload: dict[str, object],
    missing_field: str,
) -> None:
    handler.handle({**payload, "_db": db})
    before = dict(db.query(TABLE)[0])
    del payload[missing_field]

    with pytest.raises(ValidationError) as exc_info:
        handler.handle({**payload, "_db": db, "_terminal_event_id": "malformed"})

    assert [(error["loc"], error["type"]) for error in exc_info.value.errors()] == [
        ((missing_field,), "missing")
    ]
    assert db.query(TABLE) == [before]
    assert db.upsert_count == 1


@pytest.mark.parametrize("adapter", [None, object()], ids=["missing", "invalid"])
def test_handle_requires_database_adapter(
    handler: HandlerLlmDelegationProjection,
    payload: dict[str, object],
    adapter: object,
) -> None:
    if adapter is not None:
        payload["_db"] = adapter
    with pytest.raises(TypeError, match="requires a DatabaseAdapter"):
        handler.handle(payload)


def test_zero_token_completion_uses_runtime_defaults(
    handler: HandlerLlmDelegationProjection,
    db: InmemoryDatabaseAdapter,
    payload: dict[str, object],
) -> None:
    payload.update(
        tokens_in=0,
        tokens_out=0,
        latency_ms=0,
        actual_cost_usd="0",
        opus_equivalent_cost_usd="0",
        savings_usd="0",
        quality_score=None,
        _db=db,
    )
    before = dict(payload)

    result = handler.handle(payload)

    assert result == {
        "rows_upserted": 1,
        "idempotency_key": "corr-boundary:cause-boundary:req-boundary",
        "projection_cursor": f"{DELEGATION_CALL_COMPLETED_TOPIC_V1}:0:0",
        "skipped_duplicate": False,
    }
    assert payload == before
    row = db.query(TABLE)[0]
    assert row["total_calls"] == row["successful_calls"] == 1
    assert row["total_tokens_in"] == row["total_tokens_out"] == 0
    assert row["total_latency_ms"] == 0
    for field in (
        "avg_latency_ms",
        "total_actual_cost_usd",
        "total_opus_equivalent_usd",
        "total_savings_usd",
    ):
        assert Decimal(str(row[field])) == Decimal("0")
    assert row["avg_quality_score"] is None


@pytest.mark.parametrize("terminal_reason", ["refused", "timed_out"])
def test_unsuccessful_terminal_preserves_previous_quality(
    handler: HandlerLlmDelegationProjection,
    db: InmemoryDatabaseAdapter,
    payload: dict[str, object],
    terminal_reason: str,
) -> None:
    """The completion wire represents these outcomes as unsuccessful calls."""
    handler.handle({**payload, "_db": db})
    payload.update(
        success=False,
        quality_score=None,
        tokens_out=0,
        escalated_to="fallback-model",
        escalation_reason=terminal_reason,
        request_id=f"req-{terminal_reason}",
    )

    result = handler.handle({**payload, "_db": db, "_offset": 2})

    assert result["rows_upserted"] == 1
    row = db.query(TABLE)[0]
    assert row["total_calls"] == 2
    assert row["successful_calls"] == 1
    assert row["escalated_calls"] == 1
    assert row["total_tokens_in"] == 200
    assert row["total_tokens_out"] == 50
    assert row["total_latency_ms"] == 400
    assert Decimal(str(row["total_actual_cost_usd"])) == Decimal("0.002")
    assert row["avg_quality_score"] == 0.8
    assert row["source_event_id"] == f"req-{terminal_reason}"


def test_duplicate_command_id_does_not_rewrite_row(
    handler: HandlerLlmDelegationProjection,
    db: InmemoryDatabaseAdapter,
    payload: dict[str, object],
) -> None:
    """A command ID is passed as the runtime's terminal_event_id, not a wire field."""
    command_id = "command-replayed"
    first = handler.handle(
        {**payload, "_db": db, "_terminal_event_id": command_id, "_offset": 1}
    )
    before = dict(db.query(TABLE)[0])

    duplicate = handler.handle(
        {**payload, "_db": db, "_terminal_event_id": command_id, "_offset": 99}
    )

    assert first["rows_upserted"] == 1
    assert duplicate == {
        "rows_upserted": 0,
        "idempotency_key": first["idempotency_key"],
        "projection_cursor": f"{DELEGATION_CALL_COMPLETED_TOPIC_V1}:0:99",
        "skipped_duplicate": True,
    }
    assert db.query(TABLE) == [before]
    assert db.upsert_count == 1


@pytest.mark.parametrize(
    ("stored_calls", "stored_quality"),
    [
        ("1", "0.8"),
        (b"1", b"0.8"),
        (bytearray(b"1"), bytearray(b"0.8")),
        (Decimal("1"), Decimal("0.8")),
        (1.0, 0.8),
        (1, 1),
    ],
    ids=["str", "bytes", "bytearray", "decimal", "float", "int"],
)
def test_existing_numeric_storage_types_accumulate(
    handler: HandlerLlmDelegationProjection,
    db: InmemoryDatabaseAdapter,
    payload: dict[str, object],
    stored_calls: object,
    stored_quality: object,
) -> None:
    handler.handle({**payload, "_db": db})
    row = db.query(TABLE)[0]
    row["total_calls"] = stored_calls
    row["avg_quality_score"] = stored_quality

    handler.handle({**payload, "_db": db, "_terminal_event_id": "second"})

    updated = db.query(TABLE)[0]
    assert updated["total_calls"] == 2
    assert updated["total_tokens_in"] == 200
    assert updated["avg_quality_score"] == pytest.approx(
        (float(stored_quality) + 0.8) / 2
    )


def test_first_quality_after_unscored_completion(
    handler: HandlerLlmDelegationProjection,
    db: InmemoryDatabaseAdapter,
    payload: dict[str, object],
) -> None:
    handler.handle({**payload, "quality_score": None, "_db": db})

    handler.handle({**payload, "_db": db, "_terminal_event_id": "scored"})

    row = db.query(TABLE)[0]
    assert row["total_calls"] == 2
    assert row["avg_quality_score"] == 0.8


@pytest.mark.parametrize(
    ("field", "error"),
    [
        ("total_calls", "total_calls must be int-compatible"),
        ("avg_quality_score", "avg_quality_score must be float-compatible"),
    ],
)
def test_invalid_stored_number_rejected_without_write(
    handler: HandlerLlmDelegationProjection,
    db: InmemoryDatabaseAdapter,
    payload: dict[str, object],
    field: str,
    error: str,
) -> None:
    handler.handle({**payload, "_db": db})
    row = db.query(TABLE)[0]
    row[field] = object()
    before = dict(row)

    with pytest.raises(TypeError, match=error):
        handler.handle({**payload, "_db": db, "_terminal_event_id": "invalid-row"})

    assert db.query(TABLE) == [before]
    assert db.upsert_count == 1


def test_failed_upsert_reports_no_materialized_rows(
    handler: HandlerLlmDelegationProjection,
    payload: dict[str, object],
) -> None:
    class RefusingDatabase(InmemoryDatabaseAdapter):
        def upsert(self, table: str, conflict_key: str, row: dict[str, object]) -> bool:
            return False

    db = RefusingDatabase()

    result = handler.handle({**payload, "_db": db})

    assert result["rows_upserted"] == 0
    assert result["skipped_duplicate"] is False
    assert db.query(TABLE) == []
    assert db.upsert_count == 0


def test_empty_average_is_decimal_zero() -> None:
    assert _avg(Decimal("0"), 0) == Decimal("0")
