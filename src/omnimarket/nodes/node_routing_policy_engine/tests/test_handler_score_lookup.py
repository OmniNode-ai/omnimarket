# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for build_available_models_from_scores bridge function."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_routing_policy_engine.handlers.handler_score_lookup import (
    build_available_models_from_scores,
)


@pytest.mark.unit
def test_build_available_models_from_capability_scores() -> None:
    """Two rows with different success_rates produce correct ModelAvailableModel instances."""
    rows: list[dict[str, object]] = [
        {"model_key": "qwen3-coder", "success_rate": 0.92},
        {"model_key": "deepseek-r1", "success_rate": 0.75},
    ]
    cost_map = {"qwen3-coder": 0.001, "deepseek-r1": 0.002}

    result = build_available_models_from_scores(rows, cost_map)

    assert len(result) == 2

    first = result[0]
    assert first.key == "qwen3-coder"
    assert first.score == pytest.approx(0.92)
    assert first.cost_per_token == pytest.approx(0.001)

    second = result[1]
    assert second.key == "deepseek-r1"
    assert second.score == pytest.approx(0.75)
    assert second.cost_per_token == pytest.approx(0.002)


@pytest.mark.unit
def test_build_available_models_empty_scores_returns_empty() -> None:
    """Empty input rows and cost_map produce an empty list."""
    result = build_available_models_from_scores([], {})
    assert result == []
