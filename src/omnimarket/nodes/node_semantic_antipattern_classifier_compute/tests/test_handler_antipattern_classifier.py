# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD tests for node_semantic_antipattern_classifier_compute.

Verifies:
- Pure determinism: same input always produces same output
- Blocking violations require similarity >= threshold + enforcement=blocking
- Files under 10 lines always return empty violations
- Non-blocking enforcement yields advisory violations, not blocking
- Explanation is present when violation fires
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_semantic_antipattern_classifier_compute.handlers.handler_antipattern_classifier import (
    HandlerAntipatternClassifier,
)
from omnimarket.nodes.node_semantic_antipattern_classifier_compute.models.model_antipattern_classify_request import (
    ModelAntipatternClassifyRequest,
    ModelAntipatternMatch,
    ModelAntipatternMatchConfig,
)
from omnimarket.nodes.node_semantic_antipattern_classifier_compute.models.model_antipattern_classify_result import (
    ModelAntipatternClassifyResult,
    ModelAntipatternViolation,
)


def _make_match(
    pattern_id: str = "god-class",
    label: str = "God Class",
    similarity: float = 0.90,
    enforcement: str = "blocking",
    description: str = "Class does too many things",
    file_path: str = "src/foo.py",
    line_count: int = 50,
) -> ModelAntipatternMatch:
    return ModelAntipatternMatch(
        pattern_id=pattern_id,
        label=label,
        similarity=similarity,
        enforcement=enforcement,
        description=description,
        file_path=file_path,
        line_count=line_count,
    )


def _make_request(
    matches: tuple[ModelAntipatternMatch, ...] = (),
    similarity_threshold: float = 0.80,
) -> ModelAntipatternClassifyRequest:
    return ModelAntipatternClassifyRequest(
        matches=matches,
        config=ModelAntipatternMatchConfig(similarity_threshold=similarity_threshold),
    )


@pytest.mark.unit
def test_determinism_same_input_same_output() -> None:
    handler = HandlerAntipatternClassifier()
    match = _make_match(similarity=0.85, enforcement="blocking")
    req = _make_request(matches=(match,))

    result1 = handler.handle(req)
    result2 = handler.handle(req)

    assert result1 == result2


@pytest.mark.unit
def test_no_matches_returns_empty_violations() -> None:
    handler = HandlerAntipatternClassifier()
    req = _make_request(matches=())
    result = handler.handle(req)

    assert isinstance(result, ModelAntipatternClassifyResult)
    assert result.violations == ()
    assert not result.has_blocking_violation


@pytest.mark.unit
def test_blocking_violation_above_threshold() -> None:
    handler = HandlerAntipatternClassifier()
    match = _make_match(similarity=0.90, enforcement="blocking")
    req = _make_request(matches=(match,), similarity_threshold=0.80)
    result = handler.handle(req)

    assert len(result.violations) == 1
    violation = result.violations[0]
    assert isinstance(violation, ModelAntipatternViolation)
    assert violation.is_blocking
    assert violation.explanation
    assert result.has_blocking_violation


@pytest.mark.unit
def test_similarity_below_threshold_yields_no_violation() -> None:
    handler = HandlerAntipatternClassifier()
    match = _make_match(similarity=0.70, enforcement="blocking")
    req = _make_request(matches=(match,), similarity_threshold=0.80)
    result = handler.handle(req)

    assert result.violations == ()
    assert not result.has_blocking_violation


@pytest.mark.unit
def test_similarity_at_threshold_yields_violation() -> None:
    """Boundary: exactly at threshold must trigger."""
    handler = HandlerAntipatternClassifier()
    match = _make_match(similarity=0.80, enforcement="blocking")
    req = _make_request(matches=(match,), similarity_threshold=0.80)
    result = handler.handle(req)

    assert len(result.violations) == 1
    assert result.violations[0].is_blocking


@pytest.mark.unit
def test_advisory_enforcement_not_blocking() -> None:
    handler = HandlerAntipatternClassifier()
    match = _make_match(similarity=0.95, enforcement="advisory")
    req = _make_request(matches=(match,), similarity_threshold=0.80)
    result = handler.handle(req)

    assert len(result.violations) == 1
    violation = result.violations[0]
    assert not violation.is_blocking
    assert not result.has_blocking_violation


@pytest.mark.unit
def test_file_under_10_lines_returns_empty_violations() -> None:
    handler = HandlerAntipatternClassifier()
    match = _make_match(similarity=0.99, enforcement="blocking", line_count=9)
    req = _make_request(matches=(match,), similarity_threshold=0.80)
    result = handler.handle(req)

    assert result.violations == ()
    assert not result.has_blocking_violation


@pytest.mark.unit
def test_file_exactly_10_lines_is_not_exempt() -> None:
    """10 lines is not under 10; must be evaluated normally."""
    handler = HandlerAntipatternClassifier()
    match = _make_match(similarity=0.90, enforcement="blocking", line_count=10)
    req = _make_request(matches=(match,), similarity_threshold=0.80)
    result = handler.handle(req)

    assert len(result.violations) == 1


@pytest.mark.unit
def test_multiple_matches_mixed_results() -> None:
    handler = HandlerAntipatternClassifier()
    blocking_match = _make_match(
        pattern_id="god-class",
        similarity=0.90,
        enforcement="blocking",
        file_path="src/a.py",
        line_count=50,
    )
    advisory_match = _make_match(
        pattern_id="long-method",
        label="Long Method",
        similarity=0.85,
        enforcement="advisory",
        file_path="src/b.py",
        line_count=100,
    )
    below_threshold = _make_match(
        pattern_id="feature-envy",
        label="Feature Envy",
        similarity=0.60,
        enforcement="blocking",
        file_path="src/c.py",
        line_count=30,
    )
    short_file = _make_match(
        pattern_id="data-clumps",
        label="Data Clumps",
        similarity=0.95,
        enforcement="blocking",
        file_path="src/d.py",
        line_count=5,
    )

    req = _make_request(
        matches=(blocking_match, advisory_match, below_threshold, short_file),
        similarity_threshold=0.80,
    )
    result = handler.handle(req)

    assert len(result.violations) == 2
    assert result.has_blocking_violation
    blocking_violations = [v for v in result.violations if v.is_blocking]
    advisory_violations = [v for v in result.violations if not v.is_blocking]
    assert len(blocking_violations) == 1
    assert len(advisory_violations) == 1


@pytest.mark.unit
def test_explanation_present_on_violation() -> None:
    handler = HandlerAntipatternClassifier()
    match = _make_match(
        label="God Class",
        similarity=0.92,
        enforcement="blocking",
        description="Class does too many things",
    )
    req = _make_request(matches=(match,))
    result = handler.handle(req)

    assert result.violations
    assert result.violations[0].explanation
    assert "God Class" in result.violations[0].explanation


@pytest.mark.unit
def test_handler_importable() -> None:
    assert HandlerAntipatternClassifier.__name__ == "HandlerAntipatternClassifier"
