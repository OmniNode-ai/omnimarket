# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13370: marker heuristics are reject-only, not routing authority."""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)


def _gate(
    content: str,
    *,
    deterministic: tuple[str, ...] = (),
    heuristic: tuple[str, ...] = (),
    expected_markers: tuple[str, ...] = (),
    task_type: str = "research",
) -> ModelQualityGateResult:
    return quality_gate_delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type=task_type,
            llm_response_content=content,
            expected_markers=expected_markers,
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
        )
    )


@pytest.mark.unit
def test_schema_length_no_refusal_and_markers_cannot_create_pass() -> None:
    result = _gate(
        (
            "According to Theorem 2, the bound follows from the cited construction. "
            "Therefore the evidence matches the requested claim."
        ),
        deterministic=(
            "response_non_empty",
            "plain_text_only",
            "exactly_two_sentences",
        ),
        heuristic=(
            "no_refusal",
            "min_length_chars_20",
            "cites_sources",
            "methodical_analysis",
        ),
    )

    assert result.passed is False
    assert result.fail_category == "fail_heuristic"
    assert result.quality_score == pytest.approx(1.0)
    assert result.fallback_recommended is True
    assert result.failure_reasons == (
        "TASK_MISMATCH: no deterministic acceptance or judge adequacy authority; "
        "schema/length/no-refusal/marker checks are reject-only",
    )


@pytest.mark.unit
def test_no_refusal_prefilter_rejects_refusal_but_cannot_pass_clean_output() -> None:
    refusal = _gate("I cannot help with this.", heuristic=("no_refusal",))
    assert refusal.passed is False
    assert any(reason.startswith("REFUSAL") for reason in refusal.failure_reasons)

    clean = _gate("This is a clean non-refusal response.", heuristic=("no_refusal",))
    assert clean.passed is False
    assert clean.quality_score == pytest.approx(1.0)
    assert any("reject-only" in reason for reason in clean.failure_reasons)


@pytest.mark.unit
def test_length_prefilter_rejects_short_output_but_cannot_pass_long_output() -> None:
    short = _gate("short", heuristic=("min_length_chars_40",))
    assert short.passed is False
    assert any(reason.startswith("WEAK_OUTPUT") for reason in short.failure_reasons)

    long_enough = _gate(
        "This response is long enough to pass the explicit length pre-filter.",
        heuristic=("min_length_chars_40",),
    )
    assert long_enough.passed is False
    assert long_enough.quality_score == pytest.approx(1.0)
    assert any("reject-only" in reason for reason in long_enough.failure_reasons)


@pytest.mark.unit
def test_marker_prefilter_rejects_missing_marker_but_cannot_pass_marker_rich_output() -> (
    None
):
    missing = _gate(
        "The function returns a value.",
        heuristic=("cites_specific_lines",),
        task_type="review",
    )
    assert missing.passed is False
    assert any(
        "specific line citations" in reason for reason in missing.failure_reasons
    )

    marker_rich = _gate(
        "Line 42 has the risk because it mutates shared state.",
        heuristic=("cites_specific_lines", "explains_tradeoffs"),
        task_type="review",
    )
    assert marker_rich.passed is False
    assert marker_rich.quality_score == pytest.approx(1.0)
    assert any("reject-only" in reason for reason in marker_rich.failure_reasons)


@pytest.mark.unit
def test_semantic_adequacy_can_authorize_when_marker_prefilters_are_clean() -> None:
    result = _gate(
        (
            "According to Theorem 2, the compactness bound follows from the cited "
            "finite-rank approximation. Therefore the evidence supports the requested claim."
        ),
        deterministic=("response_non_empty",),
        heuristic=(
            "no_refusal",
            "cites_sources",
            "methodical_analysis",
            "semantic_adequacy",
        ),
    )

    assert result.passed is True
    assert result.fail_category == "pass"
    assert result.quality_score == pytest.approx(1.0)
    assert result.failure_reasons == ()


@pytest.mark.unit
def test_legacy_marker_fallback_is_reject_only() -> None:
    result = quality_gate_delta(
        ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type="test",
            llm_response_content=(
                "def test_example():\n"
                "    assert True\n"
                "# @pytest.mark keeps the legacy marker score high\n"
            ),
        )
    )

    assert result.passed is False
    assert result.quality_score >= 0.6
    assert any("reject-only" in reason for reason in result.failure_reasons)
