# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev identity provision handler (OMN-24115): one named dev Keycloak user, a generated password, one probe tenant.

The tenant is created by the runtime's tenant provision command, asked over the bus through the tenant provisioner
seam; no separate HTTP API is called. The tenant is ownerless: the runtime's owner path does not exist yet.

The password value moves only between the key generator and the Keycloak calls; it is never printed, logged, serialized
or returned. The result carries names and ids only. The dev-only guard refuses, before any secret read or HTTP call,
when the lane or realm is not the one the contract declares.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import SecretStr

from omnimarket.handlers.delegation_scenario_settings import ProtocolSettingStore
from omnimarket.models.model_dev_identity_http import (
    ModelIdentityHttpRequest,
)
from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
)
from omnimarket.nodes.node_dev_identity_provision_effect.models.model_dev_identity_provision import (
    EnumDevIdentityOutcome,
    ModelDevIdentityProvisionCommand,
    ModelDevIdentityProvisionResult,
)
from omnimarket.nodes.node_dev_identity_provision_effect.models.model_tenant_provision_wire import (
    ModelTenantProvisionRequest,
)
from omnimarket.nodes.node_dev_identity_provision_effect.overlay import (
    ModelDevIdentityProvisionOverlay,
)
from omnimarket.nodes.node_dev_identity_provision_effect.protocols.protocol_dev_identity_transport import (
    ProtocolDevIdentityTransport,
    ProtocolKeyGenerator,
    ProtocolTenantProvisioner,
    TenantProvisionUnavailableError,
)

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"

# The replay key of a user's tenant command: one user always asks the same thing, so a rerun is answered as a replay.
_TENANT_COMMAND_NAMESPACE = uuid.UUID("5c0b1e7e-2f0a-4c6e-9a53-0d1e6f6b3a10")
_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")
_CREATED_OUTCOMES = frozenset({"created", "already_exists"})


def _tenant_slug(username: str) -> str:
    """A DNS-label form of the username; the runtime's slug rule decides whether it is a usable slug."""
    return _SLUG_UNSAFE.sub("-", username.lower()).strip("-")


class _UserLookupFailedError(Exception):
    """Keycloak did not answer the user lookup with a usable body."""


@dataclass(frozen=True)
class _Identity:
    """The identity one run provisions: the command's values over the overlay user's."""

    username: str
    email: str
    first_name: str
    last_name: str
    client_id: str
    password_key_id: str
    password_folder: str
    password_key: str

    @property
    def tenant_slug(self) -> str:
        return _tenant_slug(self.username)


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
        tenant_provisioner: ProtocolTenantProvisioner,
        contract_path: Path | None = None,
    ) -> None:
        self._overlay = overlay
        self._transport = transport
        self._settings_store = settings_store
        self._key_generator = key_generator
        self._tenant_provisioner = tenant_provisioner
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
            "dev identity provision outcome=%s realm=%s lane=%s user=%s",
            outcome.value,
            self._overlay.keycloak.realm,
            self._overlay.lane,
            user_name,
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

        guard = self._dev_guard(command)
        if guard is not None:
            return guard

        user = self._identity(command)
        if isinstance(user, ModelDevIdentityProvisionResult):
            return user
        if command.write and not command.allow_protected:
            protected = {name.lower() for name in overlay.protected_usernames}
            if user.username.lower() in protected:
                return self._result(
                    EnumDevIdentityOutcome.USER_PROTECTED,
                    detail=(
                        f"user {user.username!r} is in the overlay's protected_usernames; "
                        "nothing was read or sent. Name the user on purpose with allow_protected to touch it"
                    ),
                    user_name=user.username,
                )

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
        # The overlay's own key is read where the overlay says; a key the command named is read where the generator stored it.
        if command.password_key_id is None:
            password_folder, password_key = user.password_folder, user.password_key
        elif gen.folder is not None and gen.key is not None:
            password_folder, password_key = gen.folder, gen.key
        else:
            return self._result(
                EnumDevIdentityOutcome.PASSWORD_UNAVAILABLE,
                detail=f"the key generator named no folder and key for {user.password_key_id!r}",
            )
        password = await self._settings_store.read(password_folder, password_key)
        if password is None:
            return self._result(
                EnumDevIdentityOutcome.PASSWORD_UNAVAILABLE,
                detail=f"the generated password {password_folder}/{password_key} is not in the store",
            )

        # An existing user is refused here, after the key and password reads and before any write.
        try:
            found = await self._find_user(admin_token, user.username)
        except _UserLookupFailedError:
            return self._result(
                EnumDevIdentityOutcome.USER_CREATE_FAILED,
                detail="Keycloak refused to look up the dev user",
                user_name=user.username,
            )
        if found is not None and not command.reset_existing:
            return self._result(
                EnumDevIdentityOutcome.USER_EXISTS_REFUSED,
                detail=(
                    f"user {user.username!r} already exists; its password is not reset "
                    "unless the command carries reset_existing"
                ),
                user_name=user.username,
            )

        user_id = await self._ensure_user(admin_token, password, user, found)
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

        request = ModelTenantProvisionRequest(
            command_id=uuid.uuid5(_TENANT_COMMAND_NAMESPACE, user.tenant_slug),
            tenant_slug=user.tenant_slug,
            name=user.username,
        )
        try:
            answer = await self._tenant_provisioner.provision(request)
        except TenantProvisionUnavailableError as exc:
            return self._result(
                EnumDevIdentityOutcome.RUNTIME_UNREACHABLE,
                detail=str(exc),
                user_name=user.username,
            )
        if answer.outcome not in _CREATED_OUTCOMES:
            return self._result(
                EnumDevIdentityOutcome.PROVISION_REFUSED,
                detail=f"the runtime answered {answer.outcome}: {answer.detail}",
                user_name=user.username,
            )
        if answer.tenant_id is None or answer.tenant_slug is None:
            return self._result(
                EnumDevIdentityOutcome.PROVISION_RESPONSE_INVALID,
                detail="the runtime's answer is missing tenant_id or tenant_slug",
                user_name=user.username,
            )
        return self._result(
            EnumDevIdentityOutcome.PROVISIONED,
            detail="the probe tenant was created through the runtime for the dev user (ownerless)",
            user_name=user.username,
            tenant_id=answer.tenant_id,
            tenant_slug=answer.tenant_slug,
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

    def _identity(
        self, command: ModelDevIdentityProvisionCommand
    ) -> _Identity | ModelDevIdentityProvisionResult:
        """The identity this run provisions: the command's values over the overlay user's."""
        user = self._overlay.user
        username = command.username or user.username
        if username != user.username and not (
            command.email and command.password_key_id
        ):
            return self._result(
                EnumDevIdentityOutcome.INVALID_IDENTITY_INPUT,
                detail=(
                    f"user {username!r} is not the overlay user; it needs its own email and "
                    "password_key_id, never the overlay user's"
                ),
                user_name=username,
            )
        if not _tenant_slug(username):
            return self._result(
                EnumDevIdentityOutcome.INVALID_IDENTITY_INPUT,
                detail=f"user {username!r} has no letter or digit to make a tenant slug from",
                user_name=username,
            )
        return _Identity(
            username=username,
            email=command.email or user.email,
            first_name=user.first_name,
            last_name=user.last_name,
            client_id=user.client_id,
            password_key_id=command.password_key_id or user.password_key_id,
            password_folder=user.password_folder,
            password_key=user.password_key,
        )

    async def _find_user(self, admin_token: SecretStr, username: str) -> str | None:
        """The user's id when it exists, None when it does not; raises when Keycloak did not answer usably."""
        kc = self._overlay.keycloak
        users_url = (
            f"{kc.base_url}{kc.admin_users_path_template.format(realm=kc.realm)}"
        )
        found = await self._send(
            ModelIdentityHttpRequest(
                method="GET",
                url=users_url,
                query={"username": username, "exact": "true"},
                bearer=admin_token,
                timeout_seconds=kc.timeout_seconds,
            )
        )
        if isinstance(found, str):
            raise _UserLookupFailedError
        if (
            found.status == 200
            and isinstance(found.json_body, list)
            and found.json_body
        ):
            user_id = found.json_body[0].get("id")
            if not isinstance(user_id, str):
                raise _UserLookupFailedError
            return user_id
        return None

    async def _ensure_user(
        self,
        admin_token: SecretStr,
        password: SecretStr,
        user: _Identity,
        existing_id: str | None,
    ) -> str | None:
        """Create the user, or, when ``existing_id`` names one (only a reset_existing run gets here), reset its password."""
        overlay = self._overlay
        kc = overlay.keycloak
        users_url = (
            f"{kc.base_url}{kc.admin_users_path_template.format(realm=kc.realm)}"
        )
        if isinstance(existing_id, str):
            user_id = existing_id
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
