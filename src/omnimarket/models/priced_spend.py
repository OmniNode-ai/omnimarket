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

from omnimarket.pricing import _load_table

__all__ = ["historical_unpriced_zero", "priced_spend"]


def historical_unpriced_zero(
    amount: object, source: object, tier: object, *models: object
) -> bool:
    """True for a stored zero that no price stood behind. Never reprices a run.

    A run is free when its tier is local, its model id ends in ":free", or the
    pricing manifest prices its model (a declared zero included). A charged
    amount or a recorded cost source is served as stored, and so is a row that
    names no model. Only a zero with none of those is unknown.
    """
    if (
        source not in (None, "", "no_cost_model")
        or str(tier or "").strip().casefold() == "local"
    ):
        return False
    if amount is None or isinstance(amount, bool):
        return False
    try:
        if Decimal(str(amount)) != 0:
            return False
    except (InvalidOperation, ValueError):
        return False
    name = next(
        (str(m).strip() for m in models if m is not None and str(m).strip()), ""
    )
    if not name or name.casefold().endswith(":free"):
        return False
    table = _load_table()
    return table.get_entry(name) is None and table.get_entry(name.lower()) is None


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
