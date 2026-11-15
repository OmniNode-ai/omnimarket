# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stored delegation row carries a cost above zero for a declared GLM model.

R8 reads the organic row's ``cost_usd``. This composes the path that row takes
for a call on the ``byok-glm-general`` backend: the catalogue price, the call
effect's cost, the attempt, the delegate-skill terminal and the stored
``ModelDelegationEventProjectionRow``. A declared model (``glm-5.3-flash``)
stores a cost above zero; an undeclared model stores NULL, which R8 reads as 0.
With the base catalogue (no ``model_prices`` row) the declared model stores NULL
too, so this file fails at the merge base.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

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

_GENERAL_API_URL = "https://api.z.ai/api/paas/v4/chat/completions"
_BACKEND = "byok-glm-general"
_DECLARED_MODEL = "glm-5.3-flash"
_UNDECLARED_MODEL = "glm-5.3-flashx"
_INPUT_TOKENS = 289
_OUTPUT_TOKENS = 1679


def _stored_row(model_id: str) -> ModelDelegationEventProjectionRow:
    actual, _opus, _savings, basis = _compute_cost(
        model_id,
        _INPUT_TOKENS,
        _OUTPUT_TOKENS,
        model_tier="tenant_byok",
        endpoint_url=_GENERAL_API_URL,
        provider=_BACKEND,
    )
    source = _cost_usage_source(basis).value
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
        "input_tokens": _INPUT_TOKENS,
        "output_tokens": _OUTPUT_TOKENS,
        "model_cloud_baseline": "claude-sonnet-5-5",
        "attempts": [
            {
                "tier": "tenant_byok",
                "backend_id": _BACKEND,
                "model_id": model_id,
                "quality_gate_passed": True,
                "usage_source": source,
                "cost_usd": float(actual),
                "acceptance_decision": "accept",
            }
        ],
        "cost_usd": float(actual),
    }
    response: ModelDelegateSkillResponse = _response_from_result(
        request,
        result,
        tenant_id=None,
        queue_wait_ms=None,
        execution_duration_ms=10,
        budget_evidence=None,
    )
    terminal = ModelDelegateSkillTerminalProjection.from_payload(
        delegate_skill_terminal_from_response(response).model_dump(mode="json")
    )
    return ModelDelegationEventProjectionRow.from_terminal_event(terminal)


def test_a_declared_glm_model_is_stored_with_a_cost_above_zero() -> None:
    row = _stored_row(_DECLARED_MODEL)

    # 289 in at 0.15 and 1679 out at 0.50 per 1M.
    assert row.cost_usd == Decimal("0.00088285")
    assert row.cost_usd is not None
    assert row.cost_usd > 0


def test_an_undeclared_glm_model_is_stored_with_a_null_cost() -> None:
    row = _stored_row(_UNDECLARED_MODEL)

    assert row.cost_usd is None
