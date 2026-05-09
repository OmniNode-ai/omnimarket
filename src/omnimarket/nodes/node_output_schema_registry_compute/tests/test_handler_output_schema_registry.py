"""Tests for HandlerOutputSchemaRegistry — deterministic schema key resolution."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_output_schema_registry_compute.handlers.handler_output_schema_registry import (
    HandlerOutputSchemaRegistry,
    known_schema_keys,
)
from omnimarket.nodes.node_output_schema_registry_compute.models.model_schema_registry_request import (
    ModelSchemaRegistryRequest,
)
from omnimarket.nodes.node_output_schema_registry_compute.models.model_schema_registry_result import (
    EnumSchemaRegistryStatus,
)


@pytest.mark.unit
class TestHandlerOutputSchemaRegistrySuccess:
    def test_review_output_returns_ok(self) -> None:
        request = ModelSchemaRegistryRequest(schema_key="review_output")
        result = HandlerOutputSchemaRegistry().handle(request)
        assert result.status == EnumSchemaRegistryStatus.OK

    def test_review_output_schema_key_echoed(self) -> None:
        request = ModelSchemaRegistryRequest(schema_key="review_output")
        result = HandlerOutputSchemaRegistry().handle(request)
        assert result.schema_key == "review_output"

    def test_review_output_schema_is_dict(self) -> None:
        request = ModelSchemaRegistryRequest(schema_key="review_output")
        result = HandlerOutputSchemaRegistry().handle(request)
        assert isinstance(result.json_schema, dict)

    def test_review_output_schema_has_title(self) -> None:
        request = ModelSchemaRegistryRequest(schema_key="review_output")
        result = HandlerOutputSchemaRegistry().handle(request)
        assert result.json_schema is not None
        assert result.json_schema.get("title") == "ModelReviewOutput"

    def test_review_output_schema_has_properties(self) -> None:
        request = ModelSchemaRegistryRequest(schema_key="review_output")
        result = HandlerOutputSchemaRegistry().handle(request)
        assert result.json_schema is not None
        props = result.json_schema.get("properties", {})
        assert "verdict" in props
        assert "summary" in props
        assert "findings" in props

    def test_plan_document_returns_ok(self) -> None:
        request = ModelSchemaRegistryRequest(schema_key="plan_document")
        result = HandlerOutputSchemaRegistry().handle(request)
        assert result.status == EnumSchemaRegistryStatus.OK

    def test_plan_document_schema_is_dict(self) -> None:
        request = ModelSchemaRegistryRequest(schema_key="plan_document")
        result = HandlerOutputSchemaRegistry().handle(request)
        assert isinstance(result.json_schema, dict)

    def test_no_error_on_success(self) -> None:
        request = ModelSchemaRegistryRequest(schema_key="review_output")
        result = HandlerOutputSchemaRegistry().handle(request)
        assert result.error is None


@pytest.mark.unit
class TestHandlerOutputSchemaRegistryError:
    def test_unknown_key_returns_error_status(self) -> None:
        request = ModelSchemaRegistryRequest(schema_key="nonexistent_key")
        result = HandlerOutputSchemaRegistry().handle(request)
        assert result.status == EnumSchemaRegistryStatus.ERROR

    def test_unknown_key_schema_is_none(self) -> None:
        request = ModelSchemaRegistryRequest(schema_key="nonexistent_key")
        result = HandlerOutputSchemaRegistry().handle(request)
        assert result.json_schema is None

    def test_unknown_key_error_message_contains_key(self) -> None:
        request = ModelSchemaRegistryRequest(schema_key="bad_key")
        result = HandlerOutputSchemaRegistry().handle(request)
        assert result.error is not None
        assert "bad_key" in result.error

    def test_unknown_key_error_message_lists_known_keys(self) -> None:
        request = ModelSchemaRegistryRequest(schema_key="bad_key")
        result = HandlerOutputSchemaRegistry().handle(request)
        assert result.error is not None
        assert "review_output" in result.error

    def test_unknown_key_echoed_in_result(self) -> None:
        request = ModelSchemaRegistryRequest(schema_key="bad_key")
        result = HandlerOutputSchemaRegistry().handle(request)
        assert result.schema_key == "bad_key"


@pytest.mark.unit
class TestHandlerOutputSchemaRegistryDeterminism:
    def test_same_key_produces_identical_schema(self) -> None:
        request = ModelSchemaRegistryRequest(schema_key="review_output")
        handler = HandlerOutputSchemaRegistry()
        result_a = handler.handle(request)
        result_b = handler.handle(request)
        assert result_a.json_schema == result_b.json_schema

    def test_different_keys_produce_different_schemas(self) -> None:
        handler = HandlerOutputSchemaRegistry()
        result_a = handler.handle(
            ModelSchemaRegistryRequest(schema_key="review_output")
        )
        result_b = handler.handle(
            ModelSchemaRegistryRequest(schema_key="plan_document")
        )
        assert result_a.json_schema != result_b.json_schema


@pytest.mark.unit
class TestKnownSchemaKeys:
    def test_known_keys_includes_review_output(self) -> None:
        assert "review_output" in known_schema_keys()

    def test_known_keys_includes_plan_document(self) -> None:
        assert "plan_document" in known_schema_keys()

    def test_known_keys_is_sorted(self) -> None:
        keys = known_schema_keys()
        assert keys == sorted(keys)
