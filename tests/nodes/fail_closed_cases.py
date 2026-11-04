# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC2 cases shared by the three nodes: refuse before any adapter call."""

from __future__ import annotations

from typing import Any

from omnimarket.handlers.inference_credentials_http.bindings import (
    reset_inference_credential_ports,
)
from tests.nodes.inference_credential_http_support import (
    GOOD_BODY,
    IDEMPOTENCY,
    ROUTE,
    TENANT_A,
    TENANT_B,
    World,
    fake_row,
)

#: (method, path, json) for each of the three routes.
REQUESTS: dict[str, tuple[str, str, dict[str, Any] | None]] = {
    "create": ("POST", ROUTE, GOOD_BODY),
    "list": ("GET", ROUTE, None),
    "revoke": ("DELETE", f"{ROUTE}/cred_{TENANT_B}_openai_victim", None),
}


def send(world: World, route: str, headers: dict[str, str]) -> Any:
    method, path, body = REQUESTS[route]
    return world.client.request(
        method, path, json=body, headers={**IDEMPOTENCY, **headers}
    )


def assert_unauthenticated_refused(route: str) -> None:
    world = World(rows=[fake_row(TENANT_B, "victim")])
    for headers in ({}, {"x-api-key": "unknown-key"}):
        response = send(world, route, headers)
        assert response.status_code == 401, response.text
        assert response.json()["detail"] == "Unauthorized"
        assert response.json()["code"] in {
            "auth.credential.absent",
            "auth.credential.invalid",
        }
    bearer = send(world, route, {"authorization": "Bearer not-provisioned"})
    assert bearer.status_code == 401
    assert bearer.json() == {"detail": "tenant_not_provisioned"}
    assert world.adapter_calls() == 0


def assert_unbound_refused(route: str) -> None:
    world = World(rows=[fake_row(TENANT_B, "victim")])
    reset_inference_credential_ports()
    for headers in ({}, world.as_tenant_a(), world.as_tenant_b()):
        response = send(world, route, headers)
        assert response.status_code == 503, response.text
        assert response.json() == {"detail": "OIDC authentication is not configured"}
    assert world.adapter_calls() == 0


def assert_cross_tenant_revoke_refused() -> None:
    victim = fake_row(TENANT_B, "victim")
    world = World(rows=[victim, fake_row(TENANT_A, "mine")])
    response = send(world, "revoke", world.as_tenant_a())
    assert response.status_code == 404, response.text
    assert world.store.calls == []
    assert world.bus.published == []
