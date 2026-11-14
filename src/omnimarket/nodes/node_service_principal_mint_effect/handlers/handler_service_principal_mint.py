# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Mint a broker service principal login and deliver its credential env file.

``handle(request: ModelServicePrincipalMintRequest) -> ModelServicePrincipalMintResult``.
Creates ONE broker SCRAM login for a named internal service, generates its password straight
into the secret store (create-only, never printed, never in a result/log/exception), and
delivers a credential env file to a declared read-only folder on each declared host. It NEVER
rotates or changes an existing login's password.

The password is never printed, serialized, logged or put in any exception text, log record,
detail or result. Logs name the principal and outcome words only.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from typing import Literal

from pydantic import SecretStr

from ..models.model_service_principal_mint import (
    ModelServicePrincipalDeliveryReading,
    ModelServicePrincipalMintOverlay,
    ModelServicePrincipalMintRequest,
    ModelServicePrincipalMintResult,
)
from ..protocols.protocol_service_principal_mint import (
    BrokerLoginAdminError,
    PrincipalVaultError,
    ProtocolBrokerLoginAdmin,
    ProtocolCredentialDelivery,
    ProtocolPrincipalVault,
)

logger = logging.getLogger(__name__)


class HandlerServicePrincipalMint:
    """EFFECT: mint one declared service principal login and deliver its credential file.

    Args:
        overlay: The overlay declaring all service principals.
        admin: The broker login administration boundary.
        vault: The principal secret store boundary.
        delivery: The credential file delivery boundary.
        token_source: The random token source; injectable so a test can hold the value it returned.
    """

    def __init__(
        self,
        overlay: ModelServicePrincipalMintOverlay,
        admin: ProtocolBrokerLoginAdmin,
        vault: ProtocolPrincipalVault,
        delivery: ProtocolCredentialDelivery,
        token_source: Callable[[int], str] = secrets.token_urlsafe,
    ) -> None:
        self._overlay = overlay
        self._admin = admin
        self._vault = vault
        self._delivery = delivery
        self._token_source = token_source

    def _result(
        self,
        request: ModelServicePrincipalMintRequest,
        outcome: Literal["created", "exists", "would-create", "refused", "failed"],
        *,
        refusal_kind: Literal[
            "",
            "undeclared",
            "login_without_secret",
            "broker_unreachable",
            "vault_unavailable",
            "delivery_failed",
        ] = "",
        broker_login: Literal[
            "created", "existed", "would-create", "absent", "failed"
        ] = "absent",
        secret: Literal[
            "created", "existed", "would-create", "absent", "failed"
        ] = "absent",
        deliveries: tuple[ModelServicePrincipalDeliveryReading, ...] = (),
        detail: str = "",
    ) -> ModelServicePrincipalMintResult:
        """One result; the log line names the principal and the outcome words only."""
        logger.info(
            "mint principal=%s outcome=%s refusal=%s",
            request.principal,
            outcome,
            refusal_kind,
        )
        return ModelServicePrincipalMintResult(
            principal=request.principal,
            outcome=outcome,
            refusal_kind=refusal_kind,
            broker_login=broker_login,
            secret=secret,
            deliveries=deliveries,
            detail=detail,
            correlation_id=request.correlation_id,
        )

    async def handle(
        self, request: ModelServicePrincipalMintRequest
    ) -> ModelServicePrincipalMintResult:
        """Mint in the fixed order: list logins, read the secret, decide, create, deliver."""
        declaration = next(
            (p for p in self._overlay.principals if p.name == request.principal), None
        )
        if declaration is None:
            return self._result(
                request,
                "refused",
                refusal_kind="undeclared",
                detail="the overlay declares no principal with this name; nothing was touched",
            )
        apply = not request.check_only
        folder, key = declaration.vault_folder, declaration.password_key

        try:
            broker_has = declaration.name in await self._admin.list_logins()
        except BrokerLoginAdminError as exc:
            return self._result(
                request,
                "refused",
                refusal_kind="broker_unreachable",
                detail=f"broker unreachable: {type(exc).__name__}",
            )
        try:
            stored = await self._vault.read(folder, key)
        except PrincipalVaultError as exc:
            return self._result(
                request,
                "refused",
                refusal_kind="vault_unavailable",
                detail=f"vault unavailable: {exc}",
            )

        if broker_has and stored is None:
            return self._result(
                request,
                "refused",
                refusal_kind="login_without_secret",
                broker_login="existed",
                detail=(
                    f"the login {declaration.name!r} exists with no stored password; nothing was "
                    "changed; replacing a password is a separate approved action"
                ),
            )

        secret_state: Literal["created", "existed", "would-create"] = "existed"
        login_state: Literal["created", "existed", "would-create"] = "existed"
        if stored is None:
            # The password exists only between here and the store, then in the delivered file.
            password = SecretStr(self._token_source(declaration.password_bytes))
            secret_state = "created" if apply else "would-create"
            if apply:
                try:
                    if await self._vault.create(folder, key, password) == "exists":
                        # Another run stored one first: use that value, never ours.
                        raced = await self._vault.read(folder, key)
                        if raced is None:
                            raise PrincipalVaultError("the key vanished after create")
                        password, secret_state = raced, "existed"
                except PrincipalVaultError as exc:
                    return self._result(
                        request,
                        "refused",
                        refusal_kind="vault_unavailable",
                        detail=f"vault unavailable: {exc}",
                    )
        else:
            password = stored

        if not broker_has:
            login_state = "created" if apply else "would-create"
            if apply:
                try:
                    await self._admin.create_login(declaration.name, password)
                except BrokerLoginAdminError as exc:
                    return self._result(
                        request,
                        "failed",
                        broker_login="failed",
                        secret=secret_state,
                        detail=f"broker login creation failed: {type(exc).__name__}",
                    )

        content = SecretStr(
            f"{declaration.username_env}={declaration.name}\n"
            f"{declaration.password_env}={password.get_secret_value()}\n"
        )
        readings = tuple(
            [
                await self._delivery.deliver(target, content, apply=apply)
                for target in declaration.deliveries
            ]
        )
        if any(r.state == "failed" for r in readings):
            return self._result(
                request,
                "failed",
                refusal_kind="delivery_failed",
                broker_login=login_state,
                secret=secret_state,
                deliveries=readings,
                detail="one or more deliveries failed",
            )

        outcome: Literal["created", "exists", "would-create"] = "exists"
        if secret_state != "existed" or login_state != "existed":
            outcome = "created" if apply else "would-create"
        return self._result(
            request,
            outcome,
            broker_login=login_state,
            secret=secret_state,
            deliveries=readings,
        )
