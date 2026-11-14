# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev keycloak client handler (OMN-24237): one confidential service-account client, two realm-management roles.

The bootstrap admin password moves only from the host env file into the admin token request; the client secret moves
only between the key generator, the client representation and the token requests. Neither is ever printed, logged,
serialized or returned, and the proof request's ``access_token`` is never stored or returned. The result carries names,
ids and a status code only. The dev-only guard refuses, before any secret read or HTTP call, when the lane, realm, a
requested role or the sign-in client is not what the contract declares. After the roles the same admin token also
ensures the declared sign-in client's OIDC protocol mappers, so its tokens carry the tenant claims.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import SecretStr

from omnimarket.handlers.delegation_scenario_settings import ProtocolSettingStore
from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
)
from omnimarket.nodes.node_dev_keycloak_client_effect.models.model_dev_keycloak_client import (
    EnumDevKeycloakClientOutcome,
    ModelDevKeycloakClientCommand,
    ModelDevKeycloakClientResult,
)
from omnimarket.nodes.node_dev_keycloak_client_effect.models.model_keycloak_client_http import (
    ModelKeycloakHttpRequest,
)
from omnimarket.nodes.node_dev_keycloak_client_effect.overlay import (
    ModelDevKeycloakClientOverlay,
)
from omnimarket.nodes.node_dev_keycloak_client_effect.protocols.protocol_dev_keycloak_client import (
    ProtocolBootstrapCredentialSource,
    ProtocolKeycloakTransport,
    ProtocolKeyGenerator,
)

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"


def _load_dev_only(contract_path: Path | None) -> dict[str, Any]:
    """The contract's ``dev_only`` block; the guard reads it from the file, never from code."""
    path = contract_path or _CONTRACT_PATH
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    dev_only: dict[str, Any] = document["dev_only"]
    return dev_only


class HandlerDevKeycloakClient:
    """Ensures one confidential dev Keycloak service-account client and proves it with a client_credentials token.

    The same admin token also ensures the declared sign-in client's tenant claim protocol mappers.
    """

    def __init__(
        self,
        *,
        overlay: ModelDevKeycloakClientOverlay,
        transport: ProtocolKeycloakTransport,
        settings_store: ProtocolSettingStore,
        key_generator: ProtocolKeyGenerator,
        credential_source: ProtocolBootstrapCredentialSource,
        contract_path: Path | None = None,
    ) -> None:
        self._overlay = overlay
        self._transport = transport
        self._settings_store = settings_store
        self._key_generator = key_generator
        self._credential_source = credential_source
        self._contract_path = contract_path

    def _result(
        self,
        outcome: EnumDevKeycloakClientOutcome,
        *,
        detail: str,
        client_id: str | None = None,
        token_status: int | None = None,
        expected_location: str | None = None,
        mappers_ensured: int | None = None,
    ) -> ModelDevKeycloakClientResult:
        ok = outcome in (
            EnumDevKeycloakClientOutcome.ENSURED,
            EnumDevKeycloakClientOutcome.READY,
        )
        logger.info(
            "dev keycloak client outcome=%s realm=%s lane=%s client=%s",
            outcome.value,
            self._overlay.keycloak.realm,
            self._overlay.lane,
            client_id,
        )
        return ModelDevKeycloakClientResult(
            ok=ok,
            outcome=outcome,
            detail=detail,
            realm=self._overlay.keycloak.realm,
            lane=self._overlay.lane,
            client_id=client_id,
            token_status=token_status,
            expected_location=expected_location,
            mappers_ensured=mappers_ensured,
        )

    def _dev_guard(
        self, command: ModelDevKeycloakClientCommand
    ) -> ModelDevKeycloakClientResult | None:
        dev_only = _load_dev_only(self._contract_path)
        declared_lane = dev_only["lane"]
        declared_realms = dev_only["realms"]
        allowed_roles = dev_only["allowed_roles"]
        if command.lane != declared_lane:
            return self._result(
                EnumDevKeycloakClientOutcome.REFUSED_NOT_DEV,
                detail=f"command lane {command.lane!r} is not the declared dev lane {declared_lane!r}",
            )
        if self._overlay.lane != declared_lane:
            return self._result(
                EnumDevKeycloakClientOutcome.REFUSED_NOT_DEV,
                detail=f"overlay lane {self._overlay.lane!r} is not the declared dev lane {declared_lane!r}",
            )
        if self._overlay.keycloak.realm not in declared_realms:
            return self._result(
                EnumDevKeycloakClientOutcome.REFUSED_NOT_DEV,
                detail=(
                    f"realm {self._overlay.keycloak.realm!r} is not in the declared realms {declared_realms}"
                ),
            )
        for role in self._overlay.client.roles:
            if role not in allowed_roles:
                return self._result(
                    EnumDevKeycloakClientOutcome.REFUSED_ROLE_NOT_ALLOWED,
                    detail=f"role {role!r} is not in the declared allowed roles {allowed_roles}",
                )
        mapper_clients = dev_only["mapper_clients"]
        if self._overlay.sign_in_client.client_id not in mapper_clients:
            return self._result(
                EnumDevKeycloakClientOutcome.REFUSED_MAPPER_CLIENT_NOT_ALLOWED,
                detail=(
                    f"sign-in client {self._overlay.sign_in_client.client_id!r} is not "
                    f"in the declared mapper clients {mapper_clients}"
                ),
            )
        return None

    async def handle(
        self, command: ModelDevKeycloakClientCommand
    ) -> ModelDevKeycloakClientResult:
        overlay = self._overlay
        kc = overlay.keycloak
        bootstrap = overlay.bootstrap
        client = overlay.client

        guard = self._dev_guard(command)
        if guard is not None:
            return guard

        credentials = await self._credential_source.read(
            [bootstrap.username_name, bootstrap.password_name]
        )
        username = credentials.get(bootstrap.username_name)
        password = credentials.get(bootstrap.password_name)
        if username is None or password is None:
            return self._result(
                EnumDevKeycloakClientOutcome.BOOTSTRAP_CREDENTIAL_MISSING,
                detail=(
                    f"the bootstrap credential {bootstrap.env_file} does not hold "
                    f"{bootstrap.username_name} and {bootstrap.password_name}"
                ),
                expected_location=(
                    f"{bootstrap.env_file}:{bootstrap.username_name},{bootstrap.password_name}"
                ),
            )
        if not command.write:
            return self._result(
                EnumDevKeycloakClientOutcome.READY,
                detail="the bootstrap credential is present; the probe verb wrote nothing and called nothing",
            )

        admin_token_url = (
            f"{kc.base_url}{kc.token_path_template.format(realm=kc.admin_realm)}"
        )
        admin_token = await self._admin_token(
            admin_token_url, bootstrap.admin_client_id, username, password
        )
        if admin_token is None:
            return self._result(
                EnumDevKeycloakClientOutcome.ADMIN_TOKEN_REFUSED,
                detail="the Keycloak admin token endpoint refused the bootstrap credentials",
            )
        if isinstance(admin_token, str):
            return self._result(
                EnumDevKeycloakClientOutcome.KEYCLOAK_UNREACHABLE,
                detail=admin_token,
            )

        gen = await self._key_generator.generate(client.secret_key_id)
        if gen.outcome not in (
            EnumSecretStoreOutcome.CREATED,
            EnumSecretStoreOutcome.EXISTS,
        ):
            return self._result(
                EnumDevKeycloakClientOutcome.CLIENT_SECRET_UNAVAILABLE,
                detail=f"the key generator returned {gen.outcome.value} for {client.secret_key_id!r}",
            )
        secret = await self._settings_store.read(
            client.secret_folder, client.secret_key
        )
        if secret is None:
            return self._result(
                EnumDevKeycloakClientOutcome.CLIENT_SECRET_UNAVAILABLE,
                detail=(
                    f"the generated client secret {client.secret_folder}/{client.secret_key} "
                    "is not in the store"
                ),
                expected_location=f"{client.secret_folder}/{client.secret_key}",
            )

        ensured = await self._ensure_client(admin_token, secret)
        if isinstance(ensured, ModelDevKeycloakClientResult):
            return ensured
        client_uuid = ensured

        roles_failed = await self._grant_roles(admin_token, client_uuid)
        if roles_failed is not None:
            return roles_failed

        mappers_failed = await self._ensure_mappers(admin_token)
        if mappers_failed is not None:
            return mappers_failed

        verify_url = f"{kc.base_url}{kc.token_path_template.format(realm=kc.realm)}"
        response = await self._send(
            ModelKeycloakHttpRequest(
                method="POST",
                url=verify_url,
                form={
                    "grant_type": SecretStr("client_credentials"),
                    "client_id": SecretStr(client.client_id),
                    "client_secret": secret,
                },
                timeout_seconds=kc.timeout_seconds,
            )
        )
        if isinstance(response, str):
            return self._result(
                EnumDevKeycloakClientOutcome.KEYCLOAK_UNREACHABLE,
                detail=response,
                client_id=client.client_id,
            )
        if response.status != 200:
            return self._result(
                EnumDevKeycloakClientOutcome.CLIENT_TOKEN_REFUSED,
                detail=f"the client_credentials grant answered {response.status}",
                client_id=client.client_id,
                token_status=response.status,
            )
        return self._result(
            EnumDevKeycloakClientOutcome.ENSURED,
            detail=(
                "the client_credentials grant proved the dev client and the sign-in "
                "client's tenant claim mappers are in place"
            ),
            client_id=client.client_id,
            token_status=200,
            mappers_ensured=len(overlay.sign_in_client.mappers),
        )

    async def _send(self, request: ModelKeycloakHttpRequest) -> Any:
        try:
            return await self._transport.send(request)
        except ConnectionError as exc:
            return f"no response arrived: {type(exc).__name__}"

    def _unreachable(
        self, detail: str, *, client_id: str | None = None
    ) -> ModelDevKeycloakClientResult:
        return self._result(
            EnumDevKeycloakClientOutcome.KEYCLOAK_UNREACHABLE,
            detail=detail,
            client_id=client_id,
        )

    async def _admin_token(
        self,
        token_url: str,
        admin_client_id: str,
        username: SecretStr,
        password: SecretStr,
    ) -> SecretStr | str | None:
        response = await self._send(
            ModelKeycloakHttpRequest(
                method="POST",
                url=token_url,
                form={
                    "grant_type": SecretStr("password"),
                    "client_id": SecretStr(admin_client_id),
                    "username": username,
                    "password": password,
                },
                timeout_seconds=self._overlay.keycloak.timeout_seconds,
            )
        )
        if isinstance(response, str):
            return response
        if response.status != 200 or not isinstance(response.json_body, dict):
            return None
        access_token = response.json_body.get("access_token")
        if not isinstance(access_token, str):
            return None
        return SecretStr(access_token)

    async def _ensure_client(
        self, admin_token: SecretStr, secret: SecretStr
    ) -> str | ModelDevKeycloakClientResult:
        """The client uuid when the client was created or updated, else the failure result to answer."""
        overlay = self._overlay
        kc = overlay.keycloak
        client = overlay.client
        clients_url = f"{kc.base_url}{kc.clients_path_template.format(realm=kc.realm)}"
        representation = {
            "clientId": client.client_id,
            "enabled": True,
            "publicClient": False,
            "serviceAccountsEnabled": True,
            "standardFlowEnabled": False,
            "directAccessGrantsEnabled": False,
            "fullScopeAllowed": False,
            "secret": secret.get_secret_value(),
        }

        def _failed(detail: str) -> ModelDevKeycloakClientResult:
            return self._result(
                EnumDevKeycloakClientOutcome.CLIENT_WRITE_FAILED,
                detail=detail,
                client_id=client.client_id,
            )

        found = await self._send(
            ModelKeycloakHttpRequest(
                method="GET",
                url=clients_url,
                query={"clientId": client.client_id},
                bearer=admin_token,
                timeout_seconds=kc.timeout_seconds,
            )
        )
        if isinstance(found, str):
            return self._unreachable(found, client_id=client.client_id)
        if found.status != 200:
            return _failed(f"the client lookup answered {found.status}")
        if isinstance(found.json_body, list) and found.json_body:
            entry = found.json_body[0]
            client_uuid = entry.get("id") if isinstance(entry, dict) else None
            if not isinstance(client_uuid, str):
                return _failed("the client lookup carried no client id")
            client_url = f"{kc.base_url}{kc.client_path_template.format(realm=kc.realm, client_uuid=client_uuid)}"
            updated = await self._send(
                ModelKeycloakHttpRequest(
                    method="PUT",
                    url=client_url,
                    bearer=admin_token,
                    json_body=representation,
                    timeout_seconds=kc.timeout_seconds,
                )
            )
            if isinstance(updated, str):
                return self._unreachable(updated, client_id=client.client_id)
            if updated.status not in (200, 204):
                return _failed(f"the client update answered {updated.status}")
            return client_uuid

        created = await self._send(
            ModelKeycloakHttpRequest(
                method="POST",
                url=clients_url,
                bearer=admin_token,
                json_body=representation,
                timeout_seconds=kc.timeout_seconds,
            )
        )
        if isinstance(created, str):
            return self._unreachable(created, client_id=client.client_id)
        if created.status != 201:
            return _failed(f"the client create answered {created.status}")
        location = created.headers.get("Location", "")
        created_id: str = location.rsplit("/", 1)[-1] if location else ""
        if created_id:
            return created_id
        lookup = await self._send(
            ModelKeycloakHttpRequest(
                method="GET",
                url=clients_url,
                query={"clientId": client.client_id},
                bearer=admin_token,
                timeout_seconds=kc.timeout_seconds,
            )
        )
        if isinstance(lookup, str):
            return self._unreachable(lookup, client_id=client.client_id)
        if (
            lookup.status == 200
            and isinstance(lookup.json_body, list)
            and lookup.json_body
        ):
            entry = lookup.json_body[0]
            looked_up_id = entry.get("id") if isinstance(entry, dict) else None
            if isinstance(looked_up_id, str):
                return looked_up_id
        return _failed("the created client carried no uuid")

    async def _grant_roles(
        self, admin_token: SecretStr, client_uuid: str
    ) -> ModelDevKeycloakClientResult | None:
        """``None`` when every role mapping was granted, else the failure result to answer."""
        overlay = self._overlay
        kc = overlay.keycloak
        client = overlay.client
        clients_url = f"{kc.base_url}{kc.clients_path_template.format(realm=kc.realm)}"

        def _failed(detail: str) -> ModelDevKeycloakClientResult:
            return self._result(
                EnumDevKeycloakClientOutcome.ROLE_GRANT_FAILED,
                detail=detail,
                client_id=client.client_id,
            )

        found = await self._send(
            ModelKeycloakHttpRequest(
                method="GET",
                url=clients_url,
                query={"clientId": client.management_client_id},
                bearer=admin_token,
                timeout_seconds=kc.timeout_seconds,
            )
        )
        if isinstance(found, str):
            return self._unreachable(found, client_id=client.client_id)
        if found.status != 200:
            return _failed(
                f"the {client.management_client_id!r} client lookup answered {found.status}"
            )
        management_entry = (
            found.json_body[0]
            if isinstance(found.json_body, list) and found.json_body
            else None
        )
        management_uuid = (
            management_entry.get("id") if isinstance(management_entry, dict) else None
        )
        if not isinstance(management_uuid, str):
            return _failed(
                f"the {client.management_client_id!r} client lookup carried no client id"
            )

        account_url = f"{kc.base_url}{kc.service_account_user_path_template.format(realm=kc.realm, client_uuid=client_uuid)}"
        account = await self._send(
            ModelKeycloakHttpRequest(
                method="GET",
                url=account_url,
                bearer=admin_token,
                timeout_seconds=kc.timeout_seconds,
            )
        )
        if isinstance(account, str):
            return self._unreachable(account, client_id=client.client_id)
        if account.status != 200:
            return _failed(f"the service account user lookup answered {account.status}")
        account_body = account.json_body if isinstance(account.json_body, dict) else {}
        user_id = account_body.get("id")
        if not isinstance(user_id, str):
            return _failed("the service account user lookup carried no user id")

        role_representations: list[dict[str, Any]] = []
        for role in client.roles:
            role_url = f"{kc.base_url}{kc.client_role_path_template.format(realm=kc.realm, client_uuid=management_uuid, role=role)}"
            answer = await self._send(
                ModelKeycloakHttpRequest(
                    method="GET",
                    url=role_url,
                    bearer=admin_token,
                    timeout_seconds=kc.timeout_seconds,
                )
            )
            if isinstance(answer, str):
                return self._unreachable(answer, client_id=client.client_id)
            if answer.status != 200 or not isinstance(answer.json_body, dict):
                return _failed(f"the role {role!r} lookup answered {answer.status}")
            role_representations.append(answer.json_body)

        mappings_url = f"{kc.base_url}{kc.user_client_role_mappings_path_template.format(realm=kc.realm, user_id=user_id, client_uuid=management_uuid)}"
        mappings = await self._send(
            ModelKeycloakHttpRequest(
                method="POST",
                url=mappings_url,
                bearer=admin_token,
                json_body=role_representations,
                timeout_seconds=kc.timeout_seconds,
            )
        )
        if isinstance(mappings, str):
            return self._unreachable(mappings, client_id=client.client_id)
        if mappings.status != 204:
            return _failed(
                f"the service account role mapping answered {mappings.status}"
            )

        scope_url = f"{kc.base_url}{kc.client_scope_mappings_path_template.format(realm=kc.realm, client_uuid=client_uuid, management_client_uuid=management_uuid)}"
        scope = await self._send(
            ModelKeycloakHttpRequest(
                method="POST",
                url=scope_url,
                bearer=admin_token,
                json_body=role_representations,
                timeout_seconds=kc.timeout_seconds,
            )
        )
        if isinstance(scope, str):
            return self._unreachable(scope, client_id=client.client_id)
        if scope.status != 204:
            return _failed(f"the client scope mapping answered {scope.status}")
        return None

    async def _ensure_mappers(
        self, admin_token: SecretStr
    ) -> ModelDevKeycloakClientResult | None:
        """``None`` when every declared mapper exists on the sign-in client, else the failure result."""
        overlay = self._overlay
        kc = overlay.keycloak
        sign_in = overlay.sign_in_client
        clients_url = f"{kc.base_url}{kc.clients_path_template.format(realm=kc.realm)}"

        def _failed(detail: str) -> ModelDevKeycloakClientResult:
            return self._result(
                EnumDevKeycloakClientOutcome.MAPPER_WRITE_FAILED,
                detail=detail,
                client_id=sign_in.client_id,
            )

        found = await self._send(
            ModelKeycloakHttpRequest(
                method="GET",
                url=clients_url,
                query={"clientId": sign_in.client_id},
                bearer=admin_token,
                timeout_seconds=kc.timeout_seconds,
            )
        )
        if isinstance(found, str):
            return self._unreachable(found, client_id=sign_in.client_id)
        if found.status != 200:
            return _failed(f"the sign-in client lookup answered {found.status}")
        entry = (
            found.json_body[0]
            if isinstance(found.json_body, list) and found.json_body
            else None
        )
        sign_in_uuid = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(sign_in_uuid, str):
            return self._result(
                EnumDevKeycloakClientOutcome.MAPPER_CLIENT_NOT_FOUND,
                detail=f"the sign-in client {sign_in.client_id!r} is not in the realm",
                client_id=sign_in.client_id,
            )

        mappers_url = (
            f"{kc.base_url}"
            f"{kc.client_protocol_mappers_path_template.format(realm=kc.realm, client_uuid=sign_in_uuid)}"
        )
        existing = await self._send(
            ModelKeycloakHttpRequest(
                method="GET",
                url=mappers_url,
                bearer=admin_token,
                timeout_seconds=kc.timeout_seconds,
            )
        )
        if isinstance(existing, str):
            return self._unreachable(existing, client_id=sign_in.client_id)
        if existing.status != 200 or not isinstance(existing.json_body, list):
            return _failed(
                f"the sign-in client's protocol mappers answered {existing.status}"
            )
        existing_ids = {
            body["name"]: body["id"]
            for body in existing.json_body
            if isinstance(body, dict)
            and isinstance(body.get("name"), str)
            and isinstance(body.get("id"), str)
        }

        for mapper in sign_in.mappers:
            representation: dict[str, Any] = {
                "name": mapper.name,
                "protocol": "openid-connect",
                "protocolMapper": mapper.protocol_mapper,
                "config": dict(mapper.config),
            }
            mapper_id = existing_ids.get(mapper.name)
            if mapper_id is not None:
                mapper_url = (
                    f"{kc.base_url}"
                    f"{kc.client_protocol_mapper_path_template.format(realm=kc.realm, client_uuid=sign_in_uuid, mapper_id=mapper_id)}"
                )
                updated = await self._send(
                    ModelKeycloakHttpRequest(
                        method="PUT",
                        url=mapper_url,
                        bearer=admin_token,
                        json_body={**representation, "id": mapper_id},
                        timeout_seconds=kc.timeout_seconds,
                    )
                )
                if isinstance(updated, str):
                    return self._unreachable(updated, client_id=sign_in.client_id)
                if updated.status not in (200, 204):
                    return _failed(
                        f"the mapper {mapper.name!r} update answered {updated.status}"
                    )
                continue
            created = await self._send(
                ModelKeycloakHttpRequest(
                    method="POST",
                    url=mappers_url,
                    bearer=admin_token,
                    json_body=representation,
                    timeout_seconds=kc.timeout_seconds,
                )
            )
            if isinstance(created, str):
                return self._unreachable(created, client_id=sign_in.client_id)
            if created.status != 201:
                return _failed(
                    f"the mapper {mapper.name!r} create answered {created.status}"
                )
        return None


__all__ = ["HandlerDevKeycloakClient"]
