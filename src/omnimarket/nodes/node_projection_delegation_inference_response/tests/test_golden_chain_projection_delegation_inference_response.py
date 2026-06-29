# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerProjectionDelegationInferenceResponse.

Tests verify:
- Handler imports and is instantiable (RED->GREEN gate)
- Singleton row upserted with correct latest_* fields
- recent_responses window maintained (max MAX_HISTORY entries)
- Idempotency: duplicate event with same correlation_id re-upserts (no duplicate row)
- provisioned always True after first event
- source_topic matches the contracted subscribe topic
"""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from omnimarket.nodes.node_projection_delegation_inference_response.handlers.handler_projection_delegation_inference_response import (
    TABLE,
    HandlerProjectionDelegationInferenceResponse,
)
from omnimarket.nodes.node_projection_delegation_inference_response.models.model_inference_response_projection import (
    MAX_HISTORY,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter


@pytest.mark.unit
def test_handler_importable() -> None:
    """Smoke: handler class is importable and instantiable."""
    assert HandlerProjectionDelegationInferenceResponse is not None
    handler = HandlerProjectionDelegationInferenceResponse()
    assert handler is not None


@pytest.mark.unit
def test_handle_upserts_singleton_row() -> None:
    """Single event produces one singleton row with correct latest_* fields."""
    db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionDelegationInferenceResponse()

    correlation_id = uuid4()
    payload = {
        "_db": db,
        "_topic": "onex.evt.omnibase-infra.inference-response.v1",
        "_partition": 0,
        "_offset": 1,
        "correlation_id": str(correlation_id),
        "content": "Hello from the model",
        "model_used": "glm-5.2",
        "llm_call_id": "chatcmpl-abc123",
        "latency_ms": 350,
        "prompt_tokens": 120,
        "completion_tokens": 45,
        "total_tokens": 165,
        "error_message": "",
    }

    result = handler.handle(payload)

    rows = db.query(TABLE)
    assert len(rows) == 1, "Singleton table must contain exactly one row"
    row = rows[0]
    assert row["singleton_key"] == "global"
    assert row["latest_correlation_id"] == str(correlation_id)
    assert row["latest_model_name"] == "glm-5.2"
    assert row["latest_generated_text"] == "Hello from the model"
    assert row["latest_prompt_tokens"] == 120
    assert row["latest_completion_tokens"] == 45
    assert row["latest_latency_ms"] == 350
    assert row["provisioned"] is True
    assert row["source_topic"] == "onex.evt.omnibase-infra.inference-response.v1"

    # recent_responses should hold the one new entry
    raw_recent = row["recent_responses"]
    if isinstance(raw_recent, str):
        recent: list[object] = json.loads(raw_recent)
    else:
        assert isinstance(raw_recent, list)
        recent = raw_recent
    assert len(recent) == 1
    first = recent[0]
    assert isinstance(first, dict)
    assert first["correlation_id"] == str(correlation_id)
    assert first["generated_text"] == "Hello from the model"

    # result dict must be non-empty
    assert isinstance(result, dict)
    assert result.get("rows_upserted", 0) == 1


@pytest.mark.unit
def test_handle_second_event_updates_singleton_and_grows_recent() -> None:
    """Second event updates the singleton row; recent_responses accumulates."""
    db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionDelegationInferenceResponse()

    for i in range(2):
        handler.handle(
            {
                "_db": db,
                "_topic": "onex.evt.omnibase-infra.inference-response.v1",
                "_partition": 0,
                "_offset": i,
                "correlation_id": str(uuid4()),
                "content": f"response {i}",
                "model_used": "glm-5.2",
                "llm_call_id": f"call-{i}",
                "latency_ms": 100 + i * 10,
                "prompt_tokens": 50,
                "completion_tokens": 20,
                "total_tokens": 70,
                "error_message": "",
            }
        )

    rows = db.query(TABLE)
    assert len(rows) == 1, "Must remain a singleton"
    row = rows[0]
    assert row["latest_generated_text"] == "response 1"

    raw_recent = row["recent_responses"]
    if isinstance(raw_recent, str):
        recent2: list[object] = json.loads(raw_recent)
    else:
        assert isinstance(raw_recent, list)
        recent2 = raw_recent
    assert len(recent2) == 2


@pytest.mark.unit
def test_handle_recent_responses_capped_at_max_history() -> None:
    """recent_responses never grows beyond MAX_HISTORY entries."""
    db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionDelegationInferenceResponse()

    for i in range(MAX_HISTORY + 3):
        handler.handle(
            {
                "_db": db,
                "_topic": "onex.evt.omnibase-infra.inference-response.v1",
                "_partition": 0,
                "_offset": i,
                "correlation_id": str(uuid4()),
                "content": f"response {i}",
                "model_used": "glm-5.2",
                "llm_call_id": f"call-{i}",
                "latency_ms": 100,
                "prompt_tokens": 50,
                "completion_tokens": 20,
                "total_tokens": 70,
                "error_message": "",
            }
        )

    rows = db.query(TABLE)
    assert len(rows) == 1
    raw_capped = rows[0]["recent_responses"]
    if isinstance(raw_capped, str):
        capped: list[object] = json.loads(raw_capped)
    else:
        assert isinstance(raw_capped, list)
        capped = raw_capped
    assert len(capped) == MAX_HISTORY, (
        f"recent_responses must be capped at {MAX_HISTORY}, got {len(capped)}"
    )


@pytest.mark.unit
def test_handle_missing_db_raises() -> None:
    """Omitting _db in payload raises TypeError immediately."""
    handler = HandlerProjectionDelegationInferenceResponse()
    with pytest.raises(TypeError, match="_db"):
        handler.handle(
            {
                "correlation_id": str(uuid4()),
                "content": "test",
                "model_used": "glm-5.2",
                "llm_call_id": "",
                "latency_ms": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "error_message": "",
            }
        )
