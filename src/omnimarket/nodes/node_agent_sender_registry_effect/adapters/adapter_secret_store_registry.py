# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The registry's write seam over the secret store node's handler: the machine's overlay first, the host env file second."""

from __future__ import annotations

from pathlib import Path

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


class SecretStoreRegistryAdapter:
    """Read, create-only put, delete and list one folder of the settings store."""

    def __init__(
        self,
        *,
        onex_home: Path | None = None,
        home: Path | None = None,
        handler: HandlerSecretStore | None = None,
    ) -> None:
        self._handler = handler
        self._onex_home = onex_home
        self._home = home

    async def _send(self, request: ModelSecretStoreRequest) -> ModelSecretStoreResult:
        if self._handler is not None:
            return await self._handler.handle(request)
        result = await HandlerSecretStore(onex_home=self._onex_home).handle(request)
        if result.outcome is not EnumSecretStoreOutcome.NOT_CONFIGURED:
            return result
        home = self._home or Path.home()
        overlay = host_env_overlay(home)
        if overlay is None:
            return result
        return await HandlerSecretStore(
            overlay=overlay,
            onex_home=self._onex_home,
            bootstrap_store=IdentityFileStore(home / HOST_ENV_FILE),
        ).handle(request)

    @staticmethod
    def _require(
        result: ModelSecretStoreResult, ok: set[EnumSecretStoreOutcome]
    ) -> None:
        if result.outcome not in ok:
            raise ConnectionError(f"settings store answered {result.outcome.value}")

    async def read(self, folder: str, name: str) -> SecretStr | None:
        result = await self._send(
            ModelSecretStoreRequest(
                operation=EnumSecretStoreOperation.RESOLVE, folder=folder, key=name
            )
        )
        if result.outcome is EnumSecretStoreOutcome.NOT_FOUND:
            return None
        self._require(result, {EnumSecretStoreOutcome.RESOLVED})
        return result.value

    async def create(self, folder: str, name: str, value: SecretStr) -> bool:
        result = await self._send(
            ModelSecretStoreRequest(
                operation=EnumSecretStoreOperation.CREATE,
                folder=folder,
                key=name,
                value=value,
            )
        )
        self._require(
            result, {EnumSecretStoreOutcome.CREATED, EnumSecretStoreOutcome.EXISTS}
        )
        return result.outcome is EnumSecretStoreOutcome.CREATED

    async def delete(self, folder: str, name: str) -> bool:
        result = await self._send(
            ModelSecretStoreRequest(
                operation=EnumSecretStoreOperation.DELETE, folder=folder, key=name
            )
        )
        if result.outcome is EnumSecretStoreOutcome.NOT_FOUND:
            return False
        self._require(result, {EnumSecretStoreOutcome.DELETED})
        return True

    async def names(self, folder: str) -> tuple[str, ...]:
        result = await self._send(
            ModelSecretStoreRequest(
                operation=EnumSecretStoreOperation.LIST, folder=folder
            )
        )
        self._require(result, {EnumSecretStoreOutcome.LISTED})
        return result.keys


__all__: list[str] = ["SecretStoreRegistryAdapter"]
