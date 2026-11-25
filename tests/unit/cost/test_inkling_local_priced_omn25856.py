# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-25856: Inkling-2-Mini, served from an owned lab GPU, is priced at zero marginal API cost.

A run answered by a model the pricing contract does not name is booked UNKNOWN, so the
dashboard counts it as unpriced and unmeasured. The self-hosted Inkling server carries the
same zero-marginal-cost row the other owned-GPU models carry.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from omnimarket.cost.cost_pricing import (
    MissingCostPricingError,
    load_cost_pricing,
    lookup_cost_pricing,
)
from omnimarket.enums.enum_cost_basis import EnumCostBasis

INKLING = "Inkling-2-Mini"


def test_inkling_2_mini_has_a_zero_marginal_cost_row() -> None:
    entry = lookup_cost_pricing(load_cost_pricing(), "local", INKLING)
    assert entry.cost_basis == EnumCostBasis.ZERO_MARGINAL_API_COST
    assert entry.input_token_price == Decimal(0)
    assert entry.output_token_price == Decimal(0)


def test_positive_control_unlisted_local_model_stays_unpriced() -> None:
    with pytest.raises(MissingCostPricingError):
        lookup_cost_pricing(load_cost_pricing(), "local", "Inkling-9-Nonexistent")
