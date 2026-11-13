# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""SEAM: canary request -> route -> node. The body the live chain canary sends is accepted.

Exemplar: tests/nodes/node_github_webhook_ingress_effect/test_http_runtime_profiles.py (the real caller
shape against the mounted route). The caller is omnibase_infra's chain canary (the C15 and C16
probes submit to POST /v1/workflows); either side changing its shape fails here.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from omnibase_infra.nodes.node_chain_canary_effect.handlers.handler_chain_canary import (
    HandlerChainCanary,
)
from omnibase_infra.nodes.node_chain_canary_effect.models.model_chain_canary_request import (
    ModelChainCanaryRequest,
)

from omnimarket.nodes.node_workflow_gateway_effect.handlers.http_app import create_app
from tests.nodes.node_workflow_gateway_effect.fakes import (
    API_KEY_HEADER,
    KEY_A,
    SKILL_TOPIC,
    build_gateway,
)

pytestmark = pytest.mark.unit


def _canary_body(workflow_type: str, probe_id: str) -> dict[str, object]:
    request = ModelChainCanaryRequest(
        correlation_id=uuid4(),
        probe_url="http://runtime",
        gateway_url="http://runtime",
        gateway_api_key_env="CANARY_KEY_ENV",
        gateway_workflow_type=workflow_type,
    )
    return HandlerChainCanary._build_gateway_body(request, probe_id)


async def test_the_canary_submission_is_acknowledged_and_published() -> None:
    gateway = build_gateway()
    probe_id = str(uuid4())
    # The canary's own workflow type; its body carries `source`, which only this type's schema allows.
    body = _canary_body("delegate-skill", probe_id)
    transport = httpx.ASGITransport(app=create_app(dependencies=gateway.deps))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://runtime"
    ) as client:
        response = await client.post(
            "/v1/workflows", json=body, headers={API_KEY_HEADER: KEY_A}
        )
    # The canary treats exactly 202 as accepted and reads workflow_id and correlation_id off it.
    assert response.status_code == 202, response.text
    ack = response.json()
    assert UUID(ack["workflow_id"])
    assert ack["correlation_id"] == probe_id
    assert len(gateway.publisher.published) == 1
    topic, _key, wire = gateway.publisher.published[0]
    assert wire["payload"]["correlation_id"] == probe_id
    assert topic == SKILL_TOPIC


async def test_a_refusal_is_a_json_object_the_canary_can_render() -> None:
    gateway = build_gateway()
    body = _canary_body("not-a-workflow", str(uuid4()))
    transport = httpx.ASGITransport(app=create_app(dependencies=gateway.deps))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://runtime"
    ) as client:
        response = await client.post(
            "/v1/workflows", json=body, headers={API_KEY_HEADER: KEY_A}
        )
    assert response.status_code == 400
    assert isinstance(response.json(), dict)
    assert gateway.publisher.published == []
