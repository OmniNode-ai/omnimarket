"""Tests for BaseProjectionRunner helper functions."""

from datetime import UTC, datetime

from omnimarket.projection.runner import (
    coalesce,
    deterministic_correlation_id,
    safe_float,
    safe_int,
    safe_parse_date,
)


class TestDeterministicCorrelationId:
    def test_format(self) -> None:
        result = deterministic_correlation_id("topic", 0, 42)
        # UUID-shaped: 8-4-4-4-12 hex
        parts = result.split("-")
        assert len(parts) == 5
        assert len(parts[0]) == 8
        assert len(parts[1]) == 4
        assert len(parts[2]) == 4
        assert len(parts[3]) == 4
        assert len(parts[4]) == 12

    def test_deterministic(self) -> None:
        a = deterministic_correlation_id("topic", 0, 100)
        b = deterministic_correlation_id("topic", 0, 100)
        assert a == b

    def test_different_inputs(self) -> None:
        a = deterministic_correlation_id("topic", 0, 100)
        b = deterministic_correlation_id("topic", 0, 101)
        assert a != b


class TestSafeParseDate:
    def test_iso_string(self) -> None:
        result = safe_parse_date("2026-04-06T12:00:00Z")
        assert isinstance(result, datetime)

    def test_none(self) -> None:
        result = safe_parse_date(None)
        assert isinstance(result, datetime)
        # Should be approximately now
        delta = abs((datetime.now(UTC) - result).total_seconds())
        assert delta < 5

    def test_empty_string(self) -> None:
        result = safe_parse_date("")
        assert isinstance(result, datetime)

    def test_datetime_passthrough(self) -> None:
        dt = datetime(2026, 1, 1, tzinfo=UTC)
        result = safe_parse_date(dt)
        assert result == dt

    def test_malformed(self) -> None:
        result = safe_parse_date("not-a-date")
        assert isinstance(result, datetime)


class TestSafeFloat:
    def test_valid(self) -> None:
        assert safe_float("3.14") == 3.14

    def test_none(self) -> None:
        assert safe_float(None) == 0.0

    def test_invalid(self) -> None:
        assert safe_float("abc") == 0.0

    def test_nan(self) -> None:
        assert safe_float(float("nan")) == 0.0


class TestSafeInt:
    def test_valid(self) -> None:
        assert safe_int("42") == 42

    def test_none(self) -> None:
        assert safe_int(None) == 0

    def test_invalid(self) -> None:
        assert safe_int("abc") == 0


class TestCoalesce:
    def test_first_truthy(self) -> None:
        assert coalesce(None, "", "hello") == "hello"

    def test_all_falsy(self) -> None:
        assert coalesce(None, "", 0) == 0

    def test_empty(self) -> None:
        assert coalesce() is None
