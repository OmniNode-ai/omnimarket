# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""This machine's own credential database as a ``ProtocolSecretStore`` (OMN-21921).

The adapter a customer machine's overlay names. The database is the SQLite
store ``onex secret set`` already writes (``LocalByokCredentialStore``), keyed
by the reference alone. The node's handler names a secret by an absolute path,
``<folder>/<name>``; the contract's provider and tenant namespaces both give a
reference as the name, so the name is the key here and the folder is the
node's addressing, which a one-table database has no use for.

Create-only, like every adapter of the node: an existing key is reported and
never read or replaced. There is no folder to list and no delete.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnimarket.inference.local_byok_credential_adapter import (
        LocalByokCredentialStore,
    )

PROVIDER = "local"


class LocalDatabaseStore:
    """Read and create-only write over the machine-local credential database."""

    def __init__(self, store: LocalByokCredentialStore | None = None) -> None:
        # Imported here: the inference package imports its resolver, which imports this node.
        from omnimarket.inference.local_byok_credential_adapter import (
            LocalByokCredentialStore as _Database,
        )

        self._store = store or _Database()

    async def get_secret(self, key: str) -> str | None:
        return await self._store.get_secret(key.rpartition("/")[2])

    async def set_secret(self, key: str, value: str) -> bool:
        name = key.rpartition("/")[2]
        if await self._store.get_secret(name) is not None:
            return False
        return await self._store.set_secret(name, value)

    async def delete_secret(self, key: str) -> bool:
        raise RuntimeError("the local store is written create-only")

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        del prefix
        return []

    async def health_check(self) -> bool:
        return True

    async def close(self, timeout_seconds: float = 30.0) -> None:
        del timeout_seconds
