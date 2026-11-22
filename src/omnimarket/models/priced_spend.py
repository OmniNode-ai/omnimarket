# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A run's recorded cost as spend, only from a named price source.

OMN-24615 AC3, decisions D1 and D3. A stored cost is spend only when the
pricing manifest prices the model that ran, and a positive cost on a model the
manifest lists as free is a routing tier's flat rate, not a price. Any other
run is unpriced (None): the metering fold counts it in runs_unknown_spend and
gives it no saving. Stored rows are classified here, at read time, and never
rewritten (D1).
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from omnibase_core.enums.enum_tier_cost_type import EnumTierCostType

from omnimarket.pricing import _load_table, resolve_tier_cost

__all__ = ["historical_unpriced_zero", "priced_spend"]


def historical_unpriced_zero(
    amount: object, source: object, tier: object, tier_type: object, *models: object
) -> bool:
    """True for a stored zero that no price stood behind. Never reprices a run.

    A charged amount, or a cost source other than no_cost_model, is served as
    stored. The row's own record comes first: a recorded free_local tier type
    is free, and a recorded no_cost_model source or non-free tier type is not
    made free by today's routing registry. Only a row that records neither is
    read against the registry, and only a tier it declares free_local is free;
    a tier with no cost model is not a free tier. A row that records no tier,
    tier type or source, or names no model, is served as stored, since nothing
    in it shows a priced cloud call. Otherwise the run is free only when its
    model id ends in ":free" or the pricing manifest declares its price zero.
    """
    recorded_source = str(source or "").strip()
    tier_cost_type = str(tier_type or "").strip().casefold()
    if recorded_source not in ("", "no_cost_model"):
        return False
    if tier_cost_type == EnumTierCostType.FREE_LOCAL.value:
        return False
    if not recorded_source and not tier_cost_type:
        tier_name = str(tier or "").strip().casefold()
        if not tier_name:
            return False
        cost = resolve_tier_cost(tier_name)
        if cost is not None and cost.cost_type is EnumTierCostType.FREE_LOCAL:
            return False
    if amount is None or isinstance(amount, bool):
        return False
    try:
        if Decimal(str(amount)) != 0:
            return False
    except (InvalidOperation, ValueError):
        return False
    # Every name the row records is read: delegated_to can be a backend alias
    # while routed_model names the model that answered.
    names = [str(m).strip() for m in models if m is not None and str(m).strip()]
    if not names:
        return False
    return not any(_declared_free(name) for name in names)


def _declared_free(name: str) -> bool:
    """A ":free" model id, or one the pricing manifest declares at a zero price.

    A positive price does not make a stored zero free, and the run is not
    repriced here.
    """
    if name.casefold().endswith(":free"):
        return True
    table = _load_table()
    entry = table.get_entry(name) or table.get_entry(name.lower())
    return (
        entry is not None
        and entry.input_cost_per_1k == 0
        and entry.output_cost_per_1k == 0
    )


def priced_spend(row: Mapping[str, object]) -> Decimal | None:
    """The row's ``cost_usd`` as spend, or None when no named price stands behind it."""
    raw = row.get("cost_usd")
    if raw is None:
        return None
    try:
        spend = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    model = str(row.get("model_name") or row.get("delegated_to") or "")
    # The manifest keys some models in lower case (qwen3.6-35b-a3b) while runs
    # record the served name (Qwen3.6-35B-A3B).
    table = _load_table()
    entry = table.get_entry(model) or table.get_entry(model.lower())
    if entry is None:
        return None
    free = entry.input_cost_per_1k == 0 and entry.output_cost_per_1k == 0
    return None if free and spend > 0 else spend
