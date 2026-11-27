# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The central tenant key store behind ``onex secret register-tenant-key`` (OMN-24562).

Moved out of the command file so it does not grow; behaviour is unchanged.
"""

from __future__ import annotations

from pydantic import SecretStr

from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)
from omnimarket.nodes.node_secret_store_effect.models import (
    EnumSecretStoreOperation,
    EnumSecretStoreOutcome,
    ModelSecretStoreRequest,
    ModelSecretStoreResult,
)

#: OMN-20994. The secret store node's contract maps this reference namespace to
#: the tenant folder, so this command holds a reference and never a folder.
TENANT_REFERENCE_PREFIX = "secret://onex/tenant/"


class CentralStoreRefusedError(Exception):
    """The secret store node did not create the key. Carries no value."""

    def __init__(self, result: ModelSecretStoreResult) -> None:
        detail = f" ({result.detail})" if result.detail else ""
        super().__init__(
            f"the central secret store answered {result.outcome.value}{detail}"
        )
        self.outcome = result.outcome


class CentralTenantKeyStore:
    """``ProtocolSecretStore`` over the secret store node's handler (OMN-20994).

    A tenant key is written in process, create-only, under the minted
    reference in the tenant namespace. Anything but ``created`` raises
    :class:`CentralStoreRefusedError`, so a refusal stops the registration
    before any event is published.
    """

    def __init__(self, handler: HandlerSecretStore) -> None:
        self._handler = handler

    async def get_secret(self, key: str) -> str | None:
        result = await self._handler.handle(
            ModelSecretStoreRequest(
                operation=EnumSecretStoreOperation.RESOLVE,
                reference=f"{TENANT_REFERENCE_PREFIX}{key}",
            )
        )
        if result.outcome is EnumSecretStoreOutcome.NOT_FOUND:
            return None
        if result.value is None:
            raise CentralStoreRefusedError(result)
        return result.value.get_secret_value()

    async def set_secret(self, key: str, value: str) -> bool:
        result = await self._handler.handle(
            ModelSecretStoreRequest(
                operation=EnumSecretStoreOperation.CREATE,
                reference=f"{TENANT_REFERENCE_PREFIX}{key}",
                value=SecretStr(value),
            )
        )
        if result.outcome is not EnumSecretStoreOutcome.CREATED:
            raise CentralStoreRefusedError(result)
        return True

    async def delete_secret(self, key: str) -> bool:
        raise PermissionError("the central secret store is written create-only")

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        raise PermissionError("the central tenant store is not listed from here")

    async def health_check(self) -> bool:
        return True

    async def close(self, timeout_seconds: float | None = None) -> None:
        return None
