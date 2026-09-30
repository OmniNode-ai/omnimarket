# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve requested prices from the canonical manifest."""

from decimal import Decimal

from omnibase_infra.models.pricing.model_pricing_table import ModelPricingTable

from omnimarket.nodes.node_metering_summary_compute import ModelCounterfactualBaseline


def resolve_baseline(model_id: str) -> ModelCounterfactualBaseline | None:
    """Pin the counterfactual price from the canonical pricing manifest.

    Returns ``None`` when the model is absent from the manifest. The caller
    must not substitute another model's price: a savings figure computed
    against a baseline nobody asked for is worse than no figure, because it
    looks like the one that was asked for.
    """
    table = ModelPricingTable.from_yaml()
    entry = table.get_entry(model_id)
    if entry is None:
        return None
    return ModelCounterfactualBaseline(
        model=model_id,
        price_in_per_1k=Decimal(str(entry.input_cost_per_1k)),
        price_out_per_1k=Decimal(str(entry.output_cost_per_1k)),
        as_of=entry.effective_date,
        pricing_manifest_version=table.schema_version,
        source="pricing_manifest",
    )
