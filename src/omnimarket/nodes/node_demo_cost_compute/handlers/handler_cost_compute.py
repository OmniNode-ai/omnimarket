# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_demo_cost_compute [OMN-12235].

COMPUTE node — pure, idempotent. Applies per-model pricing tables to
inference results and returns cost per model plus cheapest model ID.
"""

from __future__ import annotations

from omnimarket.events.demo import ModelDemoCostEntry
from omnimarket.nodes.node_demo_cost_compute.models.model_cost_request import (
    ModelDemoCostRequest,
    ModelDemoCostResult,
)


class NodeDemoCostCompute:
    """COMPUTE — pricing lookup and cost calculation from inference results."""

    def handle(self, request: ModelDemoCostRequest) -> ModelDemoCostResult:
        totals: dict[str, dict[str, float | int]] = {}
        order: list[str] = []

        for result in request.inference_results:
            if result.model_id not in totals:
                order.append(result.model_id)
                totals[result.model_id] = {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                }
            totals[result.model_id]["prompt_tokens"] = (
                int(totals[result.model_id]["prompt_tokens"]) + result.prompt_tokens
            )
            totals[result.model_id]["completion_tokens"] = (
                int(totals[result.model_id]["completion_tokens"])
                + result.completion_tokens
            )

        costs: list[ModelDemoCostEntry] = []
        for model_id in order:
            prompt_tokens = int(totals[model_id]["prompt_tokens"])
            completion_tokens = int(totals[model_id]["completion_tokens"])
            pricing = request.pricing_table.get(model_id)
            prompt_rate = pricing.prompt_cost_per_1k if pricing else 0.0
            completion_rate = pricing.completion_cost_per_1k if pricing else 0.0
            prompt_cost = prompt_tokens * prompt_rate / 1000.0
            completion_cost = completion_tokens * completion_rate / 1000.0
            costs.append(
                ModelDemoCostEntry(
                    model_id=model_id,
                    prompt_cost_usd=round(prompt_cost, 12),
                    completion_cost_usd=round(completion_cost, 12),
                    total_cost_usd=round(prompt_cost + completion_cost, 12),
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
            )

        cheapest = min(costs, key=lambda entry: entry.total_cost_usd, default=None)
        return ModelDemoCostResult(
            costs=costs,
            cheapest_model_id=cheapest.model_id if cheapest else None,
        )
