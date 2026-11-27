# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve requested prices from the canonical manifest."""

import contextlib
import contextvars
from collections.abc import Iterator
from decimal import Decimal
from typing import cast

from omnibase_infra.models.pricing.model_pricing_table import ModelPricingTable

from omnimarket.nodes.node_metering_summary_compute import ModelCounterfactualBaseline

_pass_cache: contextvars.ContextVar[dict[object, object] | None] = (
    contextvars.ContextVar("baseline_resolution_cache", default=None)
)


def pass_cache() -> dict[object, object] | None:
    """The held resolutions of the running baseline_resolution_cache block; None outside one."""
    return _pass_cache.get()


@contextlib.contextmanager
def baseline_resolution_cache() -> Iterator[None]:
    """OMN-26739: within this block each baseline selection and price is resolved once.

    A resolution reads and parses the pricing manifest (about 150 ms), so a pass
    that figures every stored run (the store's savings recompute) holds one.
    Outside the block every call reads the manifest, as before.
    """
    token = _pass_cache.set({})
    try:
        yield
    finally:
        _pass_cache.reset(token)


def resolve_baseline(model_id: str) -> ModelCounterfactualBaseline | None:
    """Pin the counterfactual price from the canonical pricing manifest.

    Returns ``None`` when the model is absent from the manifest. The caller
    must not substitute another model's price: a savings figure computed
    against a baseline nobody asked for is worse than no figure, because it
    looks like the one that was asked for.
    """
    held = _pass_cache.get()
    if held is not None and model_id in held:
        return cast("ModelCounterfactualBaseline | None", held[model_id])
    resolved = _resolve_from_manifest(model_id)
    if held is not None:
        held[model_id] = resolved
    return resolved


def _resolve_from_manifest(model_id: str) -> ModelCounterfactualBaseline | None:
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
