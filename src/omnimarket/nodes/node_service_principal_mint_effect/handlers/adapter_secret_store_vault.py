# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real secret store vault adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import SecretStr

from omnimarket.models.secret_store.model_secret_store_request import (
    EnumSecretStoreOperation,
    ModelSecretStoreRequest,
)
from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
    ModelSecretStoreResult,
)
from omnimarket.nodes.node_secret_store_effect.adapters import IdentityFileStore
from omnimarket.nodes.node_secret_store_effect.handlers.handler_house_model_key import (
    HOST_ENV_FILE,
    host_env_overlay,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)

from ..protocols.protocol_service_principal_mint import (
    PrincipalVaultError,
)


class SecretStoreVault:
    """Implements ProtocolPrincipalVault using HandlerSecretStore."""

    def __init__(
        self,
        handler: HandlerSecretStore | None = None,
        home: Path | None = None,
        onex_home: Path | None = None,
    ) -> None:
        self._handler = handler
        self._home = home
        self._onex_home = onex_home

    async def _handle(self, request: ModelSecretStoreRequest) -> ModelSecretStoreResult:
        """Ask the store; with no injected handler and no store block, retry once through the host env identity."""
        handler = self._handler or HandlerSecretStore(onex_home=self._onex_home)
        result = await handler.handle(request)
        if (
            result.outcome is EnumSecretStoreOutcome.NOT_CONFIGURED
            and self._handler is None
        ):
            home = self._home or Path.home()
            overlay = host_env_overlay(home)
            if overlay is not None:
                result = await HandlerSecretStore(
                    overlay=overlay,
                    onex_home=self._onex_home,
                    bootstrap_store=IdentityFileStore(home / HOST_ENV_FILE),
                ).handle(request)
        return result

    async def read(self, folder: str, key: str) -> SecretStr | None:
        result = await self._handle(
            ModelSecretStoreRequest(
                operation=EnumSecretStoreOperation.RESOLVE, folder=folder, key=key
            )
        )
        if (
            result.outcome is EnumSecretStoreOutcome.RESOLVED
            and result.value is not None
        ):
            return result.value
        if result.outcome is EnumSecretStoreOutcome.NOT_FOUND:
            return None
        raise PrincipalVaultError(f"the secret store answered {result.outcome.value}")

    async def create(
        self, folder: str, key: str, value: SecretStr
    ) -> Literal["created", "exists"]:
        result = await self._handle(
            ModelSecretStoreRequest(
                operation=EnumSecretStoreOperation.CREATE,
                folder=folder,
                key=key,
                value=value,
            )
        )
        if result.outcome is EnumSecretStoreOutcome.CREATED:
            return "created"
        if result.outcome is EnumSecretStoreOutcome.EXISTS:
            return "exists"
        raise PrincipalVaultError(f"the secret store answered {result.outcome.value}")
