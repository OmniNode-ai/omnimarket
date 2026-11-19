# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The writer prices glm-5.3-flash on the general API from the declared catalogue.

A customer key on z.ai's general API resolves ``glm-5.3-flash``, a metered model.
The catalogue declares its price beside the row, and the effect handler books a
call on that key from the declared table: a priced model yields a cost above
zero, and a model no table prices stays unpriced (cost basis UNKNOWN), never
booked at a tier rate or as free.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from omnimarket.enums.enum_cost_basis import EnumCostBasis
from omnimarket.handlers.routing.byok_provider_backends import (
    ModelByokPricing,
    ModelByokProviderBackend,
    byok_declared_price_per_1m,
    byok_model_price_per_1m,
    load_byok_plan_catalog,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call import (
    _compute_cost,
)

_GENERAL_API_URL = "https://api.z.ai/api/paas/v4/chat/completions"
_BACKEND = "byok-glm-general"


def test_the_catalogue_declares_glm_53_flash_at_its_published_price() -> None:
    assert byok_declared_price_per_1m(_GENERAL_API_URL, "glm-5.3-flash") == (
        Decimal("0.15"),
        Decimal("0.50"),
    )
    assert byok_model_price_per_1m(_BACKEND, _GENERAL_API_URL, "glm-5.3-flash") == (
        Decimal("0.15"),
        Decimal("0.50"),
    )


def test_a_priced_model_on_a_customer_key_yields_cost_above_zero() -> None:
    actual, _opus, _savings, basis = _compute_cost(
        "glm-5.3-flash",
        1000,
        500,
        model_tier="tenant_byok",
        endpoint_url=_GENERAL_API_URL,
        provider=_BACKEND,
    )
    assert actual == Decimal("0.0004")
    assert basis is EnumCostBasis.CLOUD_API_COST


def test_an_unpriced_model_on_a_customer_key_still_reads_unknown_at_zero() -> None:
    actual, _opus, savings, basis = _compute_cost(
        "glm-5.3-flashx",
        1000,
        500,
        model_tier="tenant_byok",
        endpoint_url=_GENERAL_API_URL,
        provider=_BACKEND,
    )
    assert actual == Decimal("0")
    assert savings == Decimal("0")
    assert basis is EnumCostBasis.UNKNOWN


def _glm_general_row() -> ModelByokProviderBackend:
    return next(
        backend
        for backend in load_byok_plan_catalog().values()
        if backend.endpoint_url == _GENERAL_API_URL and backend.model_prices
    )


def _with_model_prices(
    row: ModelByokProviderBackend, *prices: ModelByokPricing
) -> ModelByokProviderBackend:
    data = row.model_dump()
    data["model_prices"] = [price.model_dump() for price in prices]
    return ModelByokProviderBackend.model_validate(data)


def test_a_model_priced_twice_is_refused() -> None:
    row = _glm_general_row()
    price = row.model_prices[0]

    with pytest.raises(ValueError, match="more than once"):
        _with_model_prices(row, price, price)


def test_a_model_that_repeats_the_pricing_model_name_is_refused() -> None:
    row = _glm_general_row()
    assert row.pricing is not None
    repeat = row.model_prices[0].model_copy(
        update={"model_name": row.pricing.model_name}
    )

    with pytest.raises(ValueError, match="more than once"):
        _with_model_prices(row, repeat)


def test_the_row_as_declared_loads_with_each_model_priced_once() -> None:
    row = _glm_general_row()

    assert _with_model_prices(row, *row.model_prices).model_prices == row.model_prices
