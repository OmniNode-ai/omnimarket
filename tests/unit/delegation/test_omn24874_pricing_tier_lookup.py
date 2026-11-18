# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tier cost lookup returns the first tier of a name (OMN-24874 review).

The lookup before OMN-24874 walked the registry in order and returned the first
tier whose name matched. A dict comprehension keyed on the name returns the LAST
one instead. The registry parser does not reject a repeated tier name, so a
registry that repeats one must still price from its first entry.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from omnimarket.models.delegation.wire import parse_delegation_config_yaml
from omnimarket.pricing import resolve_tier_cost

_DUPLICATE_TIER_REGISTRY = """
tiers:
  - name: dup_tier
    cost:
      cost_type: metered
      rate_per_1k_usd: 0.001
  - name: other_tier
    cost:
      cost_type: free_local
  - name: dup_tier
    cost:
      cost_type: metered
      rate_per_1k_usd: 0.009
"""


@pytest.mark.unit
def test_omn24874_pricing_registry_parser_accepts_a_repeated_tier_name() -> None:
    # The premise of the defect: the registry does not reject a repeated tier name,
    # so the lookup order is observable.
    config = parse_delegation_config_yaml(_DUPLICATE_TIER_REGISTRY)
    assert [t.name for t in config.tiers] == ["dup_tier", "other_tier", "dup_tier"]


@pytest.mark.unit
def test_omn24874_pricing_lookup_returns_the_first_tier_with_the_name() -> None:
    config = parse_delegation_config_yaml(_DUPLICATE_TIER_REGISTRY)
    with patch("omnimarket.pricing._load_routing_config", return_value=config):
        cost = resolve_tier_cost("dup_tier")
        other = resolve_tier_cost("other_tier")
        missing = resolve_tier_cost("not_a_tier")
    assert cost is not None
    assert cost.rate_per_1k_usd == pytest.approx(0.001)  # the first, not the last
    assert other is not None  # positive control: a unique name still resolves
    assert missing is None
