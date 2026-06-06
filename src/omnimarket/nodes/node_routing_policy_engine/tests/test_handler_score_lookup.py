# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for build_available_models_from_scores."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_routing_policy_engine.handlers.handler_score_lookup import (
    build_available_models_from_scores,
)
from omnimarket.nodes.node_routing_policy_engine.models.model_routing_policy_request import (
    ModelAvailableModel,
)


@pytest.mark.unit
def test_build_available_models_from_capability_scores() -> None:
    capability_rows: list[dict[str, object]] = [
        {
            "model_key": "qwen3-coder-30b",
            "task_type": "adr_extraction",
            "success_rate": 0.92,
            "avg_latency_ms": 4500.0,
            "total_cost": 0.12,
            "total_count": 10,
        },
        {
            "model_key": "deepseek-r1-14b",
            "task_type": "adr_extraction",
            "success_rate": 0.85,
            "avg_latency_ms": 3200.0,
            "total_cost": 0.08,
            "total_count": 10,
        },
    ]
    cost_map = {
        "qwen3-coder-30b": 0.0001,
        "deepseek-r1-14b": 0.00005,
    }

    models = build_available_models_from_scores(capability_rows, cost_map)

    assert len(models) == 2
    assert models[0].key == "qwen3-coder-30b"
    assert models[0].score == pytest.approx(0.92, abs=0.01)
    assert models[0].cost_per_token == pytest.approx(0.0001)
    assert models[1].key == "deepseek-r1-14b"
    assert models[1].score == pytest.approx(0.85, abs=0.01)
    assert models[1].cost_per_token == pytest.approx(0.00005)


@pytest.mark.unit
def test_build_available_models_empty_scores_returns_empty() -> None:
    models = build_available_models_from_scores([], {})
    assert models == []


@pytest.mark.unit
def test_build_available_models_missing_cost_defaults_zero() -> None:
    capability_rows: list[dict[str, object]] = [
        {"model_key": "some-model", "success_rate": 0.75},
    ]
    models = build_available_models_from_scores(capability_rows, {})

    assert len(models) == 1
    assert models[0].cost_per_token == 0.0


@pytest.mark.unit
def test_build_available_models_none_success_rate_treated_as_zero() -> None:
    capability_rows: list[dict[str, object]] = [
        {"model_key": "some-model", "success_rate": None},
    ]
    models = build_available_models_from_scores(capability_rows, {})

    assert len(models) == 1
    assert models[0].score == 0.0


@pytest.mark.unit
def test_build_available_models_returns_model_available_model_instances() -> None:
    capability_rows: list[dict[str, object]] = [
        {"model_key": "m1", "success_rate": 0.5},
    ]
    models = build_available_models_from_scores(capability_rows, {"m1": 0.001})

    assert all(isinstance(m, ModelAvailableModel) for m in models)
    assert models[0].capabilities == frozenset()
