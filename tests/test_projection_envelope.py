"""Tests for ONEX envelope unwrapping logic."""

import json

from omnimarket.projection.envelope import unwrap_envelope


class TestUnwrapEnvelope:
    def test_payload_envelope(self) -> None:
        raw = json.dumps(
            {"payload": {"session_id": "abc", "outcome": "success"}}
        ).encode()
        result = unwrap_envelope(raw)
        assert result is not None
        assert result["session_id"] == "abc"
        assert result["outcome"] == "success"
        assert "_envelope" in result

    def test_data_envelope(self) -> None:
        raw = json.dumps(
            {
                "event_type": "session-outcome.v1",
                "correlation_id": "corr-123",
                "data": {"session_id": "xyz", "outcome": "failure"},
            }
        ).encode()
        result = unwrap_envelope(raw)
        assert result is not None
        assert result["session_id"] == "xyz"
        assert result["_event_type"] == "session-outcome.v1"
        assert result["_correlation_id"] == "corr-123"

    def test_raw_fallback(self) -> None:
        raw = json.dumps({"session_id": "raw-123", "outcome": "unknown"}).encode()
        result = unwrap_envelope(raw)
        assert result is not None
        assert result["session_id"] == "raw-123"

    def test_invalid_json(self) -> None:
        result = unwrap_envelope(b"not-json")
        assert result is None

    def test_non_dict(self) -> None:
        result = unwrap_envelope(json.dumps([1, 2, 3]).encode())
        assert result is None

    def test_empty_bytes(self) -> None:
        result = unwrap_envelope(b"")
        assert result is None

    def test_data_envelope_with_list_data(self) -> None:
        """When data is a list (not dict), return the raw object instead."""
        raw = json.dumps({"data": [1, 2, 3], "event_type": "test"}).encode()
        result = unwrap_envelope(raw)
        assert result is not None
        assert result["data"] == [1, 2, 3]
