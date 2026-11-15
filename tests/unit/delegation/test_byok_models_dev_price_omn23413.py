# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23413 Amendment 10, AC35: a customer-key call is priced from models.dev when
neither the provider catalogue nor the model registry prices its model.

Read live on 2026-11-14: Reflection's own `/openai/v1/models` and chat responses
carry no price, while models.dev lists reflection/Beam-1.0-501B-A23B at input 0,
output 0. Before this change every Beam call on a Reflection key was booked with
cost basis UNKNOWN and usage source unknown, which the Runs page shows as
"unknown". These tests pin the models.dev price for a model the catalogue leaves
unpriced, a paid models.dev price, and a model models.dev does not list.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from omnimarket.enums.enum_cost_basis import EnumCostBasis
from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.handlers import model_catalog
from omnimarket.handlers.model_catalog import ModelsDevIndex
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call import (
    _compute_cost,
    _cost_usage_source,
)

pytestmark = pytest.mark.unit

REFLECTION = "https://api.reflection.ai/openai/v1/chat/completions"
BEAM = "Beam-1.0-501B-A23B"


def _index(models: dict[tuple[str, str], dict[str, object]]) -> ModelsDevIndex:
    return ModelsDevIndex(
        models=models, fetched_at="2026-11-14T03:36:52+00:00", stale=False, error=None
    )


@pytest.fixture
def models_dev(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    def serve(models: dict[tuple[str, str], dict[str, object]]) -> None:
        monkeypatch.setattr(
            model_catalog, "pricing_models_dev_index", lambda: _index(models)
        )

    return serve


def test_models_dev_zero_price_books_a_beam_call_as_free_and_measured(
    models_dev,
) -> None:  # type: ignore[no-untyped-def]
    models_dev(
        {
            ("reflection", BEAM): {
                "id": BEAM,
                "cost": {"input": 0, "output": 0, "cache_read": 0},
            }
        }
    )
    actual, _opus, savings, cost_basis = _compute_cost(
        BEAM,
        1482,
        6056,
        model_tier="cheap_cloud",
        endpoint_url=REFLECTION,
        provider="byok-reflection",
    )
    assert actual == Decimal("0")
    assert savings > 0
    assert cost_basis is EnumCostBasis.ZERO_MARGINAL_API_COST
    assert _cost_usage_source(cost_basis) is EnumUsageSource.MEASURED


def test_a_paid_models_dev_price_is_booked_as_cloud_cost(models_dev) -> None:  # type: ignore[no-untyped-def]
    models_dev(
        {("reflection", BEAM): {"id": BEAM, "cost": {"input": 0.3, "output": 1.0}}}
    )
    actual, _opus, _savings, cost_basis = _compute_cost(
        BEAM,
        1_000_000,
        1_000_000,
        model_tier="cheap_cloud",
        endpoint_url=REFLECTION,
        provider="byok-reflection",
    )
    assert actual == Decimal("1.3")
    assert cost_basis is EnumCostBasis.CLOUD_API_COST


def test_a_model_models_dev_does_not_list_stays_unpriced(models_dev) -> None:  # type: ignore[no-untyped-def]
    models_dev({})
    actual, _opus, savings, cost_basis = _compute_cost(
        BEAM,
        1482,
        6056,
        model_tier="cheap_cloud",
        endpoint_url=REFLECTION,
        provider="byok-reflection",
    )
    assert actual == Decimal("0")
    assert savings == Decimal("0")
    assert cost_basis is EnumCostBasis.UNKNOWN
    assert _cost_usage_source(cost_basis) is EnumUsageSource.UNKNOWN


def test_a_models_dev_entry_without_a_price_stays_unpriced(models_dev) -> None:  # type: ignore[no-untyped-def]
    models_dev({("reflection", BEAM): {"id": BEAM}})
    _actual, _opus, _savings, cost_basis = _compute_cost(
        BEAM,
        1482,
        6056,
        model_tier="cheap_cloud",
        endpoint_url=REFLECTION,
        provider="byok-reflection",
    )
    assert cost_basis is EnumCostBasis.UNKNOWN
