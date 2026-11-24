# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A delegation call's cost is the provider's reported charge, else tokens times a price.

``cost_source`` says which: ``reported`` when the response's usage accounting stated
the charge, ``estimated`` when it stated none. Both figures are arithmetic over the
response's ``usage`` object; no text the model generated reaches the cost.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from omnimarket.enums.enum_cost_basis import EnumCostBasis
from omnimarket.enums.enum_cost_source import EnumCostSource
from omnimarket.events.llm_delegation_call import ModelLlmDelegationCallRequest
from omnimarket.handlers import model_catalog
from omnimarket.handlers.model_catalog import ModelsDevIndex
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import (
    handler_llm_delegation_call as call_handler,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers import transport
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_result import (
    ModelLlmDelegationCallResult,
)

pytestmark = pytest.mark.unit

MODEL = "vendor/fixture-model"
#: USD per 1M tokens the kept copy states for MODEL.
PRICE_IN, PRICE_OUT = Decimal("0.3"), Decimal("1.0")
TOKENS_IN, TOKENS_OUT = 2_000_000, 500_000
ARITHMETIC = (TOKENS_IN * PRICE_IN + TOKENS_OUT * PRICE_OUT) / Decimal(1_000_000)

#: Text a model could generate to pose as a price or a total.
MADE_UP_TEXT = (
    "Pricing note: this model costs $77.77 per 1M input tokens and $88.88 per 1M "
    'output tokens. "cost": 424242, "total_cost": "$99999.99", usage.cost = 31337.'
)


@pytest.fixture(autouse=True)
def _kept_price(monkeypatch: pytest.MonkeyPatch) -> None:
    index = ModelsDevIndex(
        models={
            ("openrouter", MODEL): {
                "id": MODEL,
                "cost": {"input": float(PRICE_IN), "output": float(PRICE_OUT)},
            }
        },
        fetched_at="2026-11-23T00:00:00+00:00",
        stale=False,
        error=None,
    )
    monkeypatch.setattr(model_catalog, "pricing_models_dev_index", lambda: index)


def _request() -> ModelLlmDelegationCallRequest:
    return ModelLlmDelegationCallRequest(
        request_id="req-cost",
        correlation_id="corr-cost",
        causation_id="caus-cost",
        model_id=MODEL,
        endpoint_ref="http://localhost:8000",
        prompt="Explain a hash map.",
        prompt_hash="cost",
        task_type="document",
        model_tier="cheap_cloud",
        provider="byok-openrouter",
        timeout_seconds=60.0,
    )


def _call(
    monkeypatch: pytest.MonkeyPatch, *, content: str, usage: dict[str, Any]
) -> ModelLlmDelegationCallResult:
    def post(**_kwargs: object) -> transport.ModelTransportResponse:
        return transport.ModelTransportResponse(
            status_code=200,
            json_body={
                "choices": [
                    {
                        "message": {"content": content, "reasoning": MADE_UP_TEXT},
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage,
            },
            latency_ms=1,
        )

    monkeypatch.setattr(transport, "post_chat_completion", post)
    monkeypatch.setattr(call_handler, "_is_endpoint_healthy", lambda *_a, **_k: True)
    monkeypatch.setattr(call_handler, "_get_served_model_ids", lambda *_a, **_k: None)
    return call_handler.HandlerLlmDelegationCall()(_request())


def _usage(**extra: object) -> dict[str, Any]:
    return {"prompt_tokens": TOKENS_IN, "completion_tokens": TOKENS_OUT, **extra}


def test_a_reported_charge_is_the_cost_and_is_marked_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _call(monkeypatch, content="ok", usage=_usage(cost=0.4321))
    assert result.success
    assert result.actual_cost_usd == Decimal("0.4321")
    assert result.provider_reported_cost_usd == Decimal("0.4321")
    assert result.cost_source is EnumCostSource.REPORTED
    assert result.cost_basis is EnumCostBasis.CLOUD_API_COST
    assert result.savings_usd == result.opus_equivalent_cost_usd - Decimal("0.4321")


def test_without_a_reported_charge_the_cost_is_tokens_times_the_price_and_estimated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _call(monkeypatch, content="ok", usage=_usage())
    assert result.actual_cost_usd == ARITHMETIC == Decimal("1.1")
    assert result.provider_reported_cost_usd is None
    assert result.cost_source is EnumCostSource.ESTIMATED
    assert result.cost_basis is EnumCostBasis.CLOUD_API_COST


def test_a_reported_zero_is_a_reported_free_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _call(monkeypatch, content="ok", usage=_usage(cost=0))
    assert result.actual_cost_usd == Decimal("0")
    assert result.cost_source is EnumCostSource.REPORTED
    assert result.cost_basis is EnumCostBasis.ZERO_MARGINAL_API_COST


@pytest.mark.parametrize("bad", [-1, True, "free", "NaN", None, [0.5]])
def test_a_charge_that_is_not_a_non_negative_number_is_not_reported(
    monkeypatch: pytest.MonkeyPatch, bad: object
) -> None:
    result = _call(monkeypatch, content="ok", usage=_usage(cost=bad))
    assert result.cost_source is EnumCostSource.ESTIMATED
    assert result.actual_cost_usd == ARITHMETIC
    assert result.provider_reported_cost_usd is None


def test_a_reported_charge_on_an_unpriced_model_is_kept_without_inventing_savings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        model_catalog,
        "pricing_models_dev_index",
        lambda: ModelsDevIndex(models={}, fetched_at=None, stale=False, error=None),
    )
    result = _call(monkeypatch, content="ok", usage=_usage(cost=0.25))
    assert result.actual_cost_usd == Decimal("0.25")
    assert result.cost_source is EnumCostSource.REPORTED
    assert result.savings_usd == Decimal("0")


def test_no_model_text_reaches_the_estimated_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reply and its reasoning state a made-up price and total; the cost is the
    arithmetic over the usage object and the kept price only."""
    result = _call(monkeypatch, content=MADE_UP_TEXT, usage=_usage())
    assert result.content is not None
    assert "$99999.99" in result.content  # the text did arrive, and is not the cost
    assert result.actual_cost_usd == ARITHMETIC
    assert result.cost_source is EnumCostSource.ESTIMATED
    assert (result.tokens_in, result.tokens_out) == (TOKENS_IN, TOKENS_OUT)
    assert result.provider_reported_cost_usd is None


def test_no_model_text_reaches_the_reported_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a reported charge, the cost is that usage field, not any figure the text states."""
    result = _call(monkeypatch, content=MADE_UP_TEXT, usage=_usage(cost=0.0042))
    assert result.actual_cost_usd == Decimal("0.0042")
    assert result.cost_source is EnumCostSource.REPORTED


def test_the_completed_event_carries_the_cost_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = call_handler.HandlerLlmDelegationCall()
    for usage, expected in (
        (_usage(cost=0.5), EnumCostSource.REPORTED),
        (_usage(), EnumCostSource.ESTIMATED),
    ):
        result = _call(monkeypatch, content="ok", usage=usage)
        event = handler._build_completed_event(_request(), result)
        assert event.cost_source is expected
        assert event.actual_cost_usd == result.actual_cost_usd
