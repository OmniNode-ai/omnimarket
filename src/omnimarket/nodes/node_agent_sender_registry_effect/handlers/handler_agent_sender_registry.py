# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Register, list and revoke one agent sender and its signing key in the settings store.

Revoke is a tombstone row, never a delete: the dev store is create-only and its adapter refuses delete.

The store folders are the agent message node's ``message_contract.settings_store`` block; the sender, principal and key
id are the request's. The keypair is generated here: the public half goes to the sender row, the seed to the key row,
and the seed never leaves the store (the result carries the public key only).
"""

from __future__ import annotations

import base64
import json
from typing import Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import SecretStr

from omnimarket.models.agent_message.model_agent_sender_registry_request import (
    EnumAgentSenderRegistryOperation,
    ModelAgentSenderRegistryRequest,
)
from omnimarket.models.agent_message.model_agent_sender_registry_result import (
    EnumAgentSenderRegistryOutcome,
    ModelAgentSenderRegistryResult,
)

Out = EnumAgentSenderRegistryOutcome


class ProtocolAgentRegistryStore(Protocol):
    """The write seam over the settings store: create-only put, delete, list and read, in process."""

    async def read(self, folder: str, name: str) -> SecretStr | None: ...
    async def create(self, folder: str, name: str, value: SecretStr) -> bool: ...
    async def delete(self, folder: str, name: str) -> bool: ...
    async def names(self, folder: str) -> tuple[str, ...]: ...


class HandlerAgentSenderRegistry:
    def __init__(
        self, store: ProtocolAgentRegistryStore, layout: dict[str, str]
    ) -> None:
        self._store = store
        self._senders = layout["senders_folder"]
        self._keys = layout["keys_folder"]
        self._revoked_prefix = layout["revoked_prefix"]

    async def handle(
        self, request: ModelAgentSenderRegistryRequest
    ) -> ModelAgentSenderRegistryResult:
        try:
            if request.operation is EnumAgentSenderRegistryOperation.LIST:
                names = await self._store.names(self._senders)
                tombstoned = {
                    n.removeprefix(self._revoked_prefix)
                    for n in names
                    if n.startswith(self._revoked_prefix)
                }
                return ModelAgentSenderRegistryResult(
                    outcome=Out.LISTED,
                    senders=tuple(
                        n
                        for n in names
                        if not n.startswith(self._revoked_prefix)
                        and n not in tombstoned
                    ),
                )
            if request.operation is EnumAgentSenderRegistryOperation.REVOKE:
                return await self._revoke(request.sender or "")
            return await self._register(request)
        except (OSError, PermissionError, ConnectionError, TimeoutError) as exc:
            return ModelAgentSenderRegistryResult(
                outcome=Out.STORE_FAILED, detail=type(exc).__name__
            )

    async def _register(
        self, request: ModelAgentSenderRegistryRequest
    ) -> ModelAgentSenderRegistryResult:
        name = request.sender or ""
        principal = request.principal or ""
        key_id = request.key_id or ""
        if (
            await self._store.read(self._senders, name) is not None
            or await self._store.read(self._senders, self._revoked_prefix + name)
            is not None
        ):
            return ModelAgentSenderRegistryResult(
                outcome=Out.ALREADY_REGISTERED, sender=name
            )
        private = Ed25519PrivateKey.generate()
        public_b64 = base64.b64encode(
            private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        ).decode()
        seed_b64 = base64.b64encode(private.private_bytes_raw()).decode()
        key_row = SecretStr(json.dumps({"key_id": key_id, "seed": seed_b64}))
        sender_row = SecretStr(
            json.dumps(
                {"principal": principal, "key_id": key_id, "public_key": public_b64}
            )
        )
        # The key row first: a sender row with no key is never visible to a reader.
        if not await self._store.create(self._keys, name, key_row):
            return ModelAgentSenderRegistryResult(
                outcome=Out.ALREADY_REGISTERED, sender=name, detail="key row exists"
            )
        if not await self._store.create(self._senders, name, sender_row):
            await self._store.delete(self._keys, name)
            return ModelAgentSenderRegistryResult(
                outcome=Out.ALREADY_REGISTERED, sender=name
            )
        return ModelAgentSenderRegistryResult(
            outcome=Out.REGISTERED,
            sender=name,
            principal=principal,
            key_id=key_id,
            public_key=public_b64,
        )

    async def _revoke(self, name: str) -> ModelAgentSenderRegistryResult:
        """Write a tombstone row; the settings store is create-only, so nothing is deleted and no delete grant is needed.

        A revoked name stays burnt: its rows remain in the store, readers refuse it as unknown_sender and it cannot be
        registered again, so a revoked key is never revived.
        """
        if await self._store.read(self._senders, name) is None:
            return ModelAgentSenderRegistryResult(
                outcome=Out.NOT_REGISTERED, sender=name
            )
        tombstone = SecretStr(json.dumps({"revoked": True}))
        if not await self._store.create(
            self._senders, self._revoked_prefix + name, tombstone
        ):
            return ModelAgentSenderRegistryResult(
                outcome=Out.NOT_REGISTERED, sender=name, detail="already revoked"
            )
        return ModelAgentSenderRegistryResult(outcome=Out.REVOKED, sender=name)


__all__: list[str] = ["HandlerAgentSenderRegistry", "ProtocolAgentRegistryStore"]
