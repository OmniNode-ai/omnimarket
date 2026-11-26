# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A delegation-inference submission can carry named tests through the gateway.

Exemplar: tests/nodes/node_workflow_gateway_effect/test_golden_chain_workflow_gateway.py
(submit through the mounted routes and read the published wire).

The gateway refused ``named_tests`` with 400 "Additional properties are not
allowed" because the catalog entry declared neither the key in its payload
schema nor in its wire keys, so a code answer never reached the gate with a
test run to judge it on.
"""

from __future__ import annotations

import httpx
import pytest

from omnimarket.nodes.node_delegation_orchestrator.models import ModelDelegationRequest
from omnimarket.nodes.node_workflow_gateway_effect.handlers.http_app import create_app
from tests.nodes.node_workflow_gateway_effect.fakes import (
    API_KEY_HEADER,
    KEY_A,
    Gateway,
    build_gateway,
)

pytestmark = pytest.mark.unit

NAMED_TESTS = {
    "answer_path": "answer.py",
    "test_ids": ["test_answer.py::test_f"],
    "test_files": [
        {
            "path": "test_answer.py",
            "content": "from answer import f\n\ndef test_f():\n    assert f() == 1\n",
        }
    ],
}
PAYLOAD = {
    "prompt": "write f returning 1",
    "task_type": "code_generation",
    "named_tests": NAMED_TESTS,
}


def _client(gateway: Gateway) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(dependencies=gateway.deps))
    return httpx.AsyncClient(transport=transport, base_url="http://runtime")


async def _submit(gateway: Gateway, payload: dict[str, object]) -> httpx.Response:
    async with _client(gateway) as client:
        return await client.post(
            "/v1/workflows",
            headers={API_KEY_HEADER: KEY_A},
            json={"workflow_type": "delegation-inference", "payload": payload},
        )


async def test_named_tests_are_accepted_and_reach_the_command_topic() -> None:
    gateway = build_gateway()
    response = await _submit(gateway, PAYLOAD)
    assert response.status_code == 202, response.text
    assert len(gateway.publisher.published) == 1
    wire_payload = gateway.publisher.published[0][2]["payload"]
    assert wire_payload["named_tests"] == NAMED_TESTS
    # SEAM: the real consumer model accepts exactly this payload.
    request = ModelDelegationRequest.model_validate(wire_payload)
    assert request.named_tests is not None
    assert request.named_tests.test_ids == ("test_answer.py::test_f",)


async def test_malformed_named_tests_are_still_refused() -> None:
    gateway = build_gateway()
    response = await _submit(
        gateway,
        {**PAYLOAD, "named_tests": {**NAMED_TESTS, "unexpected": 1}},
    )
    assert response.status_code == 400
    assert gateway.publisher.published == []


async def test_an_undeclared_key_is_still_refused() -> None:
    gateway = build_gateway()
    response = await _submit(gateway, {**PAYLOAD, "other_key": 1})
    assert response.status_code == 400
    assert gateway.publisher.published == []
