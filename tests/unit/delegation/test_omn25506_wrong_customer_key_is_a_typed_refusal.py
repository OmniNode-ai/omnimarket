# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-25506: a customer's wrong provider key ends as a typed key refusal.

A key the register call accepted but the provider refuses (HTTP 401 or 403) used
to end the run with ``no_higher_tier_available`` and the provider's own text, so
the customer could not tell their key was wrong. It now ends with the
``provider_key_rejected`` reason of the ``EnumCustomerKeyRefusalReason`` family,
never climbs to another tier or a house key, and states no provider text.
Other provider errors keep their class.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest
from omnibase_core.models.delegation.wire import ModelInferenceIntent

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

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("stub_provider_quota_reader")]

URL = "https://api.anthropic.com/v1/chat/completions"
BACKEND_REF = "byok-anthropic"
MODEL = "claude-sonnet-5-5"
TASK = "test"
CUSTOMER_TENANT = "a0ea038d-8538-494c-b004-4ceaf9677fac"
# A reference, not a secret (see tenant_credential_ref).
CUSTOMER_REF = f"cred_{CUSTOMER_TENANT}_anthropic_911110ccc89c4b6db98203024ba23371"
PROVIDER_BODY = "Invalid Anthropic API Key"
KEY_REJECTED = "provider_key_rejected"


def _message(status: int) -> str:
    return (
        f"provider HTTP {status} Unauthorized for {URL}; "
        f'response_body={{"error":{{"message":"{PROVIDER_BODY}"}}}}'
    )


def _request(cid: UUID) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Write a test for a function that adds two numbers.",
        task_type=TASK,
        correlation_id=cid,
        emitted_at=datetime.now(UTC),
        tenant_id=CUSTOMER_TENANT,
    )


def _run_to_the_end(error_message: str) -> tuple[list[Any], ModelDelegationResult]:
    """Feed the same provider error until the run stops asking the provider."""
    handler = HandlerDelegationWorkflow()
    cid = uuid4()
    handler.handle_delegation_request(_request(cid))
    handler.handle_routing_decision(
        build_tenant_overlay_decision(
            _request(cid),
            task_type=TASK,
            overlay=ModelTenantRoutingOverlayBackend(
                tenant_id=CUSTOMER_TENANT,
                task_type=TASK,
                backend_id=BACKEND_REF,
                provider="anthropic",
                endpoint_url=URL,
                model_name=MODEL,
                models=(MODEL,),
                secret_ref=CUSTOMER_REF,
            ),
            estimated_tokens=10,
            dod_resolution=ModelDodResolution(),
            system_prompt="s",
            backend_id=uuid5(NAMESPACE_DNS, f"omninode.ai/backends/{BACKEND_REF}"),
        )
    )
    seen: list[Any] = []
    for _ in range(10):
        events = list(
            handler.handle_inference_response(
                ModelInferenceResponseData(
                    correlation_id=cid,
                    content="",
                    model_used=MODEL,
                    latency_ms=10,
                    error_message=error_message,
                )
            )
        )
        seen.extend(events)
        if not any(isinstance(e, ModelInferenceIntent) for e in events):
            break
    results = [e for e in seen if isinstance(e, ModelDelegationResult)]
    assert len(results) == 1, [type(e).__name__ for e in seen]
    return seen, results[0]


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_customer_key_is_a_typed_refusal_not_a_ladder_end(
    status: int, frontier_unconfigured_bifrost: None
) -> None:
    seen, result = _run_to_the_end(_message(status))

    assert not [e for e in seen if isinstance(e, ModelRoutingIntent)], (
        "a refused customer key was escalated to another tier"
    )
    assert result.terminal_failure_reason == KEY_REJECTED
    assert "no_higher_tier_available" not in str(result.terminal_failure_reason)
    texts = [result.failure_reason or ""] + [
        r for h in result.escalation_history for r in h.get("failure_reasons", ())
    ]
    for text in texts:
        assert PROVIDER_BODY not in text
        assert URL not in text


def test_another_provider_error_keeps_its_class(
    frontier_unconfigured_bifrost: None,
) -> None:
    """Control: a 503 on the customer's route is not a key refusal."""
    _, result = _run_to_the_end(_message(503).replace("Unauthorized", "Unavailable"))
    assert result.terminal_failure_reason != KEY_REJECTED
