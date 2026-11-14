# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory fakes for the dev keycloak client node tests (OMN-24237).

``FakeKeycloak`` is a strict, stateful fake that enforces the real wire rules: the admin realm's
token endpoint wants the password grant for the admin-cli client and the right bootstrap username
and password (else 401), the dev realm's token endpoint wants the client_credentials grant for the
dev client and the secret it last accepted on a client write (else 401, and a client whose service
accounts are disabled also 401), every admin path wants the admin bearer (else 403), the client
representation must carry the confidential service-account shape with a non-empty secret (else
400, created with a Location header, updated with 204), the service-account user lookup answers the
account's id, the role endpoint answers only the management client's known roles (else 404), and
the mapping endpoints want a JSON array of role objects each with an id and a name (else 400,
answered 204). The sign-in client exists a priori (unless a test says otherwise) and speaks the
protocol-mapper wire: its collection GET lists the stored mapper representations with their ids
(500 on ``fail_mapper_lookup``), its collection POST wants an OIDC mapper representation -- a
name, protocol ``openid-connect``, a protocolMapper and a config (else 400, 500 on
``fail_mapper_create``, answered 201 and stored with a fresh id) -- and its mapper PUT replaces the
stored one (unknown id 404, bad body 400, 500 on ``fail_mapper_update``, answered 204). It records
every request it accepted or refused and holds the secret it received, so a test can prove which
values moved on the wire. ``FakeSettingStore``, ``FakeKeyGenerator`` and ``FakeCredentialSource``
are the store, generator and bootstrap credential seams; the generator holds the value it
generated in memory so a test can prove it never appears in a result, a repr or a log.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import SecretStr

from omnimarket.models.secret_store.model_generate_key_result import (
    ModelGenerateKeyResult,
)
from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
)
from omnimarket.nodes.node_dev_keycloak_client_effect.models.model_keycloak_client_http import (
    ModelKeycloakHttpRequest,
    ModelKeycloakHttpResponse,
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


class FakeCredentialSource:
    """The host env file seam in memory: only the wanted names found, each value a secret."""

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values: dict[str, str] = dict(values or {})
        self.calls: list[list[str]] = []

    async def read(self, names: Sequence[str]) -> dict[str, SecretStr]:
        wanted = list(names)
        self.calls.append(wanted)
        return {
            name: SecretStr(value)
            for name, value in self._values.items()
            if name in wanted
        }


class FakeKeycloak:
    """A stateful Keycloak that speaks the real wire rules and records every request."""

    def __init__(
        self,
        *,
        base_url: str = "https://keycloak.example.invalid",
        admin_realm: str = "master",
        realm: str = "omninode",
        admin_client_id: str = "admin-cli",
        admin_username: str = "bootstrap-admin",
        admin_password: str = "bootstrap-password-value",
        dev_client_id: str = "provisioner-client",
        management_client_id: str = "realm-management",
        admin_token: str = "admin-access-token",
        client_token: str = "client-access-token",
        known_roles: tuple[str, ...] = ("manage-users", "view-users"),
        client_service_accounts_enabled: bool = True,
        sign_in_client_id: str = "omnidash-spa",
        sign_in_client_exists: bool = True,
        fail_admin_token: bool = False,
        fail_client_lookup: bool = False,
        fail_client_create: bool = False,
        fail_client_update: bool = False,
        fail_service_account: bool = False,
        fail_role_lookup: bool = False,
        fail_role_mapping: bool = False,
        fail_scope_mapping: bool = False,
        fail_mapper_lookup: bool = False,
        fail_mapper_create: bool = False,
        fail_mapper_update: bool = False,
        fail_client_token: bool = False,
        raise_connection_error: bool = False,
        raise_connection_error_on: str | None = None,
    ) -> None:
        self.base_url = base_url
        self.admin_realm = admin_realm
        self.realm = realm
        self.admin_client_id = admin_client_id
        self.admin_username = admin_username
        self.admin_password = admin_password
        self.dev_client_id = dev_client_id
        self.management_client_id = management_client_id
        self.admin_token = admin_token
        self.client_token = client_token
        self.known_roles = known_roles
        self.client_service_accounts_enabled = client_service_accounts_enabled
        self.sign_in_client_id = sign_in_client_id
        self.fail_mapper_lookup = fail_mapper_lookup
        self.fail_mapper_create = fail_mapper_create
        self.fail_mapper_update = fail_mapper_update
        self.fail_admin_token = fail_admin_token
        self.fail_client_lookup = fail_client_lookup
        self.fail_client_create = fail_client_create
        self.fail_client_update = fail_client_update
        self.fail_service_account = fail_service_account
        self.fail_role_lookup = fail_role_lookup
        self.fail_role_mapping = fail_role_mapping
        self.fail_scope_mapping = fail_scope_mapping
        self.fail_client_token = fail_client_token
        self.raise_connection_error = raise_connection_error
        self.raise_connection_error_on = raise_connection_error_on
        # The realm's clients, by clientId and by uuid: the management client and the sign-in
        # client exist a priori.
        self.clients: dict[str, dict[str, Any]] = {}
        self.clients_by_uuid: dict[str, dict[str, Any]] = {}
        self.dev_uuid: str | None = None
        self._uuid_counter = 0
        self._mapper_uuid_counter = 0
        self.management_uuid = self._next_uuid()
        management = {"id": self.management_uuid, "clientId": management_client_id}
        self.clients[management_client_id] = management
        self.clients_by_uuid[self.management_uuid] = management
        self.sign_in_uuid: str | None = None
        if sign_in_client_exists:
            self.sign_in_uuid = self._next_uuid()
            sign_in = {"id": self.sign_in_uuid, "clientId": sign_in_client_id}
            self.clients[sign_in_client_id] = sign_in
            self.clients_by_uuid[self.sign_in_uuid] = sign_in
        # Everything the fake saw, for a test to prove which values moved on the wire.
        self.call_log: list[str] = []
        self.sent: list[ModelKeycloakHttpRequest] = []
        self.token_forms: list[dict[str, str]] = []
        self.created_bodies: list[dict[str, Any]] = []
        self.updated_bodies: list[dict[str, Any]] = []
        self.role_mapping_bodies: list[list[dict[str, Any]]] = []
        self.scope_mapping_bodies: list[list[dict[str, Any]]] = []
        self.mappers_by_client: dict[str, list[dict[str, Any]]] = {}
        self.mapper_created_bodies: list[dict[str, Any]] = []
        self.mapper_updated_bodies: list[dict[str, Any]] = []
        self.received_secret: str | None = None

    def _next_uuid(self) -> str:
        self._uuid_counter += 1
        return f"client-uuid-{self._uuid_counter:04d}"

    def _next_mapper_uuid(self) -> str:
        self._mapper_uuid_counter += 1
        return f"mapper-uuid-{self._mapper_uuid_counter:04d}"

    def _answer(
        self, status: int, body: Any = None, headers: dict[str, str] | None = None
    ) -> ModelKeycloakHttpResponse:
        return ModelKeycloakHttpResponse(
            status=status, headers=headers or {}, json_body=body
        )

    async def send(
        self, request: ModelKeycloakHttpRequest
    ) -> ModelKeycloakHttpResponse:
        if self.raise_connection_error and (
            self.raise_connection_error_on is None
            or self.raise_connection_error_on in request.url
        ):
            raise ConnectionError("ConnectionError")

        self.call_log.append(f"{request.method} {request.url}")
        self.sent.append(request)

        if "/protocol/openid-connect/token" in request.url:
            return self._token(request)
        if "/admin/realms/" in request.url:
            return self._admin(request)
        return self._answer(404)

    # The token endpoint: the admin realm's password grant, the dev realm's client_credentials.

    def _token(self, request: ModelKeycloakHttpRequest) -> ModelKeycloakHttpResponse:
        if request.form is None or "/realms/" not in request.url:
            return self._answer(400)
        form = {k: v.get_secret_value() for k, v in request.form.items()}
        self.token_forms.append(form)
        realm = request.url.split("/realms/", 1)[1].split("/protocol", 1)[0]
        if realm == self.admin_realm:
            return self._admin_token_grant(form)
        if realm == self.realm:
            return self._client_token_grant(form)
        return self._answer(404)

    def _admin_token_grant(self, form: dict[str, str]) -> ModelKeycloakHttpResponse:
        if (
            self.fail_admin_token
            or form.get("grant_type") != "password"
            or form.get("client_id") != self.admin_client_id
            or form.get("username") != self.admin_username
            or form.get("password") != self.admin_password
        ):
            return self._answer(401)
        return self._answer(
            200, {"access_token": self.admin_token, "token_type": "Bearer"}
        )

    def _client_token_grant(self, form: dict[str, str]) -> ModelKeycloakHttpResponse:
        record = self.clients.get(self.dev_client_id)
        if (
            self.fail_client_token
            or form.get("grant_type") != "client_credentials"
            or form.get("client_id") != self.dev_client_id
            or form.get("client_secret") != self.received_secret
            or record is None
            or record.get("serviceAccountsEnabled") is not True
        ):
            return self._answer(401)
        return self._answer(
            200, {"access_token": self.client_token, "token_type": "Bearer"}
        )

    # The admin endpoints: the admin bearer, then one route per shape.

    def _admin(self, request: ModelKeycloakHttpRequest) -> ModelKeycloakHttpResponse:
        if (
            request.bearer is None
            or request.bearer.get_secret_value() != self.admin_token
        ):
            return self._answer(403)
        url = request.url
        # The protocol-mapper paths are matched before the generic /clients/ routes: a mapper URL
        # carries the owning client's uuid in its middle, not at its end.
        if url.endswith("/protocol-mappers/models"):
            return self._mapper_collection(request)
        if "/protocol-mappers/models/" in url:
            return self._mapper_item(request)
        if url.endswith("/service-account-user"):
            return self._service_account(url)
        if "/roles/" in url:
            return self._role(url)
        if "/role-mappings/clients/" in url:
            return self._role_mappings(request)
        if "/scope-mappings/clients/" in url:
            return self._scope_mappings(request)
        if url.endswith("/clients"):
            return self._clients_collection(request)
        if "/clients/" in url:
            return self._client_update(request)
        return self._answer(404)

    def _clients_collection(
        self, request: ModelKeycloakHttpRequest
    ) -> ModelKeycloakHttpResponse:
        if request.method == "GET":
            if self.fail_client_lookup:
                return self._answer(500)
            record = self.clients.get(request.query.get("clientId", ""))
            if record is None:
                return self._answer(200, [])
            return self._answer(
                200, [{"id": record["id"], "clientId": record["clientId"]}]
            )
        if request.method == "POST":
            return self._client_create(request)
        return self._answer(405)

    def _body_problem(self, body: Any) -> str | None:
        """One fact that makes the representation not a confidential service-account client."""
        if not isinstance(body, dict):
            return "the body is not an object"
        if not isinstance(body.get("clientId"), str) or not body["clientId"]:
            return "the clientId is missing"
        if body.get("enabled") is not True:
            return "the client is not enabled"
        if body.get("publicClient") is not False:
            return "the client is public"
        if body.get("serviceAccountsEnabled") is not True:
            return "the service accounts are not enabled"
        if body.get("standardFlowEnabled") is not False:
            return "the standard flow is enabled"
        if body.get("directAccessGrantsEnabled") is not False:
            return "the direct access grants are enabled"
        if body.get("fullScopeAllowed") is not False:
            return "the full scope is allowed"
        if not isinstance(body.get("secret"), str) or not body["secret"]:
            return "the secret is missing or empty"
        return None

    def _client_create(
        self, request: ModelKeycloakHttpRequest
    ) -> ModelKeycloakHttpResponse:
        body = request.json_body
        problem = self._body_problem(body)
        if problem is not None:
            return self._answer(400)
        if self.fail_client_create:
            return self._answer(500)
        representation = dict(body)
        self.created_bodies.append(representation)
        self.received_secret = representation["secret"]
        client_uuid = self._next_uuid()
        record = {
            "id": client_uuid,
            "clientId": representation["clientId"],
            "serviceAccountsEnabled": self.client_service_accounts_enabled,
        }
        self.clients[record["clientId"]] = record
        self.clients_by_uuid[client_uuid] = record
        self.dev_uuid = client_uuid
        return self._answer(
            201,
            None,
            {
                "Location": (
                    f"{self.base_url}/admin/realms/{self.realm}/clients/{client_uuid}"
                )
            },
        )

    def _client_update(
        self, request: ModelKeycloakHttpRequest
    ) -> ModelKeycloakHttpResponse:
        if request.method != "PUT":
            return self._answer(405)
        client_uuid = request.url.rsplit("/clients/", 1)[1]
        record = self.clients_by_uuid.get(client_uuid)
        if record is None:
            return self._answer(404)
        body = request.json_body
        problem = self._body_problem(body)
        if problem is not None:
            return self._answer(400)
        if self.fail_client_update:
            return self._answer(500)
        representation = dict(body)
        self.updated_bodies.append(representation)
        self.received_secret = representation["secret"]
        record["serviceAccountsEnabled"] = self.client_service_accounts_enabled
        return self._answer(204)

    def _service_account(self, url: str) -> ModelKeycloakHttpResponse:
        if self.fail_service_account:
            return self._answer(500)
        client_uuid = url.rsplit("/clients/", 1)[1].removesuffix(
            "/service-account-user"
        )
        record = self.clients_by_uuid.get(client_uuid)
        if record is None:
            return self._answer(404)
        account_id = f"service-account-{record['clientId']}"
        return self._answer(200, {"id": account_id, "username": account_id})

    def _role(self, url: str) -> ModelKeycloakHttpResponse:
        tail = url.rsplit("/clients/", 1)[1]
        client_uuid, _, role = tail.partition("/roles/")
        if client_uuid != self.management_uuid or not role:
            return self._answer(404)
        if self.fail_role_lookup or role not in self.known_roles:
            return self._answer(404)
        return self._answer(200, {"id": f"role-uuid-{role}", "name": role})

    def _mapping_problem(self, body: Any) -> str | None:
        """One fact that makes the body not a JSON array of role objects with id and name."""
        if not isinstance(body, list) or not body:
            return "the body is not a non-empty array"
        for entry in body:
            if not isinstance(entry, dict):
                return "an entry is not an object"
            if not isinstance(entry.get("id"), str) or not entry["id"]:
                return "an entry carries no id"
            if not isinstance(entry.get("name"), str) or not entry["name"]:
                return "an entry carries no name"
        return None

    def _role_mappings(
        self, request: ModelKeycloakHttpRequest
    ) -> ModelKeycloakHttpResponse:
        if request.method != "POST":
            return self._answer(405)
        user_id = request.url.split("/users/", 1)[1].split("/role-mappings/", 1)[0]
        _, _, management_uuid = request.url.rpartition("/role-mappings/clients/")
        if (
            user_id != f"service-account-{self.dev_client_id}"
            or management_uuid != self.management_uuid
        ):
            return self._answer(404)
        body = request.json_body
        problem = self._mapping_problem(body)
        if problem is not None:
            return self._answer(400)
        if self.fail_role_mapping:
            return self._answer(500)
        self.role_mapping_bodies.append([dict(entry) for entry in body])
        return self._answer(204)

    def _scope_mappings(
        self, request: ModelKeycloakHttpRequest
    ) -> ModelKeycloakHttpResponse:
        if request.method != "POST":
            return self._answer(405)
        head, _, management_uuid = request.url.rpartition("/scope-mappings/clients/")
        client_uuid = head.rsplit("/clients/", 1)[1]
        if client_uuid != self.dev_uuid or management_uuid != self.management_uuid:
            return self._answer(404)
        body = request.json_body
        problem = self._mapping_problem(body)
        if problem is not None:
            return self._answer(400)
        if self.fail_scope_mapping:
            return self._answer(500)
        self.scope_mapping_bodies.append([dict(entry) for entry in body])
        return self._answer(204)

    # The sign-in client's protocol mappers: a collection GET/POST and a mapper PUT.

    def _mapper_problem(self, body: Any) -> str | None:
        """One fact that makes the body not an OIDC protocol mapper representation."""
        if not isinstance(body, dict):
            return "the body is not an object"
        if not isinstance(body.get("name"), str) or not body["name"]:
            return "the name is missing"
        if body.get("protocol") != "openid-connect":
            return "the protocol is not openid-connect"
        if (
            not isinstance(body.get("protocolMapper"), str)
            or not body["protocolMapper"]
        ):
            return "the protocolMapper is missing"
        if not isinstance(body.get("config"), dict) or not body["config"]:
            return "the config is missing"
        return None

    def _mapper_client_uuid(self, url: str) -> str:
        """The uuid of the client a protocol-mapper URL talks about."""
        return url.rsplit("/clients/", 1)[1].split("/protocol-mappers/", 1)[0]

    def _mapper_collection(
        self, request: ModelKeycloakHttpRequest
    ) -> ModelKeycloakHttpResponse:
        client_uuid = self._mapper_client_uuid(request.url)
        if client_uuid not in self.clients_by_uuid:
            return self._answer(404)
        mappers = self.mappers_by_client.setdefault(client_uuid, [])
        if request.method == "GET":
            if self.fail_mapper_lookup:
                return self._answer(500)
            return self._answer(200, [dict(mapper) for mapper in mappers])
        if request.method == "POST":
            body = request.json_body
            problem = self._mapper_problem(body)
            if problem is not None:
                return self._answer(400)
            if self.fail_mapper_create:
                return self._answer(500)
            representation = {"id": self._next_mapper_uuid(), **body}
            mappers.append(representation)
            self.mapper_created_bodies.append(dict(body))
            return self._answer(201)
        return self._answer(405)

    def _mapper_item(
        self, request: ModelKeycloakHttpRequest
    ) -> ModelKeycloakHttpResponse:
        if request.method != "PUT":
            return self._answer(405)
        head, _, mapper_id = request.url.rpartition("/protocol-mappers/models/")
        client_uuid = head.rsplit("/clients/", 1)[1]
        mappers = self.mappers_by_client.get(client_uuid)
        if mappers is None:
            return self._answer(404)
        index = next(
            (i for i, mapper in enumerate(mappers) if mapper.get("id") == mapper_id),
            None,
        )
        if index is None:
            return self._answer(404)
        body = request.json_body
        problem = self._mapper_problem(body)
        if problem is not None:
            return self._answer(400)
        if self.fail_mapper_update:
            return self._answer(500)
        mappers[index] = {"id": mapper_id, **body}
        self.mapper_updated_bodies.append(dict(body))
        return self._answer(204)
