# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory fakes for the dev identity provision node tests (OMN-24115).

``FakeKeycloakAndApi`` is a stateful Keycloak fake that enforces the real wire rules: an unknown path 404s (the provision
route of the retired onex-api is one: a handler that calls it fails), the token endpoint requires the right form fields
and answers only for the right client, secret or password, and the admin endpoints require the admin bearer and reject a
user body without username, enabled or credentials. It records every request it accepted or refused, so a test can
prove which values moved on the wire. ``FakeTenantProvisioner`` is the runtime's tenant provision command in memory: it
records each request it was given and answers as a test says. ``FakeSettingStore`` and ``FakeKeyGenerator`` are the store and generator seams;
the generator holds the value it generated in memory so a test can prove it never appears in a result, a repr or a log.
"""

from __future__ import annotations

from typing import Any

from pydantic import SecretStr

from omnimarket.models.model_dev_identity_http import (
    ModelIdentityHttpRequest,
    ModelIdentityHttpResponse,
)
from omnimarket.models.secret_store.model_generate_key_result import (
    ModelGenerateKeyResult,
)
from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
)
from omnimarket.nodes.node_dev_identity_provision_effect.models.model_tenant_provision_wire import (
    ModelTenantProvisionAnswer,
    ModelTenantProvisionRequest,
)
from omnimarket.nodes.node_dev_identity_provision_effect.protocols import (
    TenantProvisionUnavailableError,
)


class FakeSettingStore:
    """A settings store that answers the secrets a test seeds, and records what it was asked."""

    def __init__(self, secrets: dict[tuple[str, str], str] | None = None) -> None:
        self._secrets: dict[tuple[str, str], str] = secrets or {}
        self.calls: list[tuple[str, str]] = []

    def set(self, folder: str, key: str, value: str) -> None:
        self._secrets[(folder, key)] = value

    async def read(self, folder: str, name: str) -> SecretStr | None:
        self.calls.append((folder, name))
        value = self._secrets.get((folder, name))
        return SecretStr(value) if value is not None else None


class FakeKeyGenerator:
    """The seed node's generate-key operation in memory: the value is held here, never returned."""

    def __init__(self, outcome: str = "created", value: str = "Dp1!generated") -> None:
        self.outcome = EnumSecretStoreOutcome(outcome)
        self.value = value
        self.calls: list[str] = []

    async def generate(self, name: str) -> ModelGenerateKeyResult:
        self.calls.append(name)
        return ModelGenerateKeyResult(
            name=name,
            reference=f"secret://onex/shared/env/{name.upper()}",
            outcome=self.outcome,
            key_length=len(self.value)
            if self.outcome is EnumSecretStoreOutcome.CREATED
            else None,
            folder="/shared/env",
            key=name.upper(),
            detail=None,
            correlation_id=None,
        )


class FakeKeycloakAndApi:
    """A stateful Keycloak that speaks the real wire rules and records every request."""

    def __init__(
        self,
        *,
        admin_client_id: str = "admin-client",
        admin_secret: str = "admin-secret-value",
        user_client_id: str = "public-client",
        username: str = "dev-probe-user",
        password: str = "Dp1!generated",
        admin_token: str = "admin-access-token",
        user_token: str = "user-access-token",
        fail_admin_token: bool = False,
        fail_user_token: bool = False,
        fail_user_create: bool = False,
        fail_user_delete: bool = False,
        raise_connection_error: bool = False,
        raise_connection_error_on: str | None = None,
    ) -> None:
        self.admin_client_id = admin_client_id
        self.admin_secret = admin_secret
        self.user_client_id = user_client_id
        self.username = username
        self.password = password
        self.admin_token = admin_token
        self.user_token = user_token
        self.fail_admin_token = fail_admin_token
        self.fail_user_token = fail_user_token
        self.fail_user_create = fail_user_create
        self.fail_user_delete = fail_user_delete
        self.raise_connection_error = raise_connection_error
        self.raise_connection_error_on = raise_connection_error_on
        self.users: dict[str, dict[str, Any]] = {}
        self.user_id_counter = 0
        self.call_log: list[str] = []
        self.sent: list[ModelIdentityHttpRequest] = []
        self.create_bodies: list[dict[str, Any]] = []
        self.reset_bodies: list[dict[str, Any]] = []
        self.deleted_ids: list[str] = []
        self.grant_forms: list[dict[str, str]] = []
        self.roles: dict[str, str] = {"dashboard-reader": "role-id-reader"}
        self.role_mappings: list[tuple[str, list[dict[str, Any]]]] = []

    def _answer(self, status: int, body: Any = None) -> ModelIdentityHttpResponse:
        return ModelIdentityHttpResponse(status=status, headers={}, json_body=body)

    async def send(
        self, request: ModelIdentityHttpRequest
    ) -> ModelIdentityHttpResponse:
        if self.raise_connection_error and (
            self.raise_connection_error_on is None
            or self.raise_connection_error_on in request.url
        ):
            raise ConnectionError("ConnectionError")

        self.call_log.append(f"{request.method} {request.url}")
        self.sent.append(request)

        # Token endpoint: the right form fields, the right client, secret or password.
        if "/protocol/openid-connect/token" in request.url:
            if request.form is None:
                return self._answer(400)
            form = {k: v.get_secret_value() for k, v in request.form.items()}
            self.grant_forms.append(form)
            grant_type = form.get("grant_type", "")

            if grant_type == "client_credentials":
                if self.fail_admin_token:
                    return self._answer(401)
                if form.get("client_id") != self.admin_client_id:
                    return self._answer(401)
                if form.get("client_secret") != self.admin_secret:
                    return self._answer(401)
                return self._answer(200, {"access_token": self.admin_token})

            if grant_type == "password":
                if self.fail_user_token:
                    return self._answer(401)
                if form.get("client_id") != self.user_client_id:
                    return self._answer(401)
                if form.get("username") != self.username:
                    return self._answer(401)
                if form.get("password") != self.password:
                    return self._answer(401)
                return self._answer(200, {"access_token": self.user_token})

            return self._answer(400)

        # Admin realm role read by name (OMN-25103).
        if (
            request.method == "GET"
            and "/admin/realms/" in request.url
            and "/roles/" in request.url
        ):
            if not self._is_admin(request):
                return self._answer(403)
            role = request.url.rstrip("/").rsplit("/", 1)[-1]
            if role in self.roles:
                return self._answer(200, {"id": self.roles[role], "name": role})
            return self._answer(404)

        # Admin realm role mapping for one user (OMN-25103).
        if request.method == "POST" and request.url.endswith("/role-mappings/realm"):
            if not self._is_admin(request):
                return self._answer(403)
            user_id = request.url.split("/users/")[1].split("/")[0]
            self.role_mappings.append((user_id, list(request.json_body or [])))
            return self._answer(204)

        # Admin users list.
        if (
            request.method == "GET"
            and "/admin/realms/" in request.url
            and request.url.rstrip("/").endswith("/users")
        ):
            if not self._is_admin(request):
                return self._answer(403)
            username = request.query.get("username", "")
            user = self.users.get(username)
            if user is not None:
                return self._answer(200, [user])
            return self._answer(200, [])

        # Admin user delete: the user's id is the last path segment; an unknown id is a 404.
        if request.method == "DELETE" and "/admin/realms/" in request.url:
            if not self._is_admin(request):
                return self._answer(403)
            if self.fail_user_delete:
                return self._answer(500)
            user_id = request.url.rstrip("/").rsplit("/", 1)[-1]
            for name, user in list(self.users.items()):
                if user["id"] == user_id:
                    del self.users[name]
                    self.deleted_ids.append(user_id)
                    return self._answer(204)
            return self._answer(404)

        # Admin user create.
        if "/admin/realms/" in request.url and request.method == "POST":
            if not self._is_admin(request):
                return self._answer(403)
            if self.fail_user_create:
                return self._answer(400)
            body = request.json_body or {}
            self.create_bodies.append(dict(body))
            if (
                "username" not in body
                or "enabled" not in body
                or "credentials" not in body
            ):
                return self._answer(400)
            uname = body["username"]
            if uname in self.users:
                return self._answer(409)
            self.user_id_counter += 1
            user_id = f"user-{self.user_id_counter}"
            self.users[uname] = {"id": user_id, "username": uname, "enabled": True}
            return ModelIdentityHttpResponse(
                status=201,
                headers={"Location": f"/admin/realms/omninode/users/{user_id}"},
                json_body=None,
            )

        # Admin reset password.
        if "/reset-password" in request.url:
            if not self._is_admin(request):
                return self._answer(403)
            body = request.json_body or {}
            self.reset_bodies.append(dict(body))
            if body.get("type") != "password" or "value" not in body:
                return self._answer(400)
            return self._answer(204)

        return self._answer(404)

    def _is_admin(self, request: ModelIdentityHttpRequest) -> bool:
        return (
            request.bearer is not None
            and request.bearer.get_secret_value() == self.admin_token
        )


class FakeTenantProvisioner:
    """The runtime's tenant provision command in memory: it records each request and answers as a test says."""

    def __init__(
        self,
        *,
        outcome: str = "created",
        tenant_id: str | None = "tenant-abc-123",
        tenant_slug: str | None = "probe-tenant",
        unavailable: str | None = None,
    ) -> None:
        self.outcome = outcome
        self.tenant_id = tenant_id
        self.tenant_slug = tenant_slug
        self.unavailable = unavailable
        self.requests: list[ModelTenantProvisionRequest] = []

    async def provision(
        self, request: ModelTenantProvisionRequest
    ) -> ModelTenantProvisionAnswer:
        self.requests.append(request)
        if self.unavailable is not None:
            raise TenantProvisionUnavailableError(self.unavailable)
        return ModelTenantProvisionAnswer(
            command_id=request.command_id,
            outcome=self.outcome,
            detail="fake runtime answer",
            tenant_id=self.tenant_id,
            tenant_slug=self.tenant_slug,
        )
