# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hosted overlay -> wire decision -> orchestrator inference retry (OMN-21217)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from omnimarket.inference.provider_quota_state import StaticProviderQuotaReader
from omnimarket.nodes.node_delegation_orchestrator.handlers import (
    handler_delegation_workflow as workflow_module,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_routing_intent import (
    ModelRoutingIntent,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
    handler_delegation_routing as routing,
)
from omnimarket.routing.customer_key_terminus import EnumDelegationSurface
from omnimarket.routing.tenant_overlay_resolver import resolve_tenant_overlay

_TENANT = "acme"
_KEY = "tenant-acme.openrouter.key-one"
_BACKEND = "openrouter-byok"
_ENDPOINT = "https://tenant-provider.example/v1/chat/completions"


class _OverlayReader:
    def __init__(self) -> None:
        self.rows = {
            "delegation_routing_tenant_overlay": [
                {
                    "tenant_id": _TENANT,
                    "task_type": "code_generation",
                    "backend_id": _BACKEND,
                    "endpoint_url": _ENDPOINT,
                    "provider": "openrouter",
                    # A stale route must not become a third model candidate.
                    "model_name": "outside-the-key-list",
                    "secret_ref": _KEY,
                }
            ],
            "tenant_inference_credentials": [
                {
                    "tenant_id": _TENANT,
                    "provider": "openrouter",
                    "api_key_ref": _KEY,
                    "models": ["chosen-a", "chosen-b"],
                    "revoked_at": None,
                    "expires_at": None,
                },
                {
                    "tenant_id": "other-tenant",
                    "provider": "openrouter",
                    "api_key_ref": "tenant-other.openrouter.key-two",
                    "models": ["outside-the-key-list"],
                    "revoked_at": None,
                    "expires_at": None,
                },
            ],
        }

    def query(self, table, filters=None):
        return [
            dict(row)
            for row in self.rows.get(table, [])
            if all(row.get(key) == value for key, value in (filters or {}).items())
        ]


def _route(request, reader):
    overlay = resolve_tenant_overlay(
        reader,
        tenant_id=request.tenant_id,
        task_type=request.task_type,
        selector_key=str(request.correlation_id),
    )
    assert overlay is not None
    decision = routing.delta(
        request, tenant_overlay=overlay, surface=EnumDelegationSurface.CLOUD
    )
    # Exercise the serialized handoff, not a local-only attribute.
    return type(decision).model_validate_json(decision.model_dump_json())


def _response(handler, request, model, *, failed):
    fields = ModelInferenceResponseData.model_fields
    data = {
        "correlation_id": request.correlation_id,
        "content": "" if failed else "The requested implementation is complete.",
        "model_used": model,
        "latency_ms": 1,
        "prompt_tokens": 2,
        "completion_tokens": 0 if failed else 3,
        "total_tokens": 2 if failed else 5,
        "error_message": "connection timeout" if failed else "",
    }
    if "inference_attempt_id" in fields:
        data["inference_attempt_id"] = handler.workflows[
            request.correlation_id
        ].current_inference_attempt_id
    return ModelInferenceResponseData(**data)


@pytest.mark.parametrize("second_fails", [False, True])
def test_hosted_retry_walks_only_the_selected_keys_ordered_models(
    monkeypatch, tmp_path, second_fails
):
    """a timeout reaches b on the same key; exhaustion cannot reach c/house."""
    monkeypatch.setenv("OMNI_HOME", str(tmp_path))
    # Same-model reissues have no budget. Walking a different chosen model
    # must not be confused with reissuing the failing one.
    monkeypatch.setattr(workflow_module, "byok_backend_max_retries", lambda _: 0)
    reader = _OverlayReader()
    request = ModelDelegationRequest(
        correlation_id=uuid4(),
        task_type="code_generation",
        prompt="Implement the requested pure function and explain the result.",
        tenant_id=_TENANT,
        emitted_at=datetime.now(UTC),
    )
    handler = workflow_module.HandlerDelegationWorkflow(
        workflows={}, quota_reader=StaticProviderQuotaReader()
    )
    handler.handle_delegation_request(request)
    initial = _route(request, reader)
    calls = list(handler.handle_routing_decision(initial))
    assert len(calls) == 1
    assert calls[0].model == "chosen-a"

    # The retry must retain the key/list snapshot already sent over the bus.
    # Re-reading mutable overlay/catalogue state would widen the route here.
    reader.rows["tenant_inference_credentials"][0]["models"] = ["outside-the-key-list"]
    reader.rows["delegation_routing_tenant_overlay"][0]["secret_ref"] = (
        "house.provider.key"
    )
    events = handler.handle_inference_response(
        _response(handler, request, "chosen-a", failed=True)
    )
    for event in events:
        if isinstance(event, ModelRoutingIntent):
            calls.extend(handler.handle_routing_decision(_route(event.payload, reader)))
        elif hasattr(event, "base_url") and hasattr(event, "model"):
            calls.append(event)

    assert [call.model for call in calls] == ["chosen-a", "chosen-b"]
    assert all(call.api_key_ref == _KEY for call in calls)
    assert all(call.base_url == _ENDPOINT for call in calls)
    live = handler.workflows[request.correlation_id].routing_decision
    assert live is not None
    assert live.selected_backend_id == initial.selected_backend_id
    assert live.selected_backend_ref == _BACKEND

    events = handler.handle_inference_response(
        _response(handler, request, "chosen-b", failed=second_fails)
    )
    assert not any(isinstance(event, ModelRoutingIntent) for event in events)
    assert not any(hasattr(event, "base_url") for event in events)
    assert [
        attempt.model_used
        for attempt in handler.workflows[request.correlation_id].escalation_history
    ] == (["chosen-a", "chosen-b"] if second_fails else ["chosen-a"])
