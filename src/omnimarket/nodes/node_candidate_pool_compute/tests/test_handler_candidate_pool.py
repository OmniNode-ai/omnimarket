"""Tests for HandlerCandidatePool — deterministic candidate pool scoring."""

from __future__ import annotations

import json

import pytest

from omnimarket.nodes.node_candidate_pool_compute.handlers.handler_candidate_pool import (
    HandlerCandidatePool,
    _count_loc,
    _fitness,
    _validate_schema,
)
from omnimarket.nodes.node_candidate_pool_compute.models.model_pool_request import (
    ModelCandidatePoolRequest,
)
from omnimarket.nodes.node_candidate_pool_compute.models.model_pool_result import (
    EnumPoolStatus,
)

_OBJECT_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["name", "value"],
    "properties": {
        "name": {"type": "string"},
        "value": {"type": "integer"},
    },
}

_VALID_JSON = json.dumps({"name": "alpha", "value": 1})
_INVALID_JSON = json.dumps({"name": "beta"})  # missing "value"
_NOT_JSON = "def foo():\n    return 42\n"


def _req(
    candidates: list[str],
    *,
    schema: dict[str, object] | None = None,
    max_loc: int = 20,
    min_candidates: int = 1,
    run_id: str = "run-test",
) -> ModelCandidatePoolRequest:
    return ModelCandidatePoolRequest(
        candidates=candidates,
        target_schema=schema if schema is not None else _OBJECT_SCHEMA,
        max_loc=max_loc,
        min_candidates=min_candidates,
        run_id=run_id,
    )


@pytest.mark.unit
class TestCountLoc:
    def test_empty_string(self) -> None:
        assert _count_loc("") == 0

    def test_blank_lines_excluded(self) -> None:
        assert _count_loc("\n\n\n") == 0

    def test_counts_non_blank_lines(self) -> None:
        assert _count_loc("a\nb\nc") == 3

    def test_mixed_blank_and_content(self) -> None:
        assert _count_loc("a\n\nb") == 2


@pytest.mark.unit
class TestValidateSchema:
    def test_valid_object(self) -> None:
        assert _validate_schema(_VALID_JSON, _OBJECT_SCHEMA) is True

    def test_missing_required_field(self) -> None:
        assert _validate_schema(_INVALID_JSON, _OBJECT_SCHEMA) is False

    def test_not_json(self) -> None:
        assert _validate_schema(_NOT_JSON, _OBJECT_SCHEMA) is False

    def test_wrong_root_type(self) -> None:
        assert _validate_schema(json.dumps([1, 2, 3]), _OBJECT_SCHEMA) is False

    def test_empty_schema_accepts_any_json(self) -> None:
        assert _validate_schema(_VALID_JSON, {}) is True

    def test_array_schema_rejects_object(self) -> None:
        array_schema: dict[str, object] = {"type": "array"}
        assert _validate_schema(_VALID_JSON, array_schema) is False

    def test_array_schema_accepts_array(self) -> None:
        array_schema: dict[str, object] = {"type": "array"}
        assert _validate_schema(json.dumps([1, 2]), array_schema) is True


@pytest.mark.unit
class TestFitness:
    def test_valid_zero_loc(self) -> None:
        score = _fitness(schema_valid=True, loc=0, max_loc=10)
        assert score == pytest.approx(1.0)

    def test_invalid_zero_loc(self) -> None:
        score = _fitness(schema_valid=False, loc=0, max_loc=10)
        assert score == pytest.approx(0.3)

    def test_valid_at_budget(self) -> None:
        score = _fitness(schema_valid=True, loc=10, max_loc=10)
        assert score == pytest.approx(0.7)

    def test_valid_over_budget_clamped(self) -> None:
        score = _fitness(schema_valid=True, loc=100, max_loc=10)
        assert score == pytest.approx(0.7)

    def test_invalid_over_budget(self) -> None:
        score = _fitness(schema_valid=False, loc=100, max_loc=10)
        assert score == pytest.approx(0.0)


@pytest.mark.unit
class TestHandlerCandidatePoolStatus:
    def test_ok_status_on_success(self) -> None:
        result = HandlerCandidatePool().handle(_req([_VALID_JSON]))
        assert result.status == EnumPoolStatus.OK

    def test_error_on_insufficient_candidates(self) -> None:
        result = HandlerCandidatePool().handle(_req([_VALID_JSON], min_candidates=3))
        assert result.status == EnumPoolStatus.ERROR
        assert result.error is not None
        assert result.best_candidate_index == -1
        assert result.ranked_candidates == []

    def test_run_id_propagated(self) -> None:
        result = HandlerCandidatePool().handle(_req([_VALID_JSON], run_id="run-xyz"))
        assert result.run_id == "run-xyz"


@pytest.mark.unit
class TestHandlerCandidatePoolScoring:
    def test_all_valid_flag_true_when_all_pass(self) -> None:
        result = HandlerCandidatePool().handle(
            _req([_VALID_JSON, json.dumps({"name": "b", "value": 2})])
        )
        assert result.all_valid is True

    def test_all_valid_flag_false_when_any_fail(self) -> None:
        result = HandlerCandidatePool().handle(_req([_VALID_JSON, _INVALID_JSON]))
        assert result.all_valid is False

    def test_valid_candidate_ranked_above_invalid(self) -> None:
        result = HandlerCandidatePool().handle(_req([_INVALID_JSON, _VALID_JSON]))
        assert result.ranked_candidates[0].schema_valid is True

    def test_best_candidate_index_points_to_original(self) -> None:
        # _INVALID_JSON first, _VALID_JSON second (original_index=1)
        result = HandlerCandidatePool().handle(_req([_INVALID_JSON, _VALID_JSON]))
        assert result.best_candidate_index == 1

    def test_compact_candidate_ranked_above_verbose_when_both_valid(self) -> None:
        compact = json.dumps({"name": "a", "value": 1})
        verbose = "\n".join(
            [json.dumps({"name": "b", "value": 2})] + ["# comment"] * 50
        )
        result = HandlerCandidatePool().handle(_req([verbose, compact], max_loc=10))
        assert result.ranked_candidates[0].original_index == 1

    def test_within_budget_flag(self) -> None:
        result = HandlerCandidatePool().handle(_req([_VALID_JSON], max_loc=1000))
        assert result.ranked_candidates[0].within_budget is True

    def test_over_budget_flag(self) -> None:
        result = HandlerCandidatePool().handle(_req([_VALID_JSON], max_loc=0))
        assert result.ranked_candidates[0].within_budget is False

    def test_loc_counted_correctly(self) -> None:
        three_line = "a\nb\nc"
        result = HandlerCandidatePool().handle(_req([three_line], schema={}))
        assert result.ranked_candidates[0].loc == 3

    def test_summary_contains_valid_count(self) -> None:
        result = HandlerCandidatePool().handle(_req([_VALID_JSON, _INVALID_JSON]))
        assert "1/2" in result.summary

    def test_no_error_field_on_success(self) -> None:
        result = HandlerCandidatePool().handle(_req([_VALID_JSON]))
        assert result.error is None


@pytest.mark.unit
class TestHandlerCandidatePoolDeterminism:
    def test_same_input_produces_identical_output(self) -> None:
        request = _req([_VALID_JSON, _INVALID_JSON, _NOT_JSON])
        handler = HandlerCandidatePool()
        assert handler.handle(request) == handler.handle(request)

    def test_different_candidates_produce_different_output(self) -> None:
        req_a = _req([_VALID_JSON])
        req_b = _req([_INVALID_JSON])
        handler = HandlerCandidatePool()
        assert handler.handle(req_a) != handler.handle(req_b)
