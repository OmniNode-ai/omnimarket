# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev identity provision handler (OMN-24115): one named dev Keycloak user, a generated password, one probe tenant.

The password value moves only between the key generator and the Keycloak calls; it is never printed, logged, serialized
or returned. The result carries names and ids only. The dev-only guard refuses, before any secret read or HTTP call,
when the lane or realm is not the one the contract declares.
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
from omnimarket.nodes.node_dev_identity_provision_effect.models.model_dev_identity_provision import (
    EnumDevIdentityOutcome,
    ModelDevIdentityProvisionCommand,
    ModelDevIdentityProvisionResult,
)
from omnimarket.nodes.node_dev_identity_provision_effect.models.model_identity_http import (
    ModelIdentityHttpRequest,
)
from omnimarket.nodes.node_dev_identity_provision_effect.overlay import (
    ModelDevIdentityProvisionOverlay,
)
from omnimarket.nodes.node_dev_identity_provision_effect.protocols.protocol_dev_identity_transport import (
    ProtocolDevIdentityTransport,
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


class HandlerDevIdentityProvision:
    """Creates one dev Keycloak user, stores a generated password for it, and provisions a probe tenant for it."""

    def __init__(
        self,
        *,
        overlay: ModelDevIdentityProvisionOverlay,
        transport: ProtocolDevIdentityTransport,
        settings_store: ProtocolSettingStore,
        key_generator: ProtocolKeyGenerator,
        contract_path: Path | None = None,
    ) -> None:
        self._overlay = overlay
        self._transport = transport
        self._settings_store = settings_store
        self._key_generator = key_generator
        self._contract_path = contract_path

    def _result(
        self,
        outcome: EnumDevIdentityOutcome,
        *,
        detail: str,
        user_name: str | None = None,
        tenant_id: str | None = None,
        tenant_slug: str | None = None,
        expected_secret: str | None = None,
    ) -> ModelDevIdentityProvisionResult:
        ok = outcome in (
            EnumDevIdentityOutcome.PROVISIONED,
            EnumDevIdentityOutcome.READY,
        )
        logger.info(
            "dev identity provision outcome=%s realm=%s lane=%s user=%s detail=%s",
            outcome.value,
            self._overlay.keycloak.realm,
            self._overlay.lane,
            user_name,
            detail,
        )
        return ModelDevIdentityProvisionResult(
            ok=ok,
            outcome=outcome,
            detail=detail,
            realm=self._overlay.keycloak.realm,
            lane=self._overlay.lane,
            user_name=user_name,
            tenant_id=tenant_id,
            tenant_slug=tenant_slug,
            expected_secret=expected_secret,
        )

    def _dev_guard(
        self, command: ModelDevIdentityProvisionCommand
    ) -> ModelDevIdentityProvisionResult | None:
        dev_only = _load_dev_only(self._contract_path)
        declared_lane = dev_only["lane"]
        declared_realms = dev_only["realms"]
        if command.lane != declared_lane:
            return self._result(
                EnumDevIdentityOutcome.REFUSED_NOT_DEV,
                detail=f"command lane {command.lane!r} is not the declared dev lane {declared_lane!r}",
            )
        if self._overlay.lane != declared_lane:
            return self._result(
                EnumDevIdentityOutcome.REFUSED_NOT_DEV,
                detail=f"overlay lane {self._overlay.lane!r} is not the declared dev lane {declared_lane!r}",
            )
        if self._overlay.keycloak.realm not in declared_realms:
            return self._result(
                EnumDevIdentityOutcome.REFUSED_NOT_DEV,
                detail=(
                    f"realm {self._overlay.keycloak.realm!r} is not in the declared realms {declared_realms}"
                ),
            )
        return None

    async def handle(
        self, command: ModelDevIdentityProvisionCommand
    ) -> ModelDevIdentityProvisionResult:
        overlay = self._overlay
        kc = overlay.keycloak
        admin = overlay.admin
        user = overlay.user
        api = overlay.api

        guard = self._dev_guard(command)
        if guard is not None:
            return guard

        admin_secret = await self._settings_store.read(
            admin.secret_folder, admin.secret_key
        )
        if admin_secret is None:
            return self._result(
                EnumDevIdentityOutcome.ADMIN_CREDENTIAL_MISSING,
                detail=f"the admin credential {admin.secret_folder}/{admin.secret_key} is not in the store",
                expected_secret=f"{admin.secret_folder}/{admin.secret_key}",
            )
        if not command.write:
            return self._result(
                EnumDevIdentityOutcome.READY,
                detail="the admin credential is present; the probe verb wrote nothing and called nothing",
            )

        token_url = f"{kc.base_url}{kc.token_path_template.format(realm=kc.realm)}"
        admin_token = await self._admin_token(token_url, admin, admin_secret)
        if admin_token is None:
            return self._result(
                EnumDevIdentityOutcome.ADMIN_TOKEN_REFUSED,
                detail="the Keycloak admin token endpoint refused the client credentials",
            )
        if isinstance(admin_token, str):
            return self._result(
                EnumDevIdentityOutcome.KEYCLOAK_UNREACHABLE,
                detail=admin_token,
            )

        gen = await self._key_generator.generate(user.password_key_id)
        if gen.outcome not in (
            EnumSecretStoreOutcome.CREATED,
            EnumSecretStoreOutcome.EXISTS,
        ):
            return self._result(
                EnumDevIdentityOutcome.PASSWORD_UNAVAILABLE,
                detail=f"the key generator returned {gen.outcome.value} for {user.password_key_id!r}",
            )
        password = await self._settings_store.read(
            user.password_folder, user.password_key
        )
        if password is None:
            return self._result(
                EnumDevIdentityOutcome.PASSWORD_UNAVAILABLE,
                detail=f"the generated password {user.password_folder}/{user.password_key} is not in the store",
            )

        user_id = await self._ensure_user(admin_token, password)
        if user_id is None:
            return self._result(
                EnumDevIdentityOutcome.USER_CREATE_FAILED,
                detail="Keycloak refused to create or reset the dev user",
                user_name=user.username,
            )

        user_token = await self._user_token(token_url, user, password)
        if user_token is None:
            return self._result(
                EnumDevIdentityOutcome.USER_TOKEN_REFUSED,
                detail="the Keycloak password grant refused the dev user",
                user_name=user.username,
            )
        if isinstance(user_token, str):
            return self._result(
                EnumDevIdentityOutcome.KEYCLOAK_UNREACHABLE,
                detail=user_token,
                user_name=user.username,
            )

        provision_url = f"{api.base_url}{api.provision_path}"
        response = await self._send(
            ModelIdentityHttpRequest(
                method="POST",
                url=provision_url,
                bearer=user_token,
                timeout_seconds=api.timeout_seconds,
            )
        )
        if isinstance(response, str):
            return self._result(
                EnumDevIdentityOutcome.API_UNREACHABLE,
                detail=response,
                user_name=user.username,
            )
        if response.status not in (200, 201):
            return self._result(
                EnumDevIdentityOutcome.PROVISION_REFUSED,
                detail=f"the provision endpoint answered {response.status}",
                user_name=user.username,
            )
        body = response.json_body
        if not isinstance(body, dict):
            return self._result(
                EnumDevIdentityOutcome.PROVISION_RESPONSE_INVALID,
                detail="the provision response carried no body",
                user_name=user.username,
            )
        tenant_id = body.get("tenant_id")
        tenant_slug = body.get("tenant_slug")
        if not isinstance(tenant_id, str) or not isinstance(tenant_slug, str):
            return self._result(
                EnumDevIdentityOutcome.PROVISION_RESPONSE_INVALID,
                detail="the provision response is missing tenant_id or tenant_slug",
                user_name=user.username,
            )
        return self._result(
            EnumDevIdentityOutcome.PROVISIONED,
            detail="the probe tenant was bound to the dev user",
            user_name=user.username,
            tenant_id=tenant_id,
            tenant_slug=tenant_slug,
        )

    async def _send(self, request: ModelIdentityHttpRequest) -> Any:
        try:
            return await self._transport.send(request)
        except ConnectionError as exc:
            return f"no response arrived: {type(exc).__name__}"

    async def _admin_token(
        self, token_url: str, admin: Any, admin_secret: SecretStr
    ) -> Any:
        response = await self._send(
            ModelIdentityHttpRequest(
                method="POST",
                url=token_url,
                form={
                    "grant_type": SecretStr("client_credentials"),
                    "client_id": SecretStr(admin.client_id),
                    "client_secret": admin_secret,
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

    async def _ensure_user(
        self, admin_token: SecretStr, password: SecretStr
    ) -> str | None:
        overlay = self._overlay
        kc = overlay.keycloak
        user = overlay.user
        users_url = (
            f"{kc.base_url}{kc.admin_users_path_template.format(realm=kc.realm)}"
        )
        found = await self._send(
            ModelIdentityHttpRequest(
                method="GET",
                url=users_url,
                query={"username": user.username, "exact": "true"},
                bearer=admin_token,
                timeout_seconds=kc.timeout_seconds,
            )
        )
        if isinstance(found, str):
            return None
        if (
            found.status == 200
            and isinstance(found.json_body, list)
            and found.json_body
        ):
            user_id = found.json_body[0].get("id")
            if not isinstance(user_id, str):
                return None
            reset_url = f"{kc.base_url}{kc.admin_reset_password_path_template.format(realm=kc.realm, user_id=user_id)}"
            reset = await self._send(
                ModelIdentityHttpRequest(
                    method="PUT",
                    url=reset_url,
                    bearer=admin_token,
                    json_body={
                        "type": "password",
                        "value": password.get_secret_value(),
                        "temporary": False,
                    },
                    timeout_seconds=kc.timeout_seconds,
                )
            )
            if isinstance(reset, str) or reset.status not in (200, 204):
                return None
            return user_id
        create = await self._send(
            ModelIdentityHttpRequest(
                method="POST",
                url=users_url,
                bearer=admin_token,
                json_body={
                    "username": user.username,
                    "email": user.email,
                    "firstName": user.first_name,
                    "lastName": user.last_name,
                    "enabled": True,
                    "emailVerified": True,
                    "credentials": [
                        {
                            "type": "password",
                            "value": password.get_secret_value(),
                            "temporary": False,
                        }
                    ],
                },
                timeout_seconds=kc.timeout_seconds,
            )
        )
        if isinstance(create, str):
            return None
        if create.status == 201:
            location = create.headers.get("Location", "")
            created_id: str = location.rsplit("/", 1)[-1] if location else ""
            if created_id:
                return created_id
            lookup = await self._send(
                ModelIdentityHttpRequest(
                    method="GET",
                    url=users_url,
                    query={"username": user.username, "exact": "true"},
                    bearer=admin_token,
                    timeout_seconds=kc.timeout_seconds,
                )
            )
            if isinstance(lookup, str):
                return None
            if (
                lookup.status == 200
                and isinstance(lookup.json_body, list)
                and lookup.json_body
            ):
                uid = lookup.json_body[0].get("id")
                if isinstance(uid, str):
                    return uid
            return None
        return None

    async def _user_token(self, token_url: str, user: Any, password: SecretStr) -> Any:
        response = await self._send(
            ModelIdentityHttpRequest(
                method="POST",
                url=token_url,
                form={
                    "grant_type": SecretStr("password"),
                    "client_id": SecretStr(user.client_id),
                    "username": SecretStr(user.username),
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


__all__ = ["HandlerDevIdentityProvision"]
