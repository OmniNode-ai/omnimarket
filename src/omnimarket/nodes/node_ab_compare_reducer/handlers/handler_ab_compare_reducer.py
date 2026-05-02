# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Handler that collects inference results and materializes the AB cost comparison.

Pure reducer: delta(state, result, pricing) -> (new_state, completed_payload | None).

When all expected inference results arrive for a correlation_id the handler
computes per-model cost from the supplied pricing map, builds a sorted
comparison table, calculates savings vs the most expensive cloud model, and
returns the completed payload.  No I/O is performed here — all pricing data is
passed in by the caller so the reducer stays unit-testable without any file I/O.

Pricing map format (keyed by model_key):
    {
        "qwen3-coder-30b": {"cost_per_1k_input": 0.0, "cost_per_1k_output": 0.0,
                             "display_name": "Qwen3-Coder-30B"},
        "claude-sonnet":   {"cost_per_1k_input": 0.003, "cost_per_1k_output": 0.015,
                             "display_name": "Claude Sonnet"},
        ...
    }
"""

from __future__ import annotations

import logging
from typing import Literal

from omnimarket.nodes.node_ab_compare_reducer.models.model_ab_compare_completed import (
    ModelAbCompareCompleted,
)
from omnimarket.nodes.node_ab_compare_reducer.models.model_ab_compare_state import (
    ModelAbCompareState,
)
from omnimarket.nodes.node_ab_compare_reducer.models.model_comparison_row import (
    ModelComparisonRow,
)
from omnimarket.nodes.node_ab_compare_reducer.models.model_inference_result_entry import (
    ModelInferenceResultEntry,
)

logger = logging.getLogger(__name__)

# Pricing map type alias: model_key -> {cost_per_1k_input, cost_per_1k_output, display_name, ...}
PricingMap = dict[str, dict[str, object]]


class HandlerAbCompareReducer:
    """Pure reducer that accumulates inference results and materializes the comparison.

    The handler is stateless — callers own the ModelAbCompareState and pass it
    in on every invocation (suitable for RuntimeLocal in-memory dict or any
    external state store in bus mode).
    """

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["reducer"] = "reducer"

    def accumulate(
        self,
        state: ModelAbCompareState,
        result: ModelInferenceResultEntry,
    ) -> ModelAbCompareState:
        """Add one inference result to the accumulation state.

        Idempotent: duplicate results for the same model_key are ignored.

        Args:
            state: Current accumulation state for this correlation_id.
            result: Incoming inference result to add.

        Returns:
            Updated state. If already completed, returns state unchanged.
        """
        if state.completed:
            logger.debug(
                "Ignoring result for completed correlation_id=%s model_key=%s",
                state.correlation_id,
                result.model_key,
            )
            return state

        if result.correlation_id != state.correlation_id:
            logger.warning(
                "correlation_id mismatch: state=%s result=%s — ignoring",
                state.correlation_id,
                result.correlation_id,
            )
            return state

        # Deduplicate by model_key
        existing_keys = {r.model_key for r in state.results}
        if result.model_key in existing_keys:
            logger.debug(
                "Duplicate result for model_key=%s correlation_id=%s — ignoring",
                result.model_key,
                state.correlation_id,
            )
            return state

        new_results = [*state.results, result]
        completed = len(new_results) >= state.expected_count

        logger.info(
            "accumulated result model_key=%s correlation_id=%s (%d/%d)",
            result.model_key,
            state.correlation_id,
            len(new_results),
            state.expected_count,
        )

        return state.model_copy(update={"results": new_results, "completed": completed})

    def materialize(
        self,
        state: ModelAbCompareState,
        pricing: PricingMap,
    ) -> ModelAbCompareCompleted | None:
        """Materialize the comparison payload when the state is complete.

        Args:
            state: Accumulation state (must have completed=True to produce output).
            pricing: Per-model pricing map keyed by model_key.

        Returns:
            ModelAbCompareCompleted when state.completed is True, else None.
        """
        if not state.completed:
            return None

        rows = [self._build_row(result, pricing) for result in state.results]

        # Sort by cost ascending (cheapest first)
        rows.sort(key=lambda r: r.cost_usd)

        # Savings = max cloud cost minus $0 local minimum
        max_cost = max((r.cost_usd for r in rows), default=0.0)
        min_cost = min((r.cost_usd for r in rows), default=0.0)
        savings_usd = max(0.0, max_cost - min_cost)

        return ModelAbCompareCompleted(
            correlation_id=state.correlation_id,
            rows=rows,
            savings_usd=savings_usd,
            model_count=len(rows),
        )

    def _build_row(
        self,
        result: ModelInferenceResultEntry,
        pricing: PricingMap,
    ) -> ModelComparisonRow:
        """Compute cost and assemble one comparison row."""
        model_pricing = pricing.get(result.model_key, {})
        display_name = str(model_pricing.get("display_name", result.model_key))
        cost_per_1k_input = float(str(model_pricing.get("cost_per_1k_input", 0.0)))
        cost_per_1k_output = float(str(model_pricing.get("cost_per_1k_output", 0.0)))

        cost_usd = (
            result.prompt_tokens * cost_per_1k_input / 1000.0
            + result.completion_tokens * cost_per_1k_output / 1000.0
        )

        return ModelComparisonRow(
            model_key=result.model_key,
            display_name=display_name,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            total_tokens=result.total_tokens,
            cost_usd=cost_usd,
            latency_ms=result.latency_ms,
            quality="skipped",
            error=result.error,
        )


__all__: list[str] = ["HandlerAbCompareReducer"]
