# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26566: a truncated answer on a tenant-overlay route escalates, never fails the run.

Recorded on the stability runtime (C16 run 46060727920, correlation
aaec1179-27e0-42f5-8162-3bd2d4274acd): ``HandlerInferenceIntent failed:
model=meta-llama/llama-3.1-8b-instruct ... error=API response truncated:
finish_reason=length`` three times on ``tier=tenant_overlay``, and the receipt
status was ``failed``. The same one model was asked again instead of the chain
moving to the next responder.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest
from omnibase_core.models.delegation.wire import ModelInferenceIntent

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.models.delegation.llm_cost_routing.model_llm_delegation_escalation_triggered_event import (
    ModelLlmDelegationEscalationTriggeredEvent,
)
from omnimarket.models.model_tenant_routing_overlay_backend import (
    ModelTenantRoutingOverlayBackend,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationResult,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_routing_intent import (
    ModelRoutingIntent,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.tenant_overlay_decision import (
    build_tenant_overlay_decision,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_dod_resolution import (
    ModelDodResolution,
)
from omnimarket.projection.tenant_isolation import HOUSE_TENANT_UUID

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_provider_quota_reader")]

BACKEND_REF = "byok-openrouter"
MODEL = "meta-llama/llama-3.1-8b-instruct"
URL = "https://openrouter.ai/api/v1/chat/completions"
TASK = "test"
# The recorded runtime line, verbatim from the error text of the failed call.
RECORDED_ERROR = "API response truncated: finish_reason=length"


def _request(cid: UUID, tenant_id: str) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Reply with one word.",
        task_type=TASK,
        correlation_id=cid,
        emitted_at=datetime.now(UTC),
        tenant_id=tenant_id,
    )


def _decision(request: ModelDelegationRequest, secret_ref: str | None) -> Any:
    tenant_id = str(request.tenant_id)
    return build_tenant_overlay_decision(
        request,
        task_type=TASK,
        overlay=ModelTenantRoutingOverlayBackend(
            tenant_id=tenant_id,
            task_type=TASK,
            backend_id=BACKEND_REF,
            provider="openrouter",
            endpoint_url=URL,
            model_name=MODEL,
            models=(MODEL,),
            secret_ref=secret_ref,
        ),
        estimated_tokens=10,
        dod_resolution=ModelDodResolution(),
        system_prompt="s",
        backend_id=uuid5(NAMESPACE_DNS, f"omninode.ai/backends/{BACKEND_REF}"),
    )


def _truncated(cid: UUID) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=cid,
        content="",
        model_used=MODEL,
        latency_ms=900,
        error_message=RECORDED_ERROR,
    )


def _drive(tenant_id: str, secret_ref: str | None) -> list[Any]:
    handler = HandlerDelegationWorkflow()
    cid = uuid4()
    request = _request(cid, tenant_id)
    handler.handle_delegation_request(request)
    handler.handle_routing_decision(_decision(request, secret_ref))
    return list(handler.handle_inference_response(_truncated(cid)))


def test_a_truncated_house_overlay_answer_escalates_to_the_next_responder(
    frontier_unconfigured_bifrost: None,
) -> None:
    events = _drive(str(HOUSE_TENANT_UUID), None)

    assert not [e for e in events if isinstance(e, ModelInferenceIntent)], (
        "the truncated model was asked again"
    )
    assert not any(isinstance(e, ModelDelegationResult) for e in events), (
        "a truncated answer ended the run instead of escalating"
    )
    assert len([e for e in events if isinstance(e, ModelRoutingIntent)]) == 1
    escalations = [
        e for e in events if isinstance(e, ModelLlmDelegationEscalationTriggeredEvent)
    ]
    assert len(escalations) == 1
    assert escalations[0].failure_class is EnumDelegationFailureClass.CONTEXT_TOO_LARGE


PROBE_TENANT = "87fadede-4a77-4205-890d-833d91a70464"


def test_a_truncated_answer_under_a_caller_cut_is_reasked_with_the_route_ceiling(
    frontier_unconfigured_bifrost: None,
) -> None:
    """RED at the base: the canary tenant's one model was re-asked with the same cut."""
    handler = HandlerDelegationWorkflow()
    cid = uuid4()
    request = _request(cid, PROBE_TENANT).model_copy(update={"max_tokens": 32})
    handler.handle_delegation_request(request)
    first = handler.handle_routing_decision(_decision(request, "cred_probe_ref"))
    sent = [e for e in first if isinstance(e, ModelInferenceIntent)]
    assert sent[0].max_tokens == 32
    events = list(handler.handle_inference_response(_truncated(cid)))
    reissued = [e for e in events if isinstance(e, ModelInferenceIntent)]
    assert len(reissued) == 1
    assert reissued[0].max_tokens == handler.workflows[cid].routing_decision.max_tokens
    assert reissued[0].max_tokens > 32
    # A second truncation at the ceiling is not lifted again.
    again = list(handler.handle_inference_response(_truncated(cid)))
    assert not [
        e
        for e in again
        if isinstance(e, ModelInferenceIntent)
        and e.max_tokens != reissued[0].max_tokens
    ]
