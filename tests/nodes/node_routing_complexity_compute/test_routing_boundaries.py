# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Boundary and fail-closed tests for trusted routing complexity."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_routing_complexity_compute.handlers.handler_complexity_scoring import (
    _band_points,
)
from omnimarket.nodes.node_routing_complexity_compute.handlers.handler_routing_complexity import (
    HandlerRoutingComplexity,
)
from omnimarket.nodes.node_routing_complexity_compute.models.model_routing_complexity import (
    ModelRoutingClassification,
    ModelRoutingRefusal,
    ModelRoutingRequest,
    ModelTrustedComplexityClassification,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def handler() -> HandlerRoutingComplexity:
    return HandlerRoutingComplexity()


@pytest.mark.parametrize(
    ("workflow", "prompt", "score", "rung"),
    [
        ("summarization", "then then", 2, "R1"),
        ("summarization", "then then then", 3, "R2"),
        ("planning", "then then then", 5, "R2"),
        ("planning", "DATA:\nthen then then", 6, "R3"),
        ("test", "", 7, "R3"),
        ("test", "then", 8, "R3b"),
        ("test", "then then", 9, "R3b"),
        ("test", "then then then", 10, "R4"),
        ("refactor", "DATA:\nthen then then", 13, "R4"),
        ("refactor", "A:\nB:\nC:\nthen then then", 14, "R5"),
        ("refactor", "```python\npass\n```\nthen then then", 15, "R5"),
        (
            "refactor",
            "CATALOGUE:\n```python\npass\n```\nthen then then",
            16,
            "R6",
        ),
    ],
)
def test_each_rung_boundary_is_inclusive(
    handler: HandlerRoutingComplexity,
    workflow: str,
    prompt: str,
    score: int,
    rung: str,
) -> None:
    result = handler.handle(ModelRoutingRequest(prompt=prompt, workflow=workflow))
    assert isinstance(result, ModelRoutingClassification)
    assert result.classification.score == score
    assert result.classification.rung == rung
    assert sum(result.classification.points.values()) == score


@pytest.mark.parametrize(
    ("characters", "tokens", "points"),
    [
        (0, 0, 0),
        (1, 1, 0),
        (3996, 999, 0),
        (3997, 1000, 1),
        (4000, 1000, 1),
        (15996, 3999, 1),
        (15997, 4000, 2),
        (16000, 4000, 2),
        (39996, 9999, 2),
        (39997, 10000, 3),
        (40000, 10000, 3),
        (400000, 100000, 3),
    ],
)
def test_prompt_length_rounds_up_and_uses_the_tail_band(
    handler: HandlerRoutingComplexity, characters: int, tokens: int, points: int
) -> None:
    result = handler.handle(
        ModelRoutingRequest(prompt="x" * characters, workflow="summarization")
    )
    assert isinstance(result, ModelRoutingClassification)
    classification = result.classification
    assert classification.features.input_tokens_estimate == tokens
    assert classification.features.distinct_sources == 1
    assert classification.features.dependent_reasoning_steps == 1
    assert classification.points["input_tokens_estimate"] == points
    assert classification.score == points


@pytest.mark.parametrize(
    ("prompt", "sources", "points"),
    [
        ("plain text", 1, 0),
        ("DATA:\nplain text", 2, 1),
        ("DATA:\n```text\nplain text\n```", 3, 1),
        ("A:\nB:\nC:\nplain text", 4, 2),
    ],
)
def test_source_count_thresholds(
    handler: HandlerRoutingComplexity, prompt: str, sources: int, points: int
) -> None:
    result = handler.handle(
        ModelRoutingRequest(prompt=prompt, workflow="summarization")
    )
    assert isinstance(result, ModelRoutingClassification)
    assert result.classification.features.distinct_sources == sources
    assert result.classification.points["distinct_sources"] == points
    assert result.classification.score == points


def test_dependency_points_are_capped_without_losing_measured_steps(
    handler: HandlerRoutingComplexity,
) -> None:
    result = handler.handle(
        ModelRoutingRequest(prompt="then " * 100, workflow="summarization")
    )
    assert isinstance(result, ModelRoutingClassification)
    assert result.classification.features.dependent_reasoning_steps == 101
    assert result.classification.points["dependent_reasoning_steps"] == 3


def test_incomplete_band_table_refuses_an_uncovered_value() -> None:
    with pytest.raises(ValueError, match="bands do not cover this value"):
        _band_points(1000, [{"below": 1000, "points": 0}])


@pytest.mark.parametrize("workflow", ["unknown", "code_review", "agent_delegation"])
@pytest.mark.parametrize("supplied", [False, True])
def test_unavailable_workflow_refuses_without_diagnostic_classification(
    handler: HandlerRoutingComplexity, workflow: str, supplied: bool
) -> None:
    payload: dict[str, object] = {"prompt": "", "workflow": workflow}
    if supplied:
        payload["scorer"] = None
    result = handler.handle(ModelRoutingRequest.model_validate(payload))
    assert isinstance(result, ModelRoutingRefusal)
    assert result.reason == (
        "rubric_input_supplied" if supplied else "workflow_contract_unavailable"
    )
    assert result.fields == (("scorer",) if supplied else ("workflow",))
    assert result.classification is None


def test_rubric_override_refuses_but_preserves_trusted_score(
    handler: HandlerRoutingComplexity,
) -> None:
    accepted = handler.handle(ModelRoutingRequest(prompt="", workflow="summarization"))
    refused = handler.handle(
        ModelRoutingRequest(prompt="", workflow="summarization", output_kind="patch")
    )
    assert isinstance(accepted, ModelRoutingClassification)
    assert isinstance(refused, ModelRoutingRefusal)
    assert refused.reason == "rubric_input_supplied"
    assert refused.fields == ("output_kind",)
    assert refused.classification == accepted.classification
    assert refused.classification.score == 0


@pytest.mark.parametrize("invalid", ["missing_origin", "wrong_feature_split"])
def test_classification_rejects_incomplete_or_inconsistent_provenance(
    handler: HandlerRoutingComplexity, invalid: str
) -> None:
    result = handler.handle(ModelRoutingRequest(prompt="", workflow="summarization"))
    assert isinstance(result, ModelRoutingClassification)
    payload = result.classification.model_dump()
    if invalid == "missing_origin":
        del payload["provenance"]["output_kind"]
        message = "every routing feature must carry trusted provenance"
    else:
        payload["features"]["measured"] = ()
        message = "feature provenance must match the measured/declared split"
    with pytest.raises(ValidationError, match=message):
        ModelTrustedComplexityClassification.model_validate(payload)
