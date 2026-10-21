# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21260: a call on a customer's own OpenRouter key is priced by its model.

A run on the paid qwen/qwen3.8-27b was booked at the routing tier's rate: 0.0
on cheap_frontier, recorded as measured with the full baseline price as
savings, and a flat 2.00 per 1M on cheap_cloud. These tests pin the model's
own price on either tier, an unpriced model recorded as unmeasured with a null
cost and no savings, and house calls left at the tier rate.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from omnimarket.enums.enum_cost_basis import EnumCostBasis
from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
    delegate_skill_terminal_from_response,
)
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
    ModelDelegationEventProjectionRow,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    _response_from_result,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_llm_delegation_call import (
    _compute_cost,
    _cost_usage_source,
)

pytestmark = pytest.mark.unit

OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
UNPRICED_MODEL_ID = "example/unpriced-model-omn21260"
PRICED_MODEL_ID = "qwen/qwen3.8-27b"


def _response(model_id: str, source: str, cost: float) -> ModelDelegateSkillResponse:
    request = ModelDelegateSkillRequest(
        prompt="what is 2+2",
        task_type="research",
        source="claude-code",
        correlation_id=uuid4(),
    )
    result: dict[str, object] = {
        "status": "completed",
        "content": "4",
        "quality_gate_passed": True,
        "input_tokens": 289,
        "output_tokens": 1679,
        "model_cloud_baseline": "claude-sonnet-5-5",
        "attempts": [
            {
                "tier": "cheap_frontier",
                "backend_id": "byok-openrouter",
                "model_id": model_id,
                "quality_gate_passed": True,
                "usage_source": source,
                "cost_usd": cost,
                "acceptance_decision": "accept",
            }
        ],
        "cost_usd": cost,
    }
    return _response_from_result(
        request,
        result,
        tenant_id=None,
        queue_wait_ms=None,
        execution_duration_ms=10,
        budget_evidence=None,
    )


@pytest.mark.parametrize("model_tier", ["cheap_frontier", "cheap_cloud"])
def test_a_paid_openrouter_model_on_the_customer_key_records_its_real_cost(
    model_tier: str,
) -> None:
    actual, _opus, _savings, cost_basis = _compute_cost(
        PRICED_MODEL_ID,
        289,
        1679,
        model_tier=model_tier,
        endpoint_url=OPENROUTER,
        provider="byok-openrouter",
    )

    assert actual == Decimal("0.00376316")
    assert actual > 0
    assert cost_basis is EnumCostBasis.CLOUD_API_COST
    assert _cost_usage_source(cost_basis) is EnumUsageSource.MEASURED


def test_an_unpriced_model_on_the_customer_key_is_not_booked_at_the_tier_rate() -> None:
    actual, _opus, savings, cost_basis = _compute_cost(
        UNPRICED_MODEL_ID,
        289,
        1679,
        model_tier="cheap_cloud",
        endpoint_url=OPENROUTER,
        provider="byok-openrouter",
    )

    assert actual == Decimal("0")
    assert savings == Decimal("0")
    assert cost_basis is EnumCostBasis.UNKNOWN
    assert _cost_usage_source(cost_basis) is EnumUsageSource.UNKNOWN


def test_a_house_call_keeps_the_tier_rate() -> None:
    actual, _opus, _savings, _cost_basis = _compute_cost(
        PRICED_MODEL_ID,
        289,
        1679,
        model_tier="cheap_cloud",
        endpoint_url=OPENROUTER,
        provider="unknown",
    )

    assert actual == Decimal("0.003936")


def test_an_unpriced_customer_run_is_not_measured_and_counts_no_savings() -> None:
    response = _response(UNPRICED_MODEL_ID, "unknown", 0.0)

    assert response.metrics.cost_measured is False
    assert response.metrics.cost_savings_usd is None


def test_a_priced_customer_run_is_measured_and_keeps_its_savings() -> None:
    response = _response(PRICED_MODEL_ID, "measured", 0.00376316)
    savings = response.metrics.cost_savings_usd

    assert response.metrics.cost_measured is True
    assert savings is not None
    assert savings > 0


def _stored_row(
    response: ModelDelegateSkillResponse,
) -> ModelDelegationEventProjectionRow:
    terminal = ModelDelegateSkillTerminalProjection.from_payload(
        delegate_skill_terminal_from_response(response).model_dump(mode="json")
    )
    return ModelDelegationEventProjectionRow.from_terminal_event(terminal)


def test_an_unpriced_customer_run_is_stored_with_a_null_cost() -> None:
    row = _stored_row(_response(UNPRICED_MODEL_ID, "unknown", 0.0))

    assert row.cost_usd is None
    assert row.cost_savings_usd == Decimal("0")


def test_a_priced_customer_run_is_stored_with_its_cost() -> None:
    row = _stored_row(_response(PRICED_MODEL_ID, "measured", 0.00376316))

    assert row.cost_usd == Decimal("0.00376316")
    assert row.cost_savings_usd is not None
    assert row.cost_savings_usd > 0
