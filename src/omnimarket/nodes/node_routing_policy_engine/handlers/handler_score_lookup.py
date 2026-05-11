# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Bridge function: converts capability_scores rows to ModelAvailableModel instances."""

from __future__ import annotations

from omnimarket.nodes.node_routing_policy_engine.models.model_routing_policy_request import (
    ModelAvailableModel,
)


def build_available_models_from_scores(
    capability_rows: list[dict[str, object]],
    cost_map: dict[str, float],
) -> list[ModelAvailableModel]:
    """Convert capability_scores table rows into ModelAvailableModel instances.

    Args:
        capability_rows: Rows from the capability_scores table. Each row must
            contain a ``model_key`` field and should contain a ``success_rate``
            field (float in [0, 1]).  Missing or None success_rate defaults to
            0.0.
        cost_map: Mapping from model_key to cost_per_token. Missing keys default
            to 0.0.

    Returns:
        List of ModelAvailableModel instances suitable for passing to the
        routing policy engine.  Order matches the input row order.
    """
    models: list[ModelAvailableModel] = []
    for row in capability_rows:
        model_key = str(row["model_key"])
        raw_score = row.get("success_rate")
        score = float(str(raw_score)) if raw_score is not None else 0.0
        cost = cost_map.get(model_key, 0.0)
        models.append(
            ModelAvailableModel(
                key=model_key,
                score=score,
                cost_per_token=cost,
                capabilities=frozenset(),
            ),
        )
    return models


__all__ = ["build_available_models_from_scores"]
