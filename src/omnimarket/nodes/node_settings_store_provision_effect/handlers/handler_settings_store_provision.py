# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The settings store provision handler (OMN-24052): the store's logins, applied to the Valkey the overlay names.

``handle(request: ModelSettingsStoreProvisionCommand) -> ModelSettingsStoreProvisionResult``. For each login the contract lets
this run apply, the password is generated into the secret store by its declared id (create-only: an existing password is reused,
never replaced), read once, and the ``ACL SETUSER`` row ``valkey_acl.py`` renders for that role is applied through the admin
connection. The password is never returned, logged or serialized; the result carries names only.

Guards, all read from the contract file or the overlay, none from code: a lane the ``dev_only`` block does not declare is refused
before any secret read or Valkey call; a role the block does not list is refused (consent covers the writer alone); a database
that already holds keys outside the store's prefix is refused, so the store never shares a database with another user; a login
that already exists with a password other than the stored one is refused (a rotation is another act, never this one).
A login that already authenticates with the stored password is left untouched; one whose row is stale but accepts the stored
password has its row applied again (the password does not change).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from pydantic import SecretStr

from omnimarket.handlers.delegation_scenario_settings import ProtocolSettingStore
from omnimarket.handlers.overlay_store.valkey_acl import (
    ModelValkeyStoreLogins,
    render_login_acl_command,
)
from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
)
from omnimarket.nodes.node_dev_identity_provision_effect.protocols.protocol_dev_identity_transport import (
    ProtocolKeyGenerator,
)
from omnimarket.nodes.node_settings_store_provision_effect.models.model_settings_store_provision import (
    EnumSettingsStoreOutcome,
    ModelSettingsStoreProvisionCommand,
    ModelSettingsStoreProvisionResult,
)
from omnimarket.nodes.node_settings_store_provision_effect.overlay import (
    ModelLoginOverlay,
    ModelSettingsStoreProvisionOverlay,
)
from omnimarket.nodes.node_settings_store_provision_effect.protocols.protocol_valkey_acl_applier import (
    ProtocolValkeyAclApplier,
    ValkeyUnavailableError,
)

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"

_OK = (
    EnumSettingsStoreOutcome.PROVISIONED,
    EnumSettingsStoreOutcome.ALREADY_PROVISIONED,
)


def _load_dev_only(contract_path: Path | None) -> dict[str, Any]:
    document = yaml.safe_load((contract_path or _CONTRACT_PATH).read_text("utf-8"))
    block: dict[str, Any] = document["dev_only"]
    return block


class HandlerSettingsStoreProvision:
    """Generates the store logins' passwords into the secret store and applies their access-list rows."""

    def __init__(
        self,
        *,
        overlay: ModelSettingsStoreProvisionOverlay,
        settings_store: ProtocolSettingStore,
        key_generator: ProtocolKeyGenerator,
        applier_factory: Callable[[SecretStr], ProtocolValkeyAclApplier],
        contract_path: Path | None = None,
    ) -> None:
        self._overlay = overlay
        self._settings_store = settings_store
        self._key_generator = key_generator
        self._applier_factory = applier_factory
        self._contract_path = contract_path

    def _result(
        self,
        outcome: EnumSettingsStoreOutcome,
        *,
        lane: str,
        detail: str,
        applied: tuple[str, ...] = (),
        unchanged: tuple[str, ...] = (),
        secret_folder: str | None = None,
        secret_key: str | None = None,
    ) -> ModelSettingsStoreProvisionResult:
        logger.info(
            "settings store provision outcome=%s lane=%s applied=%s unchanged=%s",
            outcome.value,
            lane,
            applied,
            unchanged,
        )
        return ModelSettingsStoreProvisionResult(
            ok=outcome in _OK,
            outcome=outcome,
            detail=detail,
            lane=lane,
            applied=applied,
            unchanged=unchanged,
            secret_folder=secret_folder,
            secret_key=secret_key,
        )

    async def _password(
        self, login: ModelLoginOverlay
    ) -> tuple[SecretStr, str, str] | str:
        """The login's stored password with its folder and key name, or the reason it is unavailable."""
        generated = await self._key_generator.generate(login.password_key_id)
        if (
            generated.outcome
            not in (EnumSecretStoreOutcome.CREATED, EnumSecretStoreOutcome.EXISTS)
            or generated.folder is None
            or generated.key is None
        ):
            return f"the key generator returned {generated.outcome.value} for {login.password_key_id!r}"
        secret = await self._settings_store.read(generated.folder, generated.key)
        if secret is None:
            return f"the generated password {generated.folder}/{generated.key} is not in the store"
        return secret, generated.folder, generated.key

    async def handle(
        self, request: ModelSettingsStoreProvisionCommand
    ) -> ModelSettingsStoreProvisionResult:
        lane = request.lane
        declared = _load_dev_only(self._contract_path)
        lanes: list[str] = list(declared["lanes"])
        if lane not in lanes or self._overlay.lane not in lanes:
            return self._result(
                EnumSettingsStoreOutcome.REFUSED_LANE_NOT_ALLOWED,
                lane=lane,
                detail=(
                    f"command lane {lane!r} and overlay lane {self._overlay.lane!r} must both be a lane the contract "
                    f"declares {lanes}; nothing was read or written"
                ),
            )
        allowed: list[str] = list(declared["logins"])
        refused = [r.value for r in request.logins if r.value not in allowed]
        if refused:
            return self._result(
                EnumSettingsStoreOutcome.REFUSED_LOGIN_NOT_DECLARED,
                lane=lane,
                detail=f"login role(s) {refused} are not roles the contract lets a run apply {allowed}; nothing was read or written",
            )

        store = self._overlay.store
        admin = await self._settings_store.read(
            store.admin_password_folder, store.admin_password_key
        )
        if admin is None:
            return self._result(
                EnumSettingsStoreOutcome.ADMIN_UNAVAILABLE,
                lane=lane,
                detail=f"the admin password {store.admin_password_folder}/{store.admin_password_key} is not in the secret store",
            )
        applier = self._applier_factory(admin)
        logins = ModelValkeyStoreLogins(
            key_prefix=store.key_prefix,
            writer_login=self._overlay.logins.writer.name,
            reader_login=self._overlay.logins.reader.name,
            cache_login=self._overlay.logins.cache.name,
            cache_key_pattern=store.cache_key_pattern,
        )

        applied: list[str] = []
        unchanged: list[str] = []
        folder: str | None = None
        key: str | None = None
        try:
            counts = await applier.keyspace_counts()
            for role in request.logins:
                login: ModelLoginOverlay = getattr(self._overlay.logins, role.value)
                password = await self._password(login)
                if isinstance(password, str):
                    return self._result(
                        EnumSettingsStoreOutcome.PASSWORD_UNAVAILABLE,
                        lane=lane,
                        detail=password,
                        applied=tuple(applied),
                        unchanged=tuple(unchanged),
                    )
                secret, folder, key = password
                value = secret.get_secret_value()
                if await applier.user_exists(login.name):
                    if await applier.authenticates(login.name, value):
                        unchanged.append(login.name)
                        continue
                    if await applier.accepts_password(login.name, value):
                        # The stored password is right and the row is not the one valkey_acl.py renders today: apply it again.
                        # The password is unchanged, so this is a repair of the row and never a rotation.
                        await applier.set_user(
                            render_login_acl_command(logins, role.value, password=value)
                        )
                        if await applier.authenticates(login.name, value):
                            applied.append(login.name)
                            continue
                        return self._result(
                            EnumSettingsStoreOutcome.NOT_VERIFIED,
                            lane=lane,
                            detail=f"the row for {login.name!r} was applied again and the login did not authenticate",
                            applied=tuple(applied),
                            unchanged=tuple(unchanged),
                            secret_folder=folder,
                            secret_key=key,
                        )
                    return self._result(
                        EnumSettingsStoreOutcome.REFUSED_LOGIN_HOLDS_OTHER_PASSWORD,
                        lane=lane,
                        detail=(
                            f"the login {login.name!r} exists and does not accept the stored password; "
                            "nothing was replaced (a rotation is another act)"
                        ),
                        applied=tuple(applied),
                        unchanged=tuple(unchanged),
                        secret_folder=folder,
                        secret_key=key,
                    )
                if counts.get(store.database, 0) > 0:
                    return self._result(
                        EnumSettingsStoreOutcome.REFUSED_DATABASE_IN_USE,
                        lane=lane,
                        detail=(
                            f"database {store.database} already holds {counts[store.database]} key(s) and the store's "
                            f"login {login.name!r} does not exist; another user holds it; nothing was applied"
                        ),
                        applied=tuple(applied),
                        unchanged=tuple(unchanged),
                    )
                await applier.set_user(
                    render_login_acl_command(logins, role.value, password=value)
                )
                if not await applier.authenticates(login.name, value):
                    return self._result(
                        EnumSettingsStoreOutcome.NOT_VERIFIED,
                        lane=lane,
                        detail=f"the row for {login.name!r} was applied and the login did not authenticate",
                        applied=tuple(applied),
                        unchanged=tuple(unchanged),
                        secret_folder=folder,
                        secret_key=key,
                    )
                applied.append(login.name)
        except ValkeyUnavailableError as exc:
            return self._result(
                EnumSettingsStoreOutcome.STORE_UNAVAILABLE,
                lane=lane,
                detail=f"the Valkey was unavailable: {exc}",
                applied=tuple(applied),
                unchanged=tuple(unchanged),
            )
        outcome = (
            EnumSettingsStoreOutcome.PROVISIONED
            if applied
            else EnumSettingsStoreOutcome.ALREADY_PROVISIONED
        )
        return self._result(
            outcome,
            lane=lane,
            detail="the login row(s) are in place; the password is only in the secret store",
            applied=tuple(applied),
            unchanged=tuple(unchanged),
            secret_folder=folder,
            secret_key=key,
        )


__all__ = ["HandlerSettingsStoreProvision"]
