# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the orchestrator's last hop: classification -> terminal event."""

from __future__ import annotations

import pytest

from omnimarket.models.antipattern import ModelAntipatternValidationContext
from omnimarket.nodes.node_semantic_antipattern_classifier_compute.models.model_antipattern_classify_result import (
    ModelAntipatternClassifyResult,
    ModelAntipatternViolation,
)
from omnimarket.nodes.node_semantic_antipattern_validator_orchestrator.handlers.handler_antipattern_classified_emit import (
    HandlerAntipatternClassifiedEmit,
)
from omnimarket.nodes.node_semantic_antipattern_validator_orchestrator.models.model_antipattern_classified_result import (
    ModelAntipatternClassifiedResult,
    ModelAntipatternClassifiedViolation,
)


def _context(match_error: str | None = None) -> ModelAntipatternValidationContext:
    return ModelAntipatternValidationContext(
        file_path="src/foo.py",
        line_count=14,
        similarity_threshold=0.8,
        enforcement_mode="advisory",
        match_error=match_error,
    )


def _violation(*, blocking: bool) -> ModelAntipatternClassifiedViolation:
    return ModelAntipatternClassifiedViolation(
        pattern_id="god_object",
        label="god_object",
        file_path="src/foo.py",
        similarity=0.9,
        is_blocking=blocking,
        explanation="god_object detected in src/foo.py",
    )


@pytest.mark.unit
def test_no_violations_is_a_clean_terminal_event() -> None:
    event = HandlerAntipatternClassifiedEmit().handle(
        ModelAntipatternClassifiedResult(
            correlation_id="corr-2", validation_context=_context()
        )
    )

    assert event.correlation_id == "corr-2"
    assert event.file_path == "src/foo.py"
    assert event.enforcement_mode == "advisory"
    assert event.violation_count == 0
    assert event.has_blocking_violation is False
    assert event.match_error is None


@pytest.mark.unit
def test_blocking_violation_sets_the_blocking_flag() -> None:
    event = HandlerAntipatternClassifiedEmit().handle(
        ModelAntipatternClassifiedResult(
            correlation_id="corr-3",
            violations=(_violation(blocking=False), _violation(blocking=True)),
            validation_context=_context(),
        )
    )

    assert event.violation_count == 2
    assert event.has_blocking_violation is True


@pytest.mark.unit
def test_advisory_only_violations_do_not_block() -> None:
    event = HandlerAntipatternClassifiedEmit().handle(
        ModelAntipatternClassifiedResult(
            correlation_id="corr-4",
            violations=(_violation(blocking=False),),
            validation_context=_context(),
        )
    )

    assert event.violation_count == 1
    assert event.has_blocking_violation is False


@pytest.mark.unit
def test_match_error_is_surfaced_so_a_skip_is_not_read_as_clean() -> None:
    event = HandlerAntipatternClassifiedEmit().handle(
        ModelAntipatternClassifiedResult(
            correlation_id="corr-5",
            validation_context=_context("Qdrant search failed."),
        )
    )

    assert event.violation_count == 0
    assert event.match_error == "Qdrant search failed."


@pytest.mark.unit
def test_classifier_result_wire_form_is_readable() -> None:
    """The classifier's own result, serialised, validates as this hop's input."""
    produced = ModelAntipatternClassifyResult(
        violations=(
            ModelAntipatternViolation(
                pattern_id="god_object",
                label="god_object",
                file_path="src/foo.py",
                similarity=0.9,
                is_blocking=True,
                explanation="x",
            ),
        ),
        correlation_id="corr-6",
        validation_context=_context(),
    )

    consumed = ModelAntipatternClassifiedResult.model_validate(produced.model_dump())

    assert consumed.correlation_id == "corr-6"
    assert consumed.violations[0].is_blocking is True
