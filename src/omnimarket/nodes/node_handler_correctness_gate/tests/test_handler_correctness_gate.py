# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

import pytest

from omnimarket.nodes.node_handler_correctness_gate.handlers.handler_correctness_gate import (
    HandlerCorrectnessGate,
)
from omnimarket.nodes.node_handler_correctness_gate.models.enums import (
    EnumScoringMethod,
)
from omnimarket.nodes.node_handler_correctness_gate.models.model_correctness_check_request import (
    ModelCorrectnessCheckRequest,
)
from omnimarket.nodes.node_handler_correctness_gate.models.model_eval_entry import (
    ModelEvalEntry,
)
from omnimarket.nodes.node_handler_correctness_gate.models.model_eval_set import (
    ModelEvalSet,
)


def _make_request(
    entries: list[ModelEvalEntry],
    actual_outputs: list[str],
    min_score: float = 0.85,
    name: str = "test-eval",
) -> ModelCorrectnessCheckRequest:
    return ModelCorrectnessCheckRequest(
        handler_id="handler-abc",
        eval_set=ModelEvalSet(
            entries=tuple(entries),
            min_score=min_score,
            name=name,
        ),
        actual_outputs=tuple(actual_outputs),
        correlation_id="corr-001",
    )


def _entry(
    expected: str,
    scoring: EnumScoringMethod = EnumScoringMethod.EXACT_MATCH,
) -> ModelEvalEntry:
    return ModelEvalEntry(input="input", expected=expected, scoring=scoring)


# ---------------------------------------------------------------------------
# All correct
# ---------------------------------------------------------------------------


def test_all_correct_score_one_passed() -> None:
    entries = [_entry("positive"), _entry("negative"), _entry("neutral")]
    request = _make_request(entries, ["positive", "negative", "neutral"])
    result = HandlerCorrectnessGate().handle(request)

    assert result.score == 1.0
    assert result.passed is True
    assert result.correct_entries == 3
    assert result.total_entries == 3
    assert result.failures == ()


# ---------------------------------------------------------------------------
# Partial correct
# ---------------------------------------------------------------------------


def test_partial_correct_score_and_failures_listed() -> None:
    entries = [_entry("positive"), _entry("negative"), _entry("neutral")]
    request = _make_request(entries, ["positive", "WRONG", "neutral"], min_score=0.85)
    result = HandlerCorrectnessGate().handle(request)

    assert result.score == pytest.approx(2 / 3)
    assert result.correct_entries == 2
    assert len(result.failures) == 1
    failure = result.failures[0]
    assert failure.entry_index == 1
    assert failure.expected == "negative"
    assert failure.actual == "WRONG"


# ---------------------------------------------------------------------------
# Below min_score → passed=False
# ---------------------------------------------------------------------------


def test_below_min_score_passed_false() -> None:
    entries = [_entry("a"), _entry("b"), _entry("c"), _entry("d")]
    request = _make_request(entries, ["a", "WRONG", "WRONG", "WRONG"], min_score=0.85)
    result = HandlerCorrectnessGate().handle(request)

    assert result.score == pytest.approx(0.25)
    assert result.passed is False


def test_at_min_score_boundary_passed_true() -> None:
    entries = [_entry("a"), _entry("b"), _entry("c"), _entry("d")]
    # 3/4 = 0.75; with min_score=0.75, should pass
    request = _make_request(entries, ["a", "b", "c", "WRONG"], min_score=0.75)
    result = HandlerCorrectnessGate().handle(request)

    assert result.score == pytest.approx(0.75)
    assert result.passed is True


# ---------------------------------------------------------------------------
# Scoring methods
# ---------------------------------------------------------------------------


def test_exact_match_scoring() -> None:
    entries = [
        _entry("hello", EnumScoringMethod.EXACT_MATCH),
        _entry("hello", EnumScoringMethod.EXACT_MATCH),
    ]
    request = _make_request(entries, ["hello", "hello world"], min_score=0.5)
    result = HandlerCorrectnessGate().handle(request)

    assert result.correct_entries == 1
    assert result.failures[0].actual == "hello world"


def test_contains_scoring() -> None:
    entries = [
        _entry("positive", EnumScoringMethod.CONTAINS),
        _entry("negative", EnumScoringMethod.CONTAINS),
    ]
    request = _make_request(
        entries, ["very positive sentiment", "clearly negative"], min_score=0.5
    )
    result = HandlerCorrectnessGate().handle(request)

    assert result.score == 1.0
    assert result.passed is True


def test_contains_scoring_miss() -> None:
    entries = [_entry("positive", EnumScoringMethod.CONTAINS)]
    request = _make_request(entries, ["negative sentiment"], min_score=0.5)
    result = HandlerCorrectnessGate().handle(request)

    assert result.correct_entries == 0
    assert result.passed is False


def test_starts_with_scoring() -> None:
    entries = [
        _entry("pos", EnumScoringMethod.STARTS_WITH),
        _entry("neg", EnumScoringMethod.STARTS_WITH),
    ]
    request = _make_request(entries, ["positive", "neutral"], min_score=0.5)
    result = HandlerCorrectnessGate().handle(request)

    assert result.correct_entries == 1
    assert result.failures[0].entry_index == 1


def test_starts_with_scoring_match() -> None:
    entries = [_entry("neg", EnumScoringMethod.STARTS_WITH)]
    request = _make_request(entries, ["negative"], min_score=0.5)
    result = HandlerCorrectnessGate().handle(request)

    assert result.score == 1.0
    assert result.passed is True


# ---------------------------------------------------------------------------
# Empty eval set
# ---------------------------------------------------------------------------


def test_empty_eval_set_score_zero_not_passed() -> None:
    request = _make_request([], [], min_score=0.0)
    result = HandlerCorrectnessGate().handle(request)

    assert result.score == 0.0
    assert result.passed is False
    assert result.total_entries == 0
    assert result.correct_entries == 0


# ---------------------------------------------------------------------------
# Metadata propagation
# ---------------------------------------------------------------------------


def test_eval_set_name_propagated() -> None:
    entries = [_entry("yes")]
    request = _make_request(entries, ["yes"], name="my-eval-set")
    result = HandlerCorrectnessGate().handle(request)

    assert result.eval_set_name == "my-eval-set"
    assert result.handler_id == "handler-abc"


# ---------------------------------------------------------------------------
# Fewer actual_outputs than entries (defensive)
# ---------------------------------------------------------------------------


def test_missing_actual_output_treated_as_empty_string() -> None:
    entries = [_entry("a"), _entry("b")]
    request = _make_request(entries, ["a"], min_score=0.5)
    result = HandlerCorrectnessGate().handle(request)

    assert result.correct_entries == 1
    assert result.failures[0].actual == ""
