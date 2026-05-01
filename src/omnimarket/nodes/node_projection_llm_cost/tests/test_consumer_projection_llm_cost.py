# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_projection_llm_cost consumer.

Tests cover:
- Schema validation (_validate_event)
- Row building (_build_row)
- Idempotent insert logic (ON CONFLICT DO NOTHING via mock DB)
- Malformed event handling (parse errors, missing fields)
- Envelope unwrapping integration
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnimarket.nodes.node_projection_llm_cost.consumer import (
    CONSUMER_GROUP,
    SUBSCRIBE_TOPIC,
    TABLE,
    _build_row,
    _insert_row,
    _validate_event,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _minimal_event(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "model_id": "gpt-4o",
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "usage_source": "API",
        "correlation_id": str(uuid.uuid4()),
        "input_hash": "abc123",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _validate_event
# ---------------------------------------------------------------------------


class TestValidateEvent:
    def test_valid_event_with_model_id_and_tokens(self) -> None:
        assert _validate_event(_minimal_event()) is True

    def test_valid_event_with_model_name_fallback(self) -> None:
        data = _minimal_event()
        del data["model_id"]
        data["model_name"] = "claude-3-opus"
        assert _validate_event(data) is True

    def test_invalid_missing_model(self) -> None:
        data = _minimal_event()
        del data["model_id"]
        assert _validate_event(data) is False

    def test_invalid_missing_all_tokens(self) -> None:
        data = {
            "model_id": "gpt-4o",
            "usage_source": "API",
        }
        assert _validate_event(data) is False

    def test_valid_with_only_prompt_tokens(self) -> None:
        data = {"model_id": "gpt-4o", "prompt_tokens": 10}
        assert _validate_event(data) is True

    def test_valid_with_only_total_tokens(self) -> None:
        data = {"model_id": "gpt-4o", "total_tokens": 200}
        assert _validate_event(data) is True


# ---------------------------------------------------------------------------
# _build_row
# ---------------------------------------------------------------------------


class TestBuildRow:
    def test_builds_complete_row(self) -> None:
        corr_id = str(uuid.uuid4())
        data = _minimal_event(correlation_id=corr_id, session_id="sess-1")
        row = _build_row(data)

        assert row["model_id"] == "gpt-4o"
        assert row["prompt_tokens"] == 100
        assert row["completion_tokens"] == 50
        assert row["total_tokens"] == 150
        assert row["usage_source"] == "API"
        assert row["usage_is_estimated"] is False
        assert row["correlation_id"] == corr_id
        assert row["session_id"] == "sess-1"
        assert row["input_hash"] == "abc123"

    def test_model_name_fallback_to_model_id(self) -> None:
        data = {"model_name": "claude-3-opus", "total_tokens": 100}
        row = _build_row(data)
        assert row["model_id"] == "claude-3-opus"

    def test_unknown_model_when_both_missing(self) -> None:
        data = {"total_tokens": 100}
        row = _build_row(data)
        assert row["model_id"] == "unknown"

    def test_usage_source_estimated_sets_flag(self) -> None:
        data = _minimal_event(usage_source="ESTIMATED")
        row = _build_row(data)
        assert row["usage_source"] == "ESTIMATED"
        assert row["usage_is_estimated"] is True

    def test_invalid_usage_source_defaults_to_missing(self) -> None:
        data = _minimal_event(usage_source="GARBAGE")
        row = _build_row(data)
        assert row["usage_source"] == "MISSING"

    def test_non_uuid_correlation_id_stored_as_none(self) -> None:
        data = _minimal_event(correlation_id="not-a-uuid")
        row = _build_row(data)
        assert row["correlation_id"] is None

    def test_valid_uuid_correlation_id_preserved(self) -> None:
        corr_id = str(uuid.uuid4())
        data = _minimal_event(correlation_id=corr_id)
        row = _build_row(data)
        assert row["correlation_id"] == corr_id

    def test_input_hash_from_idempotency_key_alias(self) -> None:
        data = _minimal_event()
        del data["input_hash"]
        data["idempotency_key"] = "key-xyz"
        row = _build_row(data)
        assert row["input_hash"] == "key-xyz"

    def test_input_hash_truncated_to_71_chars(self) -> None:
        long_hash = "x" * 100
        data = _minimal_event(input_hash=long_hash)
        row = _build_row(data)
        assert row["input_hash"] is not None
        assert len(row["input_hash"]) == 71

    def test_tokens_derive_total_when_zero(self) -> None:
        data = _minimal_event(total_tokens=0, prompt_tokens=30, completion_tokens=20)
        row = _build_row(data)
        # When total_tokens is 0 but p+c > 0, we derive total
        assert row["total_tokens"] == 50

    def test_prompt_token_aliases_accepted(self) -> None:
        data = {
            "model_id": "gpt-4o",
            "input_tokens": 80,
            "output_tokens": 20,
            "total_tokens": 100,
        }
        row = _build_row(data)
        assert row["prompt_tokens"] == 80
        assert row["completion_tokens"] == 20

    def test_created_at_is_datetime(self) -> None:
        data = _minimal_event(timestamp="2026-01-01T00:00:00Z")
        row = _build_row(data)
        assert isinstance(row["created_at"], datetime)

    def test_created_at_fallback_when_no_timestamp(self) -> None:
        data = _minimal_event()
        row = _build_row(data)
        assert isinstance(row["created_at"], datetime)

    def test_latency_ms_none_when_absent(self) -> None:
        data = _minimal_event()
        row = _build_row(data)
        assert row["latency_ms"] is None

    def test_latency_ms_parsed_when_present(self) -> None:
        data = _minimal_event(latency_ms=1234)
        row = _build_row(data)
        assert row["latency_ms"] == pytest.approx(1234.0)

    def test_zero_tokens_stored_as_none(self) -> None:
        data = _minimal_event(prompt_tokens=0, completion_tokens=0, total_tokens=0)
        row = _build_row(data)
        assert row["prompt_tokens"] is None
        assert row["completion_tokens"] is None
        assert row["total_tokens"] is None


# ---------------------------------------------------------------------------
# _insert_row — mock DB
# ---------------------------------------------------------------------------


class TestInsertRow:
    @pytest.mark.asyncio
    async def test_insert_calls_execute_with_on_conflict(self) -> None:
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=[])
        row = _build_row(_minimal_event())
        result = await _insert_row(mock_db, row)

        assert result is True
        mock_db.execute.assert_called_once()
        call_sql: str = mock_db.execute.call_args[0][0]
        assert "ON CONFLICT DO NOTHING" in call_sql
        assert TABLE in call_sql

    @pytest.mark.asyncio
    async def test_insert_passes_12_positional_params(self) -> None:
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=[])
        row = _build_row(_minimal_event())
        await _insert_row(mock_db, row)

        call_args = mock_db.execute.call_args[0]
        # first arg is SQL, remaining are params
        params = call_args[1:]
        assert len(params) == 12

    @pytest.mark.asyncio
    async def test_second_insert_same_input_hash_is_idempotent(self) -> None:
        """ON CONFLICT DO NOTHING means inserting twice should not raise."""
        inserted_hashes: set[str] = set()

        async def fake_execute(sql: str, *params: Any) -> list[Any]:
            if "ON CONFLICT" in sql:
                hash_val = params[10]  # input_hash is 11th param (index 10)
                if hash_val in inserted_hashes:
                    return []  # simulates conflict — no rows inserted
                if hash_val:
                    inserted_hashes.add(hash_val)
            return []

        mock_db = MagicMock()
        mock_db.execute = AsyncMock(side_effect=fake_execute)

        row = _build_row(_minimal_event(input_hash="dedup-key-42"))

        result1 = await _insert_row(mock_db, row)
        result2 = await _insert_row(mock_db, row)

        assert result1 is True
        assert result2 is True  # no exception — conflict silently skipped
        assert mock_db.execute.call_count == 2


# ---------------------------------------------------------------------------
# Envelope unwrapping integration
# ---------------------------------------------------------------------------


class TestEnvelopeUnwrapping:
    def test_unwraps_payload_envelope(self) -> None:
        from omnimarket.projection.envelope import unwrap_envelope

        inner = _minimal_event()
        envelope = {"payload": inner, "event_type": "llm-call-completed"}
        raw = json.dumps(envelope).encode()
        data = unwrap_envelope(raw)
        assert data is not None
        assert data["model_id"] == "gpt-4o"
        assert _validate_event(data) is True

    def test_unwraps_data_envelope(self) -> None:
        from omnimarket.projection.envelope import unwrap_envelope

        inner = _minimal_event()
        envelope = {"data": inner, "correlation_id": str(uuid.uuid4())}
        raw = json.dumps(envelope).encode()
        data = unwrap_envelope(raw)
        assert data is not None
        assert data["model_id"] == "gpt-4o"

    def test_non_json_returns_none(self) -> None:
        from omnimarket.projection.envelope import unwrap_envelope

        result = unwrap_envelope(b"not json at all!!")
        assert result is None

    def test_empty_bytes_returns_none(self) -> None:
        from omnimarket.projection.envelope import unwrap_envelope

        result = unwrap_envelope(b"null")
        assert result is None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_subscribe_topic_matches_contract(self) -> None:
        assert SUBSCRIBE_TOPIC == "onex.evt.omniintelligence.llm-call-completed.v1"

    def test_consumer_group_format(self) -> None:
        assert CONSUMER_GROUP == "local.omnimarket.projection-llm-cost.consume.v1"

    def test_table_name(self) -> None:
        assert TABLE == "llm_call_metrics"
