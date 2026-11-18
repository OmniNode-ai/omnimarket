# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory fakes for the dev signed-in read node tests (OMN-24345).

``FakeKeycloakAndApi`` enforces the real wire rules: the token endpoint requires the password grant's form fields and
answers only for the right client, user and password; the read route requires the user's bearer, a correlation id and a
limit, and answers 200 with a data state, 401 for an absent or wrong bearer. It records every request so a test can prove
which values moved on the wire. The settings store is the identity node's own fake.
"""

from __future__ import annotations

from typing import Any

from omnimarket.models.model_dev_identity_http import (
    ModelIdentityHttpRequest,
    ModelIdentityHttpResponse,
)
from tests.nodes.node_dev_identity_provision_effect.fakes import FakeSettingStore

__all__ = ["FakeKeycloakAndApi", "FakeSettingStore"]


class FakeKeycloakAndApi:
    """A stateful Keycloak token endpoint and onex-api read route that records every request."""

    def __init__(
        self,
        *,
        user_client_id: str = "omnidash-spa",
        username: str = "dev-probe-user",
        password: str = "Dp1!stored",
        user_token: str = "user-access-token",
        issued_token: str | None = None,
        route_path: str = "/v1/projections/hook-events/by-correlation",
        data_state: str = "not_found",
        fail_user_token: bool = False,
        read_status: int = 200,
        read_body: Any = "default",
        raise_connection_error_on: str | None = None,
    ) -> None:
        self.user_client_id = user_client_id
        self.username = username
        self.password = password
        self.user_token = user_token
        self.issued_token = issued_token if issued_token is not None else user_token
        self.route_path = route_path
        self.data_state = data_state
        self.fail_user_token = fail_user_token
        self.read_status = read_status
        self.read_body = read_body
        self.raise_connection_error_on = raise_connection_error_on
        self.sent: list[ModelIdentityHttpRequest] = []
        self.grant_forms: list[dict[str, str]] = []

    def _answer(self, status: int, body: Any = None) -> ModelIdentityHttpResponse:
        return ModelIdentityHttpResponse(status=status, headers={}, json_body=body)

    async def send(
        self, request: ModelIdentityHttpRequest
    ) -> ModelIdentityHttpResponse:
        if self.raise_connection_error_on is not None and (
            self.raise_connection_error_on in request.url
        ):
            raise ConnectionError("ConnectionError")
        self.sent.append(request)

        if "/protocol/openid-connect/token" in request.url:
            if request.form is None:
                return self._answer(400)
            form = {k: v.get_secret_value() for k, v in request.form.items()}
            self.grant_forms.append(form)
            if (
                self.fail_user_token
                or form.get("grant_type") != "password"
                or form.get("client_id") != self.user_client_id
                or form.get("username") != self.username
                or form.get("password") != self.password
            ):
                return self._answer(401)
            return self._answer(200, {"access_token": self.issued_token})

        if request.method == "GET" and request.url.endswith(self.route_path):
            if (
                request.bearer is None
                or request.bearer.get_secret_value() != self.user_token
            ):
                return self._answer(401, {"detail": "Unauthorized"})
            if not request.query.get("correlation_id") or not request.query.get(
                "limit"
            ):
                return self._answer(422)
            if self.read_status != 200:
                return self._answer(self.read_status)
            if self.read_body != "default":
                return self._answer(200, self.read_body)
            return self._answer(
                200,
                {
                    "correlation_id": request.query["correlation_id"],
                    "projection": "hook_events",
                    "data_state": self.data_state,
                    "tenant_scope": "dev-probe-tenant",
                    "count": 0,
                    "rows": [],
                    "generated_at": "2026-11-14T00:00:00Z",
                },
            )
        return self._answer(404)
