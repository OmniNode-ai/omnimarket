# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration coverage for node_demo_cost_compute [OMN-13684].

WS-5 Wave 10. Variant A (direct in-process COMPUTE handler call). Each case
varies the inference-result fan-out and the pricing table, asserting the typed
``ModelDemoCostResult`` fields (per-model cost entries, token aggregation,
cheapest-model selection). Negative control: a model absent from the pricing
table must surface a real zero-cost entry and become the cheapest model — the
missing-pricing branch must demonstrably execute, not be skipped.
"""

from __future__ import annotations

import pytest

from omnimarket.events.demo import ModelDemoInferenceResult
from omnimarket.nodes.node_demo_cost_compute.handlers.handler_cost_compute import (
    NodeDemoCostCompute,
)
from omnimarket.nodes.node_demo_cost_compute.models.model_cost_request import (
    ModelDemoCostRequest,
    ModelDemoModelPricing,
)


def _ir(model_id: str, prompt: int, completion: int) -> ModelDemoInferenceResult:
    return ModelDemoInferenceResult(
        model_id=model_id,
        prompt_tokens=prompt,
        completion_tokens=completion,
        latency_ms=10.0,
    )


_P = ModelDemoModelPricing

# (case_id, inference_results, pricing_table, expected)
#   expected = dict with: cheapest, n_entries, per-model {model_id: (prompt_tok,
#   completion_tok, total_cost)}
CASES = [
    pytest.param(
        [_ir("m1", 1000, 500)],
        {"m1": _P(prompt_cost_per_1k=0.001, completion_cost_per_1k=0.002)},
        {
            "cheapest": "m1",
            "n_entries": 1,
            "per_model": {"m1": (1000, 500, 0.002)},
        },
        id="single-model-cost-calc",
    ),
    pytest.param(
        [_ir("m1", 1000, 1000), _ir("m2", 1000, 1000)],
        {
            "m1": _P(prompt_cost_per_1k=0.01, completion_cost_per_1k=0.01),
            "m2": _P(prompt_cost_per_1k=0.001, completion_cost_per_1k=0.001),
        },
        {
            "cheapest": "m2",
            "n_entries": 2,
            "per_model": {"m1": (1000, 1000, 0.02), "m2": (1000, 1000, 0.002)},
        },
        id="multi-model-fanout-cheapest",
    ),
    pytest.param(
        # NEGATIVE CONTROL: m_unknown is absent from the pricing table. The
        # missing-pricing branch must produce a real zero-cost entry, and that
        # zero-cost model must win the cheapest selection over the priced model.
        [_ir("m_known", 1000, 1000), _ir("m_unknown", 1000, 1000)],
        {"m_known": _P(prompt_cost_per_1k=0.01, completion_cost_per_1k=0.01)},
        {
            "cheapest": "m_unknown",
            "n_entries": 2,
            "per_model": {
                "m_known": (1000, 1000, 0.02),
                "m_unknown": (1000, 1000, 0.0),
            },
        },
        id="missing-pricing-zero-cost-NEGATIVE",
    ),
    pytest.param(
        # Same model_id appears twice — tokens must aggregate into one entry.
        [_ir("m1", 10, 5), _ir("m1", 10, 5)],
        {"m1": _P(prompt_cost_per_1k=0.001, completion_cost_per_1k=0.002)},
        {
            "cheapest": "m1",
            "n_entries": 1,
            "per_model": {"m1": (20, 10, 0.00004)},
        },
        id="duplicate-model-aggregation",
    ),
    pytest.param(
        # Zero-pricing ceiling: explicit 0.0 rates must yield exactly 0.0 cost.
        [_ir("m1", 1000, 1000)],
        {"m1": _P(prompt_cost_per_1k=0.0, completion_cost_per_1k=0.0)},
        {
            "cheapest": "m1",
            "n_entries": 1,
            "per_model": {"m1": (1000, 1000, 0.0)},
        },
        id="zero-pricing-ceiling",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(("inference_results", "pricing_table", "expected"), CASES)
def test_demo_cost_compute_multiparam(
    inference_results: list[ModelDemoInferenceResult],
    pricing_table: dict[str, ModelDemoModelPricing],
    expected: dict[str, object],
) -> None:
    handler = NodeDemoCostCompute()
    result = handler.handle(
        ModelDemoCostRequest(
            inference_results=inference_results,
            pricing_table=pricing_table,
        )
    )

    assert result.cheapest_model_id == expected["cheapest"]
    assert len(result.costs) == expected["n_entries"]

    by_model = {entry.model_id: entry for entry in result.costs}
    per_model: dict[str, tuple[int, int, float]] = expected["per_model"]  # type: ignore[assignment]
    for model_id, (prompt_tok, completion_tok, total_cost) in per_model.items():
        assert model_id in by_model, f"missing cost entry for {model_id}"
        entry = by_model[model_id]
        assert entry.prompt_tokens == prompt_tok
        assert entry.completion_tokens == completion_tok
        assert entry.total_cost_usd == pytest.approx(total_cost, abs=1e-9)
        # total must equal the two cost components summed (structural truth)
        assert entry.total_cost_usd == pytest.approx(
            entry.prompt_cost_usd + entry.completion_cost_usd, abs=1e-9
        )

    # cheapest must actually be the minimum-cost entry (no silent mis-selection)
    assert (
        result.cheapest_model_id
        == min(result.costs, key=lambda e: e.total_cost_usd).model_id
    )
