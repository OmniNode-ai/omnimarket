# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19401: causal connectives satisfy the step-by-step explanation check."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    _HEURISTIC_CONTAINS_ANY_CHECKS,
    _check_contains_any,
)

pytestmark = pytest.mark.unit

_LITERAL_MARKERS = ("step", "1.", "first", "then")


@pytest.mark.parametrize(
    ("connective", "content"),
    [
        ("because", "The lookup is fast because the hash narrows the search."),
        ("therefore", "The hash narrows the search; therefore the lookup is fast."),
        (
            "consequently",
            "The hash narrows the search; consequently the lookup is fast.",
        ),
        ("since", "The lookup is fast since the hash narrows the search."),
        (
            "as a result",
            "The hash narrows the search; as a result the lookup is fast.",
        ),
        (
            "this means",
            "The hash narrows the search; this means the lookup is fast.",
        ),
    ],
)
def test_step_by_step_explanation_accepts_causal_connective(
    connective: str, content: str
) -> None:
    category, markers = _HEURISTIC_CONTAINS_ANY_CHECKS["step_by_step_explanation"]
    lowered = content.lower()
    assert all(
        marker not in lowered for marker in markers if marker in _LITERAL_MARKERS
    )
    assert tuple(marker for marker in markers if marker in lowered) == (connective,)
    assert (
        _check_contains_any(
            content,
            check_name="step_by_step_explanation",
            category=category,
            markers=markers,
        )
        is None
    )


def test_step_by_step_explanation_preserves_literal_markers() -> None:
    _, markers = _HEURISTIC_CONTAINS_ANY_CHECKS["step_by_step_explanation"]
    assert all(marker in markers for marker in _LITERAL_MARKERS)


def test_step_by_step_explanation_rejects_content_without_markers() -> None:
    category, markers = _HEURISTIC_CONTAINS_ANY_CHECKS["step_by_step_explanation"]
    content = "The lookup retrieves the stored value."
    assert all(marker not in content.lower() for marker in markers)
    assert (
        _check_contains_any(
            content,
            check_name="step_by_step_explanation",
            category=category,
            markers=markers,
        )
        == "TASK_MISMATCH: failed step_by_step_explanation"
    )
