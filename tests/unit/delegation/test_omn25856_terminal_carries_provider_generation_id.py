# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-25856: the delegation terminal names the provider's own response id.

The inference effect already reports the provider response id as
``llm_call_id`` (an OpenRouter ``gen-...``, an OpenAI ``chatcmpl-...``). The
orchestrator kept it on workflow state and dropped it on the way to the
terminal, so a gateway receipt could not be matched to the provider's record.
When the provider returned no id the effect substitutes its own uuid; that is
not a provider id and must not be carried.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest

from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
    _provider_generation_id,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationCompleted,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)

pytestmark = [pytest.mark.unit]

_MODEL = "vendor/served:free"


def _completed_terminal(llm_call_id: str) -> ModelDelegationCompleted:
    cid = uuid4()
    handler = HandlerDelegationWorkflow()
    handler.handle_delegation_request(
        ModelDelegationRequest(
            prompt="Say ok.",
            task_type="test",  # type: ignore[arg-type]
            correlation_id=cid,
            emitted_at=datetime.now(UTC),
            tenant_id="acme",
        )
    )
    handler.handle_routing_decision(
        ModelRoutingDecision(
            correlation_id=cid,
            task_type="test",
            selected_model=_MODEL,
            selected_backend_id=uuid5(NAMESPACE_DNS, "omninode.ai/backends/x"),
            endpoint_url="https://openrouter.ai/api/v1/chat/completions",
            cost_tier="low",
            tier_name="free_cloud",
            max_context_tokens=65536,
            max_tokens=4096,
            system_prompt="You are a test generation assistant.",
            rationale="routed",
        )
    )
    handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=cid,
            content="### ANSWER\ndef test_x():\n    assert True",
            model_used=_MODEL,
            llm_call_id=llm_call_id,
            latency_ms=10,
            prompt_tokens=3,
            completion_tokens=4,
            total_tokens=7,
        )
    )
    events = handler.handle_gate_result(
        ModelQualityGateResult(
            correlation_id=cid,
            passed=True,
            quality_score=0.95,
            failure_reasons=(),
            fallback_recommended=False,
        )
    )
    completed = [e for e in events if isinstance(e, ModelDelegationCompleted)]
    assert len(completed) == 1, events
    return completed[0]


# A core older than the one declaring ``provider_generation_id`` must still build
# the terminal (the orchestrator omits the stamp); on a core that declares it the
# terminal must carry the id. Both are asserted from the same declaration.
_CORE_DECLARES = "provider_generation_id" in ModelDelegationCompleted.model_fields


def test_completed_terminal_carries_the_provider_response_id() -> None:
    terminal = _completed_terminal("gen-1764-abcdef")
    expected = "gen-1764-abcdef" if _CORE_DECLARES else None
    assert getattr(terminal, "provider_generation_id", None) == expected


def test_effect_fallback_uuid_is_not_a_provider_generation_id() -> None:
    fallback: UUID = uuid4()
    terminal = _completed_terminal(str(fallback))
    assert getattr(terminal, "provider_generation_id", None) is None


@pytest.mark.parametrize(
    ("llm_call_id", "expected"),
    [
        ("gen-1764-abcdef", "gen-1764-abcdef"),
        ("  chatcmpl-9x  ", "chatcmpl-9x"),
        (str(uuid4()), None),
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_provider_generation_id_filter(
    llm_call_id: str | None, expected: str | None
) -> None:
    assert _provider_generation_id(llm_call_id) == expected
