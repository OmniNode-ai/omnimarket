# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolves the deploy agent's signing key from the secret store (OMN-24611)."""

from __future__ import annotations

from pydantic import SecretStr

from omnimarket.models.secret_store.model_secret_store_request import (
    EnumSecretStoreOperation,
    ModelSecretStoreRequest,
)
from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
)
from omnimarket.nodes.node_branch_deploy_command_effect.models.model_signing_key_reading import (
    ModelSigningKeyReading,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)


class SigningKeyStoreReader:
    """Resolves one key from the secret store through the store handler; the key never lands in a detail.

    Args:
        folder: The folder holding the key, relative to the store's root folder.
        key: The NAME of the key in the store, never a value.
        handler: The store handler to resolve through, built with the
            overlay's ``secret_store`` addressing block.
    """

    def __init__(self, folder: str, key: str, handler: HandlerSecretStore) -> None:
        self._folder = folder
        self._key = key
        self._handler = handler

    async def read(self) -> ModelSigningKeyReading:
        try:
            result = await self._handler.handle(
                ModelSecretStoreRequest(
                    operation=EnumSecretStoreOperation.RESOLVE,
                    folder=self._folder,
                    key=self._key,
                )
            )
        except Exception as exc:
            return ModelSigningKeyReading(status="refused", detail=type(exc).__name__)
        value = result.value.get_secret_value().strip() if result.value else ""
        if result.outcome is EnumSecretStoreOutcome.RESOLVED and value:
            return ModelSigningKeyReading(status="read", key=SecretStr(value))
        return ModelSigningKeyReading(status="refused", detail=result.outcome.value)
