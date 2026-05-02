# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for node_projection_llm_cost consumer.

Tests cover:
- Schema validation (_validate_event)
- Row building (_build_row) — all columns including new ones
- Idempotent insert logic (ON CONFLICT (input_hash) DO NOTHING via mock DB)
- Malformed event handling (parse errors, missing fields)
- Envelope unwrapping integration
- usage_source mapping (MEASURED → API, UNKNOWN → MISSING)
- input_hash determinism via sha256 formula
"""

from __future__ import annotations

import hashlib
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
    _compute_input_hash,
    _insert_row,
    _validate_event,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _minimal_event(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "model_id": "claude-sonnet-4-6",
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "usage_source": "MEASURED",
        "correlation_id": str(uuid.uuid4()),
        "session_id": "sess-abc",
        "reporting_source": "ab-compare",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# _compute_input_hash
# ---------------------------------------------------------------------------


class TestComputeInputHash:
    def test_deterministic(self) -> None:
        h1 = _compute_input_hash("ab-compare", "sess-1", "gpt-4o", 100, 50)
        h2 = _compute_input_hash("ab-compare", "sess-1", "gpt-4o", 100, 50)
        assert h1 == h2

    def test_differs_on_source(self) -> None:
        h1 = _compute_input_hash("src-a", "sess-1", "gpt-4o", 100, 50)
        h2 = _compute_input_hash("src-b", "sess-1", "gpt-4o", 100, 50)
        assert h1 != h2

    def test_matches_sha256_formula(self) -> None:
        key = "ab-compare:sess-1:gpt-4o:100:50"
        expected = hashlib.sha256(key.encode()).hexdigest()
        assert (
            _compute_input_hash("ab-compare", "sess-1", "gpt-4o", 100, 50) == expected
        )

    def test_none_fields_use_empty_string(self) -> None:
        h = _compute_input_hash(None, None, "gpt-4o", None, None)
        expected = hashlib.sha256(b"::gpt-4o:0:0").hexdigest()
        assert h == expected


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
        data: dict[str, Any] = {
            "model_id": "gpt-4o",
            "usage_source": "API",
        }
        assert _validate_event(data) is False

    def test_valid_with_only_prompt_tokens(self) -> None:
        data: dict[str, Any] = {"model_id": "gpt-4o", "prompt_tokens": 10}
        assert _validate_event(data) is True

    def test_valid_with_only_total_tokens(self) -> None:
        data: dict[str, Any] = {"model_id": "gpt-4o", "total_tokens": 200}
        assert _validate_event(data) is True


# ---------------------------------------------------------------------------
# _build_row — all columns
# ---------------------------------------------------------------------------


class TestBuildRow:
    def test_builds_complete_row_shape(self) -> None:
        row = _build_row(_minimal_event())
        expected_keys = {
            "correlation_id",
            "session_id",
            "run_id",
            "model_id",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "estimated_cost_usd",
            "latency_ms",
            "usage_source",
            "usage_is_estimated",
            "usage_raw",
            "input_hash",
            "source",
            "code_version",
            "contract_version",
            "created_at",
        }
        assert expected_keys.issubset(row.keys())

    def test_measured_maps_to_api(self) -> None:
        row = _build_row(_minimal_event(usage_source="MEASURED"))
        assert row["usage_source"] == "API"
        assert row["usage_is_estimated"] is False

    def test_api_usage_source_preserved(self) -> None:
        row = _build_row(_minimal_event(usage_source="API"))
        assert row["usage_source"] == "API"
        assert row["usage_is_estimated"] is False

    def test_estimated_usage_source(self) -> None:
        row = _build_row(_minimal_event(usage_source="ESTIMATED"))
        assert row["usage_source"] == "ESTIMATED"
        assert row["usage_is_estimated"] is True

    def test_unknown_usage_source_maps_to_missing(self) -> None:
        row = _build_row(_minimal_event(usage_source="UNKNOWN"))
        assert row["usage_source"] == "MISSING"
        assert row["usage_is_estimated"] is True

    def test_garbage_usage_source_maps_to_missing(self) -> None:
        row = _build_row(_minimal_event(usage_source="GARBAGE"))
        assert row["usage_source"] == "MISSING"

    def test_source_from_reporting_source(self) -> None:
        row = _build_row(_minimal_event(reporting_source="ab-compare"))
        assert row["source"] == "ab-compare"

    def test_source_none_when_absent(self) -> None:
        data = _minimal_event()
        del data["reporting_source"]
        row = _build_row(data)
        assert row["source"] is None

    def test_usage_raw_is_json_string(self) -> None:
        data = _minimal_event()
        row = _build_row(data)
        parsed = json.loads(row["usage_raw"])
        assert parsed["model_id"] == "claude-sonnet-4-6"

    def test_usage_raw_contains_full_payload(self) -> None:
        data = _minimal_event(extra_field="sentinel-value")
        row = _build_row(data)
        parsed = json.loads(row["usage_raw"])
        assert parsed["extra_field"] == "sentinel-value"

    def test_input_hash_is_sha256_hex(self) -> None:
        row = _build_row(_minimal_event())
        assert len(row["input_hash"]) == 64
        assert all(c in "0123456789abcdef" for c in row["input_hash"])

    def test_input_hash_deterministic_across_calls(self) -> None:
        data = _minimal_event()
        row1 = _build_row(data)
        row2 = _build_row(data)
        assert row1["input_hash"] == row2["input_hash"]

    def test_input_hash_matches_formula(self) -> None:
        data = _minimal_event(
            reporting_source="ab-compare",
            session_id="sess-abc",
            model_id="claude-sonnet-4-6",
            prompt_tokens=100,
            completion_tokens=50,
        )
        row = _build_row(data)
        expected = hashlib.sha256(
            b"ab-compare:sess-abc:claude-sonnet-4-6:100:50"
        ).hexdigest()
        assert row["input_hash"] == expected

    def test_run_id_populated_when_present(self) -> None:
        row = _build_row(_minimal_event(run_id="run-xyz"))
        assert row["run_id"] == "run-xyz"

    def test_run_id_none_when_absent(self) -> None:
        row = _build_row(_minimal_event())
        assert row["run_id"] is None

    def test_code_version_always_none(self) -> None:
        row = _build_row(_minimal_event())
        assert row["code_version"] is None

    def test_contract_version_always_none(self) -> None:
        row = _build_row(_minimal_event())
        assert row["contract_version"] is None

    def test_correlation_id_as_uuid_string(self) -> None:
        corr = str(uuid.uuid4())
        row = _build_row(_minimal_event(correlation_id=corr))
        assert row["correlation_id"] == corr

    def test_non_uuid_correlation_id_stored_as_none(self) -> None:
        row = _build_row(_minimal_event(correlation_id="not-a-uuid"))
        assert row["correlation_id"] is None

    def test_latency_ms_from_event(self) -> None:
        row = _build_row(_minimal_event(latency_ms=19820))
        assert row["latency_ms"] == pytest.approx(19820.0)

    def test_latency_ms_none_when_absent(self) -> None:
        data = _minimal_event()
        data.pop("latency_ms", None)
        row = _build_row(data)
        assert row["latency_ms"] is None

    def test_model_name_fallback(self) -> None:
        data = _minimal_event()
        del data["model_id"]
        data["model_name"] = "claude-3-opus"
        row = _build_row(data)
        assert row["model_id"] == "claude-3-opus"

    def test_unknown_model_when_both_missing(self) -> None:
        data: dict[str, Any] = {"total_tokens": 100}
        row = _build_row(data)
        assert row["model_id"] == "unknown"

    def test_created_at_from_emitted_at(self) -> None:
        data = _minimal_event(emitted_at="2026-05-02T19:35:51.171478+00:00")
        row = _build_row(data)
        assert isinstance(row["created_at"], datetime)

    def test_created_at_fallback_when_no_timestamp(self) -> None:
        data = _minimal_event()
        row = _build_row(data)
        assert isinstance(row["created_at"], datetime)

    def test_zero_tokens_stored_as_none(self) -> None:
        data = _minimal_event(prompt_tokens=0, completion_tokens=0, total_tokens=0)
        row = _build_row(data)
        assert row["prompt_tokens"] is None
        assert row["completion_tokens"] is None
        assert row["total_tokens"] is None

    def test_tokens_derive_total_when_zero(self) -> None:
        data = _minimal_event(total_tokens=0, prompt_tokens=30, completion_tokens=20)
        row = _build_row(data)
        assert row["total_tokens"] == 50

    def test_prompt_token_aliases_accepted(self) -> None:
        data: dict[str, Any] = {
            "model_id": "gpt-4o",
            "input_tokens": 80,
            "output_tokens": 20,
            "total_tokens": 100,
        }
        row = _build_row(data)
        assert row["prompt_tokens"] == 80
        assert row["completion_tokens"] == 20


# ---------------------------------------------------------------------------
# _insert_row — mock DB
# ---------------------------------------------------------------------------


class TestInsertRow:
    @pytest.mark.asyncio
    async def test_insert_calls_execute_with_on_conflict_input_hash(self) -> None:
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=[])
        row = _build_row(_minimal_event())
        result = await _insert_row(mock_db, row)

        assert result is True
        mock_db.execute.assert_called_once()
        call_sql: str = mock_db.execute.call_args[0][0]
        assert "ON CONFLICT (input_hash) DO NOTHING" in call_sql
        assert TABLE in call_sql

    @pytest.mark.asyncio
    async def test_insert_passes_17_positional_params(self) -> None:
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=[])
        row = _build_row(_minimal_event())
        await _insert_row(mock_db, row)

        call_args = mock_db.execute.call_args[0]
        params = call_args[1:]
        assert len(params) == 17

    @pytest.mark.asyncio
    async def test_insert_includes_usage_raw_jsonb_cast(self) -> None:
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=[])
        row = _build_row(_minimal_event())
        await _insert_row(mock_db, row)

        call_sql: str = mock_db.execute.call_args[0][0]
        assert "$12::jsonb" in call_sql

    @pytest.mark.asyncio
    async def test_second_insert_same_input_hash_is_idempotent(self) -> None:
        inserted_hashes: set[str] = set()

        async def fake_execute(sql: str, *params: Any) -> list[Any]:
            if "ON CONFLICT" in sql:
                hash_val = params[12]  # input_hash is 13th param (index 12)
                if hash_val in inserted_hashes:
                    return []
                if hash_val:
                    inserted_hashes.add(hash_val)
            return []

        mock_db = MagicMock()
        mock_db.execute = AsyncMock(side_effect=fake_execute)

        row = _build_row(_minimal_event())

        result1 = await _insert_row(mock_db, row)
        result2 = await _insert_row(mock_db, row)

        assert result1 is True
        assert result2 is True
        assert mock_db.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_insert_includes_all_new_columns_in_sql(self) -> None:
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=[])
        row = _build_row(_minimal_event())
        await _insert_row(mock_db, row)

        call_sql: str = mock_db.execute.call_args[0][0]
        for col in (
            "run_id",
            "source",
            "usage_raw",
            "code_version",
            "contract_version",
        ):
            assert col in call_sql, f"column {col!r} missing from INSERT"


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
        assert data["model_id"] == "claude-sonnet-4-6"
        assert _validate_event(data) is True

    def test_unwraps_data_envelope(self) -> None:
        from omnimarket.projection.envelope import unwrap_envelope

        inner = _minimal_event()
        envelope = {"data": inner, "correlation_id": str(uuid.uuid4())}
        raw = json.dumps(envelope).encode()
        data = unwrap_envelope(raw)
        assert data is not None
        assert data["model_id"] == "claude-sonnet-4-6"

    def test_non_json_returns_none(self) -> None:
        from omnimarket.projection.envelope import unwrap_envelope

        result = unwrap_envelope(b"not json at all!!")
        assert result is None

    def test_empty_bytes_returns_none(self) -> None:
        from omnimarket.projection.envelope import unwrap_envelope

        result = unwrap_envelope(b"null")
        assert result is None


# ---------------------------------------------------------------------------
# Full event payload (mirrors the actual Kafka event shape from the ticket)
# ---------------------------------------------------------------------------


class TestRealEventPayload:
    def test_canonical_event_maps_correctly(self) -> None:
        event: dict[str, Any] = {
            "correlation_id": "d498ad36-0000-0000-0000-000000000001",
            "model_id": "claude-sonnet-4-6",
            "prompt_tokens": 28416,
            "completion_tokens": 55,
            "total_tokens": 28471,
            "estimated_cost_usd": 0.17897125,
            "latency_ms": 19820,
            "usage_source": "MEASURED",
            "reporting_source": "ab-compare",
            "session_id": "b45a01ea-0000-0000-0000-000000000001",
            "emitted_at": "2026-05-02T19:35:51.171478+00:00",
        }
        row = _build_row(event)

        assert row["usage_source"] == "API"
        assert row["usage_is_estimated"] is False
        assert row["latency_ms"] == pytest.approx(19820.0)
        assert row["source"] == "ab-compare"
        assert row["correlation_id"] == "d498ad36-0000-0000-0000-000000000001"
        assert row["session_id"] == "b45a01ea-0000-0000-0000-000000000001"
        assert row["model_id"] == "claude-sonnet-4-6"
        assert row["prompt_tokens"] == 28416
        assert row["completion_tokens"] == 55
        assert row["total_tokens"] == 28471
        assert row["estimated_cost_usd"] == pytest.approx(0.17897125)
        assert isinstance(row["created_at"], datetime)
        assert len(row["input_hash"]) == 64

        parsed_raw = json.loads(row["usage_raw"])
        assert parsed_raw["usage_source"] == "MEASURED"
        assert parsed_raw["latency_ms"] == 19820


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
