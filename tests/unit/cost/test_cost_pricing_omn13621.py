# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13621: contract-sourced pricing tests for the canonical cost surface.

Pricing data is sourced from ``omnimarket/cost/cost_pricing.yaml`` (a contract),
never hardcoded in source. These tests prove the routed SEA pricing logic
(load/validate/lookup/calculate) on the canonical contract.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from omnimarket.cost.cost_pricing import (
    COST_PRICING_CONTRACT_PATH,
    MissingCostPricingError,
    ModelCostPricingContract,
    ModelCostPricingEntry,
    calculate_inference_cost,
    load_cost_pricing,
    lookup_cost_pricing,
    validate_cost_pricing,
)
from omnimarket.enums.enum_cost_basis import EnumCostBasis
from omnimarket.enums.enum_usage_source import EnumUsageSource


def test_canonical_contract_path_exists() -> None:
    assert COST_PRICING_CONTRACT_PATH.exists()
    assert COST_PRICING_CONTRACT_PATH.name == "cost_pricing.yaml"


def test_load_canonical_contract_validates() -> None:
    contract = load_cost_pricing()
    assert isinstance(contract, ModelCostPricingContract)
    assert contract.price_unit == "USD_PER_TOKEN"
    assert len(contract.entries) >= 1
    # The validator requires at least one cloud and one zero-marginal entry.
    bases = {entry.cost_basis for entry in contract.entries}
    assert EnumCostBasis.CLOUD_API_COST in bases
    assert EnumCostBasis.ZERO_MARGINAL_API_COST in bases


def test_validate_cost_pricing_passes_on_canonical() -> None:
    ok, errors = validate_cost_pricing()
    assert ok is True
    assert errors == ()


def test_lookup_cloud_entry_returns_priced_entry() -> None:
    contract = load_cost_pricing()
    entry = lookup_cost_pricing(contract, "google", "gemini-2.5-flash")
    assert entry.cost_basis == EnumCostBasis.CLOUD_API_COST
    assert entry.input_token_price is not None
    assert entry.output_token_price is not None


def test_lookup_unknown_without_allow_raises() -> None:
    contract = load_cost_pricing()
    with pytest.raises(MissingCostPricingError):
        lookup_cost_pricing(contract, "nonexistent_provider", "nonexistent_model")


def test_lookup_unknown_with_allow_returns_unknown_entry() -> None:
    contract = load_cost_pricing()
    entry = lookup_cost_pricing(
        contract, "nonexistent_provider", "nonexistent_model", allow_unknown=True
    )
    assert entry.cost_basis == EnumCostBasis.UNKNOWN
    assert entry.usage_source == EnumUsageSource.UNKNOWN
    assert entry.input_token_price is None
    assert entry.output_token_price is None


def test_calculate_inference_cost_from_contract_entry() -> None:
    contract = load_cost_pricing()
    entry = lookup_cost_pricing(contract, "google", "gemini-2.5-flash")
    cost = calculate_inference_cost(entry, input_tokens=1000, output_tokens=500)
    # 0.00000010 * 1000 + 0.00000040 * 500 = 0.0001 + 0.0002 = 0.0003
    assert cost == Decimal("0.0003")
    assert cost > 0


def test_calculate_inference_cost_rejects_negative_tokens() -> None:
    contract = load_cost_pricing()
    entry = lookup_cost_pricing(contract, "google", "gemini-2.5-flash")
    with pytest.raises(ValueError, match="must be non-negative"):
        calculate_inference_cost(entry, input_tokens=-1, output_tokens=0)


def test_calculate_inference_cost_unknown_basis_raises() -> None:
    entry = ModelCostPricingEntry(
        provider="p",
        model_id="m",
        input_token_price=None,
        output_token_price=None,
        currency="USD",
        provenance="explicit unknown",
        usage_source=EnumUsageSource.UNKNOWN,
        cost_basis=EnumCostBasis.UNKNOWN,
    )
    with pytest.raises(MissingCostPricingError):
        calculate_inference_cost(entry, input_tokens=10, output_tokens=10)


def test_local_zero_marginal_entry_is_zero_cost() -> None:
    contract = load_cost_pricing()
    entry = lookup_cost_pricing(contract, "local-vllm", "Qwen3-Coder-30B")
    assert entry.cost_basis == EnumCostBasis.ZERO_MARGINAL_API_COST
    cost = calculate_inference_cost(entry, input_tokens=1000, output_tokens=500)
    assert cost == Decimal("0")


def test_contract_hash_is_deterministic() -> None:
    a = load_cost_pricing()
    b = load_cost_pricing(COST_PRICING_CONTRACT_PATH)
    assert a.cost_pricing_hash == b.cost_pricing_hash
    assert a.cost_pricing_hash.startswith("sha256:")


def test_missing_file_validate_fails_closed(tmp_path: Path) -> None:
    missing = tmp_path / "nope.yaml"
    ok, errors = validate_cost_pricing(missing)
    assert ok is False
    assert errors
