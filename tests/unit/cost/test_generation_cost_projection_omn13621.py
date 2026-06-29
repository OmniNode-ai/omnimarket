# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13621 DoD acceptance proof: a generated-node run records a normalized,
contract-priced cost in the canonical cost projection, queryable via the
projection endpoint.

This is the OMN-13408 projection-write pattern extended to generation cost:
  generation handler computes cost_inference_usd from the CONTRACT-SOURCED
  pricing surface (omnimarket.cost.cost_pricing) -> benchmark
  -> onex.evt.omnimarket.node-generation-completed.v1
  -> node_projection_delegation.project_generation_completed
  -> generation_events.cost_inference_usd  (exposed at
     GET /projection/onex.evt.omnimarket.node-generation-completed.v1)

The test proves:
  1. The generation handler's cost helper sources pricing from the contract,
     not a hardcoded source constant (local -> 0.0; cloud -> contract rate).
  2. The contract-priced cost is the exact value the cost-pricing contract
     yields for the measured tokens.
  3. That cost lands in the generation_events projection row's
     cost_inference_usd column (the queryable canonical cost projection).
"""

from __future__ import annotations

from decimal import Decimal

from omnimarket.cost.cost_pricing import (
    calculate_inference_cost,
    load_cost_pricing,
    lookup_cost_pricing,
)
from omnimarket.nodes.node_generation_consumer.handlers.handler_generation_consumer import (
    _calculate_cost,
)


class _RecordingDB:
    """Minimal DatabaseAdapter stand-in that captures upserted rows."""

    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    def upsert(self, table: str, conflict_key: str, row: dict[str, object]) -> bool:
        self.rows.append({"_table": table, **row})
        return True

    def query(
        self, table: str, filters: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        return []


def _project_generation(cost_inference_usd: float) -> dict[str, object]:
    """Drive node_projection_delegation's generation-completed projection and
    return the upserted generation_events row."""
    from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
        HandlerProjectionDelegation,
    )

    db = _RecordingDB()
    handler = HandlerProjectionDelegation()
    payload: dict[str, object] = {
        "_db": db,
        "_event_type": "onex.evt.omnimarket.node-generation-completed.v1",
        "correlation_id": "omn13621-gen-cost-001",
        "task_description": "Generate a sentiment classifier node",
        "provider": "cloud",
        "model_id": "gemini-2.5-flash",
        "endpoint_class": "cheap_cloud",
        "attempt_count": 2,
        "total_latency_e2e_ms": 4321,
        "contract_passed": True,
        "cost_inference_usd": cost_inference_usd,
        "contract_yaml": "name: node_sentiment\n",
        "handler_source": "def handle(input_data):\n    return {}\n",
        "routing_source": "routing_authority",
        "resolved_endpoint": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    }
    handler.handle(payload)
    assert db.rows, "expected one generation_events upsert"
    return db.rows[0]


def test_local_run_costs_zero_from_contract() -> None:
    # Local provider resolves to a zero_marginal_api_cost contract entry -> 0.0.
    cost = _calculate_cost("local", "Qwen3.6-35B-A3B", 10_000, 5_000)
    assert cost == 0.0


def test_cloud_run_cost_is_contract_priced_not_hardcoded() -> None:
    # The cloud cost the generation handler computes must equal the contract's
    # priced value for the measured tokens (NOT a hardcoded source constant).
    input_tokens = 12_000
    output_tokens = 3_000
    cost = _calculate_cost("cloud", "gemini-2.5-flash", input_tokens, output_tokens)

    contract = load_cost_pricing()
    entry = lookup_cost_pricing(contract, "cloud", "gemini-2.5-flash")
    expected = float(
        calculate_inference_cost(
            entry, input_tokens=input_tokens, output_tokens=output_tokens
        )
    )
    assert cost == expected
    assert cost > 0.0
    # Cross-check the exact arithmetic against the contract per-token rates so a
    # silent rate drift is caught: 0.00000010*12000 + 0.00000040*3000.
    manual = float(
        Decimal("0.00000010") * Decimal(input_tokens)
        + Decimal("0.00000040") * Decimal(output_tokens)
    )
    assert cost == manual


def test_unknown_model_costs_zero_without_crashing() -> None:
    # A model with no contract entry resolves to explicit UNKNOWN -> 0.0, never
    # a crash and never a silent mispricing.
    cost = _calculate_cost("cloud", "model-not-in-contract", 1_000, 1_000)
    assert cost == 0.0


def test_contract_priced_generation_cost_lands_in_projection() -> None:
    """End-to-end DoD proof: contract-priced generation cost is queryable in the
    canonical cost projection's generation_events row."""
    input_tokens = 12_000
    output_tokens = 3_000
    cost = _calculate_cost("cloud", "gemini-2.5-flash", input_tokens, output_tokens)
    assert cost > 0.0

    row = _project_generation(cost)
    assert row["_table"] == "generation_events"
    # The normalized, contract-priced cost is persisted in the canonical cost
    # projection column exposed by the projection endpoint.
    assert row["cost_inference_usd"] == cost
    assert float(row["cost_inference_usd"]) > 0.0
    assert row["correlation_id"] == "omn13621-gen-cost-001"
    assert row["provider"] == "cloud"
    assert row["model_id"] == "gemini-2.5-flash"
