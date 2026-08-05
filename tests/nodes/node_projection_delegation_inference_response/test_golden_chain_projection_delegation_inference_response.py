# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerProjectionDelegationInferenceResponse.

Tests verify:
- Handler imports and is instantiable (RED->GREEN gate)
- Per-tenant row upserted with correct latest_* fields (OMN-14894 tranche 2:
  the table was a single global singleton until this tranche; every event
  now keys on tenant_id, defaulting to DEFAULT_TENANT when absent)
- recent_responses holds only the current event, window size 1 (OMN-15707:
  the prior read-then-prepend-then-cap rolling window required a
  db.query() against a table the contract declares access='write' only,
  which DLQ'd every live event -- see the handler module docstring)
- Idempotency: duplicate event with same correlation_id re-upserts (no duplicate row)
- provisioned always True after first event
- source_topic matches the contracted subscribe topic
- Two tenants' events land on two distinct rows, not one collapsed row
  (the confirmed leak this tranche closes -- Linear OMN-14894 comment 6b84daf0)
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
    assert len(rows) == 1, "One tenant's event must produce exactly one row"
    row = rows[0]
    assert row["singleton_key"] == "omninode", (
        "no tenant_id on the payload falls back to DEFAULT_TENANT, never 'global'"
    )
    assert row["tenant_id"] == "omninode"
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
def test_handle_second_event_updates_singleton_and_replaces_recent() -> None:
    """Second event updates the singleton row; recent_responses is replaced.

    OMN-15707: recent_responses no longer accumulates across events (that
    required a pre-upsert read against a write-only-declared table, which
    DLQ'd every live event). Each upsert now writes a window of exactly the
    current event -- a disclosed behavior change from the prior FIFO
    rolling-history semantics, documented in the handler module docstring.
    """
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
    assert len(recent2) == 1
    entry = recent2[0]
    assert isinstance(entry, dict)
    assert entry["generated_text"] == "response 1"


@pytest.mark.unit
def test_handle_recent_responses_stays_window_size_one() -> None:
    """recent_responses never grows past 1 entry (OMN-15707, was MAX_HISTORY)."""
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
    assert len(capped) == 1, (
        f"recent_responses must stay at window size 1, got {len(capped)}"
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


@pytest.mark.unit
def test_two_tenants_land_on_two_distinct_rows_omn14894() -> None:
    """OMN-14894 tranche 2: the confirmed cross-tenant leak closes.

    Before this tranche, every inference-response event upserted the same
    hardcoded 'global' singleton row regardless of tenant_id -- two tenants
    sharing write traffic collapsed onto one row, silently overwriting each
    other's latest_generated_text. This proves tenant A's event and tenant
    B's event now produce two independent rows, keyed by tenant_id.
    """
    db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionDelegationInferenceResponse()

    handler.handle(
        {
            "_db": db,
            "_topic": "onex.evt.omnibase-infra.inference-response.v1",
            "_partition": 0,
            "_offset": 1,
            "correlation_id": str(uuid4()),
            "content": "tenant A response",
            "model_used": "glm-5.2",
            "tenant_id": "tenant-a",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "latency_ms": 50,
        }
    )
    handler.handle(
        {
            "_db": db,
            "_topic": "onex.evt.omnibase-infra.inference-response.v1",
            "_partition": 0,
            "_offset": 2,
            "correlation_id": str(uuid4()),
            "content": "tenant B response",
            "model_used": "glm-5.2",
            "tenant_id": "tenant-b",
            "prompt_tokens": 20,
            "completion_tokens": 8,
            "latency_ms": 60,
        }
    )

    rows = db.query(TABLE)
    assert len(rows) == 2, "two distinct tenants must never collapse onto one row"
    by_tenant = {row["tenant_id"]: row for row in rows}
    assert by_tenant["tenant-a"]["latest_generated_text"] == "tenant A response"
    assert by_tenant["tenant-b"]["latest_generated_text"] == "tenant B response"
    assert by_tenant["tenant-a"]["singleton_key"] == "tenant-a"
    assert by_tenant["tenant-b"]["singleton_key"] == "tenant-b"


@pytest.mark.unit
def test_missing_tenant_id_falls_back_to_default_tenant_omn14894() -> None:
    """A payload with no tenant_id key still stamps DEFAULT_TENANT, never blank."""
    db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionDelegationInferenceResponse()

    handler.handle(
        {
            "_db": db,
            "_topic": "onex.evt.omnibase-infra.inference-response.v1",
            "_partition": 0,
            "_offset": 1,
            "correlation_id": str(uuid4()),
            "content": "no tenant on the wire",
            "model_used": "glm-5.2",
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "latency_ms": 1,
        }
    )

    rows = db.query(TABLE)
    assert len(rows) == 1
    assert rows[0]["tenant_id"] == "omninode"
    assert rows[0]["singleton_key"] == "omninode"
