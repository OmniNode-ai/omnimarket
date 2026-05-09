"""Tests for HandlerSchemaRepair — deterministic schema repair prompt construction."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_schema_repair_compute.handlers.handler_schema_repair import (
    HandlerSchemaRepair,
)
from omnimarket.nodes.node_schema_repair_compute.models.model_repair_request import (
    ModelSchemaRepairRequest,
)
from omnimarket.nodes.node_schema_repair_compute.models.model_repair_result import (
    EnumRepairStatus,
)

_SIMPLE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "score": {"type": "number"},
    },
    "required": ["name", "score"],
}

_TYPE_ERROR: dict[str, object] = {
    "loc": ["score"],
    "msg": "Input should be a valid number, unable to parse string as a number",
    "type": "float_parsing",
    "input": "not-a-number",
}

_MISSING_ERROR: dict[str, object] = {
    "loc": ["name"],
    "msg": "Field required",
    "type": "missing",
    "input": {},
}


def _make_request(
    *,
    malformed_output: str = '{"score": "not-a-number"}',
    validation_errors: list[dict[str, object]] | None = None,
    target_schema: dict[str, object] | None = None,
    original_prompt: str = "Extract the name and score from the document.",
    run_id: str = "",
) -> ModelSchemaRepairRequest:
    return ModelSchemaRepairRequest(
        malformed_output=malformed_output,
        validation_errors=validation_errors
        if validation_errors is not None
        else [_TYPE_ERROR],
        target_schema=target_schema if target_schema is not None else _SIMPLE_SCHEMA,
        original_prompt=original_prompt,
        run_id=run_id,
    )


@pytest.mark.unit
class TestHandlerSchemaRepairStatus:
    def test_status_ok(self) -> None:
        result = HandlerSchemaRepair().handle(_make_request())
        assert result.status == EnumRepairStatus.OK

    def test_run_id_passed_through(self) -> None:
        result = HandlerSchemaRepair().handle(_make_request(run_id="run-abc"))
        assert result.run_id == "run-abc"

    def test_no_error_field_on_success(self) -> None:
        result = HandlerSchemaRepair().handle(_make_request())
        assert result.error is None


@pytest.mark.unit
class TestHandlerSchemaRepairRepairable:
    def test_repairable_true_for_type_errors(self) -> None:
        result = HandlerSchemaRepair().handle(
            _make_request(validation_errors=[_TYPE_ERROR])
        )
        assert result.repairable is True

    def test_repairable_true_for_missing_field(self) -> None:
        result = HandlerSchemaRepair().handle(
            _make_request(validation_errors=[_MISSING_ERROR])
        )
        assert result.repairable is True

    def test_repairable_false_for_empty_errors(self) -> None:
        result = HandlerSchemaRepair().handle(_make_request(validation_errors=[]))
        assert result.repairable is False

    def test_repairable_true_for_multiple_errors(self) -> None:
        result = HandlerSchemaRepair().handle(
            _make_request(validation_errors=[_TYPE_ERROR, _MISSING_ERROR])
        )
        assert result.repairable is True


@pytest.mark.unit
class TestHandlerSchemaRepairErrorSummary:
    def test_error_summary_mentions_field_name(self) -> None:
        result = HandlerSchemaRepair().handle(
            _make_request(validation_errors=[_TYPE_ERROR])
        )
        assert "score" in result.error_summary

    def test_error_summary_mentions_error_type(self) -> None:
        result = HandlerSchemaRepair().handle(
            _make_request(validation_errors=[_TYPE_ERROR])
        )
        assert "float_parsing" in result.error_summary

    def test_error_summary_mentions_error_msg(self) -> None:
        result = HandlerSchemaRepair().handle(
            _make_request(validation_errors=[_TYPE_ERROR])
        )
        assert "valid number" in result.error_summary

    def test_error_summary_counts_errors(self) -> None:
        result = HandlerSchemaRepair().handle(
            _make_request(validation_errors=[_TYPE_ERROR, _MISSING_ERROR])
        )
        assert "2" in result.error_summary

    def test_error_summary_no_errors_message(self) -> None:
        result = HandlerSchemaRepair().handle(_make_request(validation_errors=[]))
        assert "No validation errors" in result.error_summary

    def test_error_summary_nested_loc(self) -> None:
        nested_err: dict[str, object] = {
            "loc": ["items", 0, "price"],
            "msg": "Field required",
            "type": "missing",
            "input": {},
        }
        result = HandlerSchemaRepair().handle(
            _make_request(validation_errors=[nested_err])
        )
        assert "items" in result.error_summary
        assert "price" in result.error_summary


@pytest.mark.unit
class TestHandlerSchemaRepairPromptContent:
    def test_repair_prompt_includes_original_prompt(self) -> None:
        original = "Extract the name and score from the document."
        result = HandlerSchemaRepair().handle(_make_request(original_prompt=original))
        assert original in result.repair_prompt

    def test_repair_prompt_includes_malformed_output(self) -> None:
        malformed = '{"score": "not-a-number"}'
        result = HandlerSchemaRepair().handle(_make_request(malformed_output=malformed))
        assert malformed in result.repair_prompt

    def test_repair_prompt_escapes_embedded_fences(self) -> None:
        malformed = '{"text": "before ```json\\n{}\\n``` after"}'
        result = HandlerSchemaRepair().handle(_make_request(malformed_output=malformed))
        previous_response = result.repair_prompt.split(
            "## Your Previous (Invalid) Response", maxsplit=1
        )[1].split("## What Went Wrong", maxsplit=1)[0]

        assert "\\`\\`\\`json" in previous_response
        assert previous_response.count("```") == 2

    def test_repair_prompt_includes_schema_fields(self) -> None:
        result = HandlerSchemaRepair().handle(_make_request())
        assert "name" in result.repair_prompt
        assert "score" in result.repair_prompt

    def test_repair_prompt_includes_error_summary(self) -> None:
        result = HandlerSchemaRepair().handle(
            _make_request(validation_errors=[_TYPE_ERROR])
        )
        assert "float_parsing" in result.repair_prompt

    def test_repair_prompt_instructs_json_only(self) -> None:
        result = HandlerSchemaRepair().handle(_make_request())
        assert "JSON" in result.repair_prompt

    def test_repair_prompt_instructs_no_extra_fields(self) -> None:
        result = HandlerSchemaRepair().handle(_make_request())
        assert "not defined in the schema" in result.repair_prompt

    def test_repair_prompt_instructs_required_fields(self) -> None:
        result = HandlerSchemaRepair().handle(_make_request())
        assert (
            "required field" in result.repair_prompt.lower()
            or "every required" in result.repair_prompt
        )

    def test_repair_prompt_not_empty(self) -> None:
        result = HandlerSchemaRepair().handle(_make_request())
        assert len(result.repair_prompt) > 100


@pytest.mark.unit
class TestHandlerSchemaRepairDeterminism:
    def test_same_input_produces_identical_output(self) -> None:
        request = _make_request(run_id="det-run")
        handler = HandlerSchemaRepair()
        result_a = handler.handle(request)
        result_b = handler.handle(request)
        assert result_a.repair_prompt == result_b.repair_prompt
        assert result_a.error_summary == result_b.error_summary
        assert result_a.repairable == result_b.repairable

    def test_different_errors_produce_different_prompts(self) -> None:
        req_a = _make_request(validation_errors=[_TYPE_ERROR])
        req_b = _make_request(validation_errors=[_MISSING_ERROR])
        handler = HandlerSchemaRepair()
        assert (
            handler.handle(req_a).repair_prompt != handler.handle(req_b).repair_prompt
        )

    def test_different_malformed_outputs_produce_different_prompts(self) -> None:
        req_a = _make_request(malformed_output='{"score": "bad"}')
        req_b = _make_request(malformed_output='{"name": 42}')
        handler = HandlerSchemaRepair()
        assert (
            handler.handle(req_a).repair_prompt != handler.handle(req_b).repair_prompt
        )

    def test_different_schemas_produce_different_prompts(self) -> None:
        schema_b: dict[str, object] = {
            "type": "object",
            "properties": {"title": {"type": "string"}},
            "required": ["title"],
        }
        req_a = _make_request(target_schema=_SIMPLE_SCHEMA)
        req_b = _make_request(target_schema=schema_b)
        handler = HandlerSchemaRepair()
        assert (
            handler.handle(req_a).repair_prompt != handler.handle(req_b).repair_prompt
        )
