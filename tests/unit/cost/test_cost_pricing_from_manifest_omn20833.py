# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20833: omnimarket's cost surface prices cloud models from the infra manifest.

The omnibase_infra pricing manifest is the one pricing authority. A cloud row in
``cost_pricing.yaml`` names a provider and a model only; its token prices come
from the manifest when the contract loads, and a cloud model the manifest does
not carry stays unpriced.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from omnibase_infra.models.pricing.model_pricing_table import ModelPricingTable

from omnimarket.cost.cost_pricing import (
    COST_PRICING_CONTRACT_PATH,
    MissingCostPricingError,
    calculate_inference_cost,
    load_cost_pricing,
    lookup_cost_pricing,
    validate_cost_pricing,
)
from omnimarket.enums.enum_cost_basis import EnumCostBasis

PLANTED_UNKNOWN_MODEL = "omn20833-model-not-in-any-manifest"


def _raw_cloud_rows() -> list[dict[str, object]]:
    data = yaml.safe_load(COST_PRICING_CONTRACT_PATH.read_text())
    return [
        row
        for row in data["entries"]
        if row["cost_basis"] == EnumCostBasis.CLOUD_API_COST.value
    ]


def test_cost_pricing_yaml_carries_no_cloud_price() -> None:
    rows = _raw_cloud_rows()
    assert rows
    for row in rows:
        assert row.get("input_token_price") is None, row
        assert row.get("output_token_price") is None, row


@pytest.mark.parametrize(
    ("provider", "model_id"),
    [(row["provider"], row["model_id"]) for row in _raw_cloud_rows()],
)
def test_cloud_price_equals_the_manifest(provider: str, model_id: str) -> None:
    manifest_entry = ModelPricingTable.from_yaml().get_entry(model_id)
    assert manifest_entry is not None
    entry = lookup_cost_pricing(load_cost_pricing(), provider, model_id)
    assert entry.cost_basis == EnumCostBasis.CLOUD_API_COST
    cost = calculate_inference_cost(entry, input_tokens=1_000, output_tokens=1_000)
    assert cost == Decimal(str(manifest_entry.input_cost_per_1k)) + Decimal(
        str(manifest_entry.output_cost_per_1k)
    )
    assert "omnibase_infra pricing manifest" in entry.provenance


def test_a_cloud_row_with_its_own_price_is_refused(tmp_path: Path) -> None:
    data = yaml.safe_load(COST_PRICING_CONTRACT_PATH.read_text())
    for row in data["entries"]:
        if row["cost_basis"] == EnumCostBasis.CLOUD_API_COST.value:
            row["input_token_price"] = "0.00000010"
            row["output_token_price"] = "0.00000040"
            break
    path = tmp_path / "cost_pricing.yaml"
    path.write_text(yaml.safe_dump(data))
    ok, errors = validate_cost_pricing(path)
    assert not ok
    assert "pricing manifest" in errors[0]


def test_a_cloud_model_absent_from_the_manifest_loads_unpriced(
    tmp_path: Path,
) -> None:
    data = yaml.safe_load(COST_PRICING_CONTRACT_PATH.read_text())
    data["entries"].append(
        {
            "provider": "cloud",
            "model_id": PLANTED_UNKNOWN_MODEL,
            "currency": "USD",
            "usage_source": "estimated",
            "cost_basis": EnumCostBasis.CLOUD_API_COST.value,
        }
    )
    path = tmp_path / "cost_pricing.yaml"
    path.write_text(yaml.safe_dump(data))
    entry = lookup_cost_pricing(load_cost_pricing(path), "cloud", PLANTED_UNKNOWN_MODEL)
    assert entry.cost_basis == EnumCostBasis.UNKNOWN
    assert entry.input_token_price is None
    assert entry.output_token_price is None
    with pytest.raises(MissingCostPricingError):
        calculate_inference_cost(entry, input_tokens=1_000, output_tokens=1_000)


def test_an_unknown_model_is_never_given_another_rate() -> None:
    entry = lookup_cost_pricing(
        load_cost_pricing(), "cloud", PLANTED_UNKNOWN_MODEL, allow_unknown=True
    )
    assert entry.cost_basis == EnumCostBasis.UNKNOWN
    assert entry.input_token_price is None
    assert entry.output_token_price is None
    with pytest.raises(MissingCostPricingError):
        calculate_inference_cost(entry, input_tokens=1_000, output_tokens=1_000)
