# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev identity provision handler (OMN-24115): one named dev Keycloak user, a generated password, one probe tenant.

The tenant is created by the runtime's tenant provision command, asked over the bus through the tenant provisioner
seam; no separate HTTP API is called. The tenant is ownerless: the runtime's owner path does not exist yet.

With a ``tenant_id`` on the command (OMN-26476) the run binds the user to that EXISTING tenant instead: it reads the tenant
through the tenant reader seam (the runtime's tenant read command), never asks for a new tenant, sets the user's tenant
attributes, signs in and reads the audience and tenant claims back out of the token it got. Every attribute, claim and
audience name comes from the contract's ``tenant_binding`` block.

The password value moves only between the key generator and the Keycloak calls; it is never printed, logged, serialized
or returned. The result carries names and ids only. The dev-only guard refuses, before any secret read or HTTP call,
when the lane or realm is not the one the contract declares.

A run deletes the user it created when it ends: after every typed failure past the create (and after an exception), and
after a success only when the command declares the user disposable. A user that existed before the run and a protected
username are never deleted by a run. The ``lookup`` and ``delete`` verbs (OMN-23563) read and remove one named user.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from pydantic import SecretStr

from omnimarket.handlers.delegation_scenario_settings import ProtocolSettingStore
from omnimarket.models.model_dev_identity_http import (
    ModelIdentityHttpRequest,
)
from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
)
from omnimarket.nodes.node_dev_identity_provision_effect.handlers.token_claims import (
    read_token_claims,
)
from omnimarket.nodes.node_dev_identity_provision_effect.models.model_dev_identity_provision import (
    EnumDevIdentityCleanup,
    EnumDevIdentityOutcome,
    ModelDevIdentityProvisionCommand,
    ModelDevIdentityProvisionResult,
)
from omnimarket.nodes.node_dev_identity_provision_effect.models.model_tenant_binding import (
    ModelTenantBinding,
)
from omnimarket.nodes.node_dev_identity_provision_effect.models.model_tenant_provision_wire import (
    ModelTenantProvisionRequest,
    ModelTenantReadRequest,
)
from omnimarket.nodes.node_dev_identity_provision_effect.overlay import (
    ModelDevIdentityProvisionOverlay,
)
from omnimarket.nodes.node_dev_identity_provision_effect.protocols.protocol_dev_identity_transport import (
    ProtocolDevIdentityTransport,
    ProtocolKeyGenerator,
    ProtocolTenantProvisioner,
    ProtocolTenantReader,
    TenantProvisionUnavailableError,
)

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"

# The replay key of a user's tenant command: one user always asks the same thing, so a rerun is answered as a replay.
_TENANT_COMMAND_NAMESPACE = uuid.UUID("5c0b1e7e-2f0a-4c6e-9a53-0d1e6f6b3a10")
_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")
_CREATED_OUTCOMES = frozenset({"created", "already_exists"})
_OK_OUTCOMES = frozenset(
    {
        EnumDevIdentityOutcome.PROVISIONED,
        EnumDevIdentityOutcome.READY,
        EnumDevIdentityOutcome.USER_FOUND,
        EnumDevIdentityOutcome.USER_ABSENT,
        EnumDevIdentityOutcome.USER_DELETED,
        EnumDevIdentityOutcome.BOUND,
        EnumDevIdentityOutcome.USER_PROVISIONED,
    }
)


def _tenant_slug(username: str) -> str:
    """A DNS-label form of the username; the runtime's slug rule decides whether it is a usable slug."""
    return _SLUG_UNSAFE.sub("-", username.lower()).strip("-")


@dataclass(frozen=True)
class _BoundTenant:
    """The existing tenant a bind run attaches the user to, as the runtime's read answered it."""

    tenant_id: UUID
    tenant_slug: str


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


def _load_user_only(contract_path: Path | None) -> dict[str, str] | None:
    """The contract's ``user_only`` block (realm role and role mapping path templates), or None when it is not declared."""
    document = yaml.safe_load(
        (contract_path or _CONTRACT_PATH).read_text(encoding="utf-8")
    )
    block = document.get("user_only")
    if not isinstance(block, dict):
        return None
    paths = {
        key: block.get(key)
        for key in ("realm_roles_path_template", "role_mapping_path_template")
    }
    if not all(isinstance(value, str) and value for value in paths.values()):
        return None
    return {key: str(value) for key, value in paths.items()}


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
        tenant_reader: ProtocolTenantReader | None = None,
        tenant_binding: ModelTenantBinding | None = None,
        contract_path: Path | None = None,
    ) -> None:
        self._overlay = overlay
        self._transport = transport
        self._settings_store = settings_store
        self._key_generator = key_generator
        self._tenant_provisioner = tenant_provisioner
        self._tenant_reader = tenant_reader
        self._tenant_binding = tenant_binding
        self._contract_path = contract_path

    def _result(
        self,
        outcome: EnumDevIdentityOutcome,
        *,
        detail: str,
        user_name: str | None = None,
        tenant_id: str | None = None,
        tenant_slug: str | None = None,
        user_id: str | None = None,
        cleanup: EnumDevIdentityCleanup | None = None,
        expected_secret: str | None = None,
        token_audience: str | None = None,
        claims_verified: list[str] | None = None,
    ) -> ModelDevIdentityProvisionResult:
        ok = outcome in _OK_OUTCOMES
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
            user_id=user_id,
            cleanup=cleanup,
            expected_secret=expected_secret,
            token_audience=token_audience,
            claims_verified=claims_verified,
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

        if command.lookup_user or command.delete_user:
            return await self._user_verb(command)

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

        user_only_paths: dict[str, str] | None = None
        if command.user_only:
            user_only_paths = _load_user_only(self._contract_path)
            if user_only_paths is None:
                return self._result(
                    EnumDevIdentityOutcome.USER_ONLY_UNCONFIGURED,
                    detail=(
                        "user_only needs the contract's user_only block (realm role and role mapping paths); "
                        "nothing was read or sent"
                    ),
                    user_name=user.username,
                )

        bind: tuple[ModelTenantBinding, ProtocolTenantReader] | None = None
        if command.tenant_id is not None:
            if self._tenant_binding is None or self._tenant_reader is None:
                return self._result(
                    EnumDevIdentityOutcome.TENANT_BINDING_UNCONFIGURED,
                    detail=(
                        "binding a user to an existing tenant needs the contract's tenant_binding block and a "
                        "tenant reader; nothing was read or sent"
                    ),
                    user_name=user.username,
                )
            bind = (self._tenant_binding, self._tenant_reader)

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

        # An unknown or inactive tenant is refused here, before the Keycloak admin token and any user write.
        bound_tenant: _BoundTenant | None = None
        if bind is not None and command.tenant_id is not None:
            read = await self._read_tenant(
                bind[1], bind[0], command.tenant_id, user.username
            )
            if isinstance(read, ModelDevIdentityProvisionResult):
                return read
            bound_tenant = read

        token_url = f"{kc.base_url}{kc.token_path_template.format(realm=kc.realm)}"
        admin_token = await self._admin_bearer(token_url, admin_secret)
        if isinstance(admin_token, ModelDevIdentityProvisionResult):
            return admin_token

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
                cleanup=EnumDevIdentityCleanup.NOT_CREATED,
            )

        # Only a user this run created is this run's to delete; an existing user (reset_existing) never is.
        created_id = user_id if found is None else None
        try:
            if user_only_paths is not None and command.read_role is not None:
                result = await self._grant_role_and_verify(
                    user_only_paths,
                    admin_token,
                    token_url,
                    user,
                    user_id,
                    password,
                    command.read_role,
                )
            elif bind is not None and bound_tenant is not None:
                result = await self._bind_and_verify(
                    bind[0],
                    admin_token,
                    token_url,
                    user,
                    user_id,
                    password,
                    bound_tenant,
                )
            else:
                result = await self._sign_in_and_provision(token_url, user, password)
        except BaseException:
            if created_id is not None and not self._is_protected(user.username):
                await self._delete_after_exception(
                    admin_token, created_id, user.username
                )
            raise
        cleanup = await self._cleanup(command, admin_token, user, created_id, result)
        return result.model_copy(update={"cleanup": cleanup})

    async def _grant_role_and_verify(
        self,
        paths: dict[str, str],
        admin_token: SecretStr,
        token_url: str,
        user: _Identity,
        user_id: str,
        password: SecretStr,
        role: str,
    ) -> ModelDevIdentityProvisionResult:
        """User-only mode: grant the named realm role, then sign in once; no tenant is asked for, read or bound."""
        kc = self._overlay.keycloak
        roles_url = (
            f"{kc.base_url}{paths['realm_roles_path_template'].format(realm=kc.realm)}"
        )
        listed = await self._send(
            ModelIdentityHttpRequest(
                method="GET",
                url=roles_url,
                bearer=admin_token,
                timeout_seconds=kc.timeout_seconds,
            )
        )
        role_id: str | None = None
        if not isinstance(listed, str) and listed.status == 200:
            body = listed.json_body
            for entry in body if isinstance(body, list) else []:
                if (
                    isinstance(entry, dict)
                    and entry.get("name") == role
                    and isinstance(entry.get("id"), str)
                ):
                    role_id = entry["id"]
        if role_id is None:
            return self._result(
                EnumDevIdentityOutcome.ROLE_NOT_FOUND,
                detail=f"the realm role {role!r} was not in the dev realm's role list",
                user_name=user.username,
            )
        mapping_url = f"{kc.base_url}{paths['role_mapping_path_template'].format(realm=kc.realm, user_id=user_id)}"
        mapped = await self._send(
            ModelIdentityHttpRequest(
                method="POST",
                url=mapping_url,
                bearer=admin_token,
                json_body=[{"id": role_id, "name": role}],
                timeout_seconds=kc.timeout_seconds,
            )
        )
        if isinstance(mapped, str) or mapped.status not in (200, 204):
            return self._result(
                EnumDevIdentityOutcome.ROLE_ASSIGN_FAILED,
                detail=f"Keycloak refused to grant the realm role {role!r} to the dev user",
                user_name=user.username,
            )
        user_token = await self._sign_in(token_url, user, password)
        if isinstance(user_token, ModelDevIdentityProvisionResult):
            return user_token
        return self._result(
            EnumDevIdentityOutcome.USER_PROVISIONED,
            detail=f"the dev user was created with the realm role {role!r} and no tenant; it signed in once",
            user_name=user.username,
            user_id=user_id,
        )

    async def _sign_in_and_provision(
        self,
        token_url: str,
        user: _Identity,
        password: SecretStr,
    ) -> ModelDevIdentityProvisionResult:
        """Sign in as the user and ask the runtime for the probe tenant; every failure is a typed result."""
        user_token = await self._sign_in(token_url, user, password)
        if isinstance(user_token, ModelDevIdentityProvisionResult):
            return user_token

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

    async def _sign_in(
        self, token_url: str, user: _Identity, password: SecretStr
    ) -> SecretStr | ModelDevIdentityProvisionResult:
        """The user's access token from the password grant, or the typed result that says why there is none."""
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
        return SecretStr(user_token.get_secret_value())

    async def _read_tenant(
        self,
        reader: ProtocolTenantReader,
        binding: ModelTenantBinding,
        tenant_id: UUID,
        username: str,
    ) -> _BoundTenant | ModelDevIdentityProvisionResult:
        """Read the existing tenant over the runtime's read command; every refusal is a typed result."""
        try:
            answer = await reader.read(
                ModelTenantReadRequest(command_id=uuid.uuid4(), tenant_id=tenant_id)
            )
        except TenantProvisionUnavailableError as exc:
            return self._result(
                EnumDevIdentityOutcome.RUNTIME_UNREACHABLE,
                detail=str(exc),
                user_name=username,
            )
        if answer.outcome == "not_found":
            return self._result(
                EnumDevIdentityOutcome.TENANT_NOT_FOUND,
                detail=f"the runtime knows no tenant {tenant_id}; no user was made",
                user_name=username,
            )
        if answer.outcome != "found":
            return self._result(
                EnumDevIdentityOutcome.PROVISION_REFUSED,
                detail=f"the runtime answered {answer.outcome} to the tenant read: {answer.detail}",
                user_name=username,
            )
        if answer.tenant_id != str(tenant_id) or not answer.tenant_slug:
            return self._result(
                EnumDevIdentityOutcome.PROVISION_RESPONSE_INVALID,
                detail="the runtime's read answer names another tenant id or no tenant_slug",
                user_name=username,
            )
        if answer.provisioning_state != binding.required_provisioning_state:
            return self._result(
                EnumDevIdentityOutcome.TENANT_NOT_ACTIVE,
                detail=(
                    f"tenant {tenant_id} is in state {answer.provisioning_state!r}, not "
                    f"{binding.required_provisioning_state!r}; no user was made"
                ),
                user_name=username,
            )
        return _BoundTenant(tenant_id=tenant_id, tenant_slug=answer.tenant_slug)

    async def _bind_and_verify(
        self,
        binding: ModelTenantBinding,
        admin_token: SecretStr,
        token_url: str,
        user: _Identity,
        user_id: str,
        password: SecretStr,
        tenant: _BoundTenant,
    ) -> ModelDevIdentityProvisionResult:
        """Set the user's tenant attributes, sign in, and read the audience and tenant claims back out of the token."""
        attributes = {
            binding.tenant_id_attribute: [str(tenant.tenant_id)],
            binding.tenant_slug_attribute: [tenant.tenant_slug],
        }
        if not await self._set_user_attributes(admin_token, user_id, attributes):
            return self._result(
                EnumDevIdentityOutcome.USER_ATTRIBUTES_FAILED,
                detail="Keycloak refused to read or update the dev user's attributes",
                user_name=user.username,
            )
        user_token = await self._sign_in(token_url, user, password)
        if isinstance(user_token, ModelDevIdentityProvisionResult):
            return user_token
        claims = read_token_claims(user_token.get_secret_value())
        verified = ["aud", binding.tenant_id_claim, binding.tenant_slug_claim]
        failing = _failing_claims(claims, binding, tenant)
        if failing:
            return self._result(
                EnumDevIdentityOutcome.TOKEN_CLAIMS_MISMATCH,
                detail=(
                    "the signed-in token does not carry what the inference-credentials route checks; "
                    f"missing or different: {', '.join(failing)}"
                ),
                user_name=user.username,
            )
        return self._result(
            EnumDevIdentityOutcome.BOUND,
            detail=(
                "the dev user is bound to the existing tenant; its token carries the declared audience and "
                "both tenant claims"
            ),
            user_name=user.username,
            tenant_id=str(tenant.tenant_id),
            tenant_slug=tenant.tenant_slug,
            token_audience=binding.audience,
            claims_verified=verified,
        )

    async def _set_user_attributes(
        self, admin_token: SecretStr, user_id: str, attributes: dict[str, list[str]]
    ) -> bool:
        """Merge ``attributes`` into the user's own and write the user back; True only on a 2xx update."""
        kc = self._overlay.keycloak
        user_url = f"{kc.base_url}{kc.admin_users_path_template.format(realm=kc.realm)}/{user_id}"
        current = await self._send(
            ModelIdentityHttpRequest(
                method="GET",
                url=user_url,
                bearer=admin_token,
                timeout_seconds=kc.timeout_seconds,
            )
        )
        if (
            isinstance(current, str)
            or current.status != 200
            or not isinstance(current.json_body, dict)
        ):
            return False
        held = current.json_body.get("attributes")
        merged: dict[str, Any] = dict(held) if isinstance(held, dict) else {}
        merged.update(attributes)
        update = await self._send(
            ModelIdentityHttpRequest(
                method="PUT",
                url=user_url,
                bearer=admin_token,
                json_body={**current.json_body, "attributes": merged},
                timeout_seconds=kc.timeout_seconds,
            )
        )
        return not isinstance(update, str) and update.status in (200, 204)

    async def _admin_bearer(
        self, token_url: str, admin_secret: SecretStr
    ) -> SecretStr | ModelDevIdentityProvisionResult:
        """The Keycloak admin token, or the typed result that says why there is none."""
        admin_token = await self._admin_token(
            token_url, self._overlay.admin, admin_secret
        )
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
        return SecretStr(admin_token.get_secret_value())

    def _is_protected(self, username: str) -> bool:
        return username.lower() in {
            name.lower() for name in self._overlay.protected_usernames
        }

    async def _user_verb(
        self, command: ModelDevIdentityProvisionCommand
    ) -> ModelDevIdentityProvisionResult:
        """``lookup`` reads one named user; ``delete`` removes one and reads it gone. The dev guard has already passed."""
        overlay = self._overlay
        kc = overlay.keycloak
        admin = overlay.admin
        username = command.username or ""
        if command.delete_user and (
            username.lower() == overlay.user.username.lower()
            or self._is_protected(username)
        ):
            return self._result(
                EnumDevIdentityOutcome.USER_PROTECTED,
                detail=(
                    f"user {username!r} is the overlay user or in the overlay's protected_usernames; "
                    "nothing was read or sent, and a delete never touches it"
                ),
                user_name=username,
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
        token_url = f"{kc.base_url}{kc.token_path_template.format(realm=kc.realm)}"
        admin_token = await self._admin_bearer(token_url, admin_secret)
        if isinstance(admin_token, ModelDevIdentityProvisionResult):
            return admin_token
        try:
            user_id = await self._find_user(admin_token, username)
        except _UserLookupFailedError:
            return self._result(
                EnumDevIdentityOutcome.KEYCLOAK_UNREACHABLE,
                detail="Keycloak did not answer the user lookup with a usable body",
                user_name=username,
            )
        if user_id is None:
            return self._result(
                EnumDevIdentityOutcome.USER_ABSENT,
                detail=f"user {username!r} is not in realm {kc.realm!r} (never existed or already deleted)",
                user_name=username,
            )
        if command.lookup_user:
            return self._result(
                EnumDevIdentityOutcome.USER_FOUND,
                detail=f"user {username!r} exists in realm {kc.realm!r}",
                user_name=username,
                user_id=user_id,
            )
        if not await self._delete_user(admin_token, username, user_id):
            return self._result(
                EnumDevIdentityOutcome.USER_DELETE_FAILED,
                detail=f"user {username!r} was not removed from realm {kc.realm!r}",
                user_name=username,
                user_id=user_id,
            )
        return self._result(
            EnumDevIdentityOutcome.USER_DELETED,
            detail=f"user {username!r} was deleted from realm {kc.realm!r} and read back gone",
            user_name=username,
            user_id=user_id,
        )

    async def _delete_user(
        self, admin_token: SecretStr, username: str, user_id: str
    ) -> bool:
        """Delete the user by id, then read it back: True only when the realm no longer holds the username."""
        kc = self._overlay.keycloak
        users_url = (
            f"{kc.base_url}{kc.admin_users_path_template.format(realm=kc.realm)}"
        )
        response = await self._send(
            ModelIdentityHttpRequest(
                method="DELETE",
                url=f"{users_url}/{user_id}",
                bearer=admin_token,
                timeout_seconds=kc.timeout_seconds,
            )
        )
        if isinstance(response, str) or response.status not in (204, 404):
            return False
        try:
            return await self._find_user(admin_token, username) is None
        except _UserLookupFailedError:
            return False

    async def _delete_after_exception(
        self, admin_token: SecretStr, user_id: str, username: str
    ) -> None:
        """Best effort while an exception is already on its way out; it never replaces that exception."""
        try:
            deleted = await self._delete_user(admin_token, username, user_id)
        except Exception:
            logger.warning("cleanup of dev user %s raised", username)
            return
        logger.info("cleanup after an exception: user=%s deleted=%s", username, deleted)

    async def _cleanup(
        self,
        command: ModelDevIdentityProvisionCommand,
        admin_token: SecretStr,
        user: _Identity,
        created_id: str | None,
        result: ModelDevIdentityProvisionResult,
    ) -> EnumDevIdentityCleanup:
        """Delete the user this run created after a failure, or after a success the command declared disposable."""
        if created_id is None:
            return EnumDevIdentityCleanup.NOT_CREATED
        if self._is_protected(user.username):
            return EnumDevIdentityCleanup.KEPT_PROTECTED
        if result.ok and not command.disposable:
            return EnumDevIdentityCleanup.KEPT
        if await self._delete_user(admin_token, user.username, created_id):
            return EnumDevIdentityCleanup.DELETED
        logger.warning(
            "dev user %s was created by this run and not deleted", user.username
        )
        return EnumDevIdentityCleanup.DELETE_FAILED

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
        if (
            isinstance(found, str)
            or found.status != 200
            or not isinstance(found.json_body, list)
        ):
            raise _UserLookupFailedError
        if not found.json_body:
            return None
        user_id = found.json_body[0].get("id")
        if not isinstance(user_id, str):
            raise _UserLookupFailedError
        return user_id

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


def _failing_claims(
    claims: dict[str, Any] | None, binding: ModelTenantBinding, tenant: _BoundTenant
) -> list[str]:
    """The names of the claims the route would reject: the audience and the two tenant claims, by name only."""
    if claims is None:
        return ["aud", binding.tenant_id_claim, binding.tenant_slug_claim]
    failing: list[str] = []
    audience = claims.get("aud")
    held = [audience] if isinstance(audience, str) else audience
    if not isinstance(held, list) or binding.audience not in held:
        failing.append("aud")
    if claims.get(binding.tenant_id_claim) != str(tenant.tenant_id):
        failing.append(binding.tenant_id_claim)
    if claims.get(binding.tenant_slug_claim) != tenant.tenant_slug:
        failing.append(binding.tenant_slug_claim)
    return failing


__all__ = ["HandlerDevIdentityProvision"]
