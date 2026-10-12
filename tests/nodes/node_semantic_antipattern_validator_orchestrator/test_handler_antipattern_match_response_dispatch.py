# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the orchestrator's second hop: match response -> classify command."""

from __future__ import annotations

import pytest

from omnimarket.models.antipattern import ModelAntipatternValidationContext
from omnimarket.nodes.node_semantic_antipattern_classifier_compute.models.model_antipattern_classify_request import (
    ModelAntipatternClassifyRequest,
)
from omnimarket.nodes.node_semantic_antipattern_validator_orchestrator.handlers.handler_antipattern_match_response_dispatch import (
    HandlerAntipatternMatchResponseDispatch,
)
from omnimarket.nodes.node_semantic_antipattern_validator_orchestrator.models.model_antipattern_match_result import (
    ModelAntipatternMatchCandidate,
    ModelAntipatternMatchResult,
)


def _context(**overrides: object) -> ModelAntipatternValidationContext:
    params: dict[str, object] = {
        "file_path": "src/foo.py",
        "line_count": 14,
        "similarity_threshold": 0.85,
        "enforcement_mode": "blocking",
    }
    params.update(overrides)
    return ModelAntipatternValidationContext.model_validate(params)


def _result(
    *,
    matches: tuple[ModelAntipatternMatchCandidate, ...] = (),
    error_message: str | None = None,
) -> ModelAntipatternMatchResult:
    return ModelAntipatternMatchResult(
        correlation_id="corr-1",
        matches=matches,
        error_message=error_message,
        validation_context=_context(),
    )


def _candidate(name: str = "god_object") -> ModelAntipatternMatchCandidate:
    return ModelAntipatternMatchCandidate(
        antipattern_name=name,
        similarity_score=0.91,
        enforcement="blocking",
        description="Class does too many unrelated things.",
    )


@pytest.mark.unit
def test_each_match_becomes_a_classifier_match_for_the_validated_file() -> None:
    command = HandlerAntipatternMatchResponseDispatch().handle(
        _result(matches=(_candidate(),))
    )

    (match,) = command.matches
    assert match.pattern_id == "god_object"
    assert match.label == "god_object"
    assert match.similarity == pytest.approx(0.91)
    assert match.enforcement == "blocking"
    assert match.file_path == "src/foo.py"
    assert match.line_count == 14


@pytest.mark.unit
def test_threshold_and_correlation_id_are_forwarded() -> None:
    command = HandlerAntipatternMatchResponseDispatch().handle(_result())

    assert command.config.similarity_threshold == pytest.approx(0.85)
    assert command.correlation_id == "corr-1"


@pytest.mark.unit
def test_empty_response_still_yields_a_classify_command() -> None:
    """No match, and a graceful skip, both reach the classifier so the validation
    ends on the terminal event like any other."""
    command = HandlerAntipatternMatchResponseDispatch().handle(_result())

    assert command.matches == ()


@pytest.mark.unit
def test_match_error_rides_on_the_context() -> None:
    command = HandlerAntipatternMatchResponseDispatch().handle(
        _result(error_message="Qdrant unavailable — no QDRANT_HOST configured.")
    )

    assert command.validation_context.match_error is not None
    assert "Qdrant unavailable" in command.validation_context.match_error
    assert command.validation_context.file_path == "src/foo.py"


@pytest.mark.unit
def test_command_is_accepted_by_the_classifier_request_model() -> None:
    """The classifier's request model refuses fields it does not declare."""
    command = HandlerAntipatternMatchResponseDispatch().handle(
        _result(matches=(_candidate(),))
    )

    request = ModelAntipatternClassifyRequest.model_validate(command.model_dump())

    assert request.correlation_id == "corr-1"
    assert request.matches[0].line_count == 14
    assert request.validation_context == command.validation_context
