# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Register, list and revoke one agent sender and its signing key in the settings store.

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

    async def handle(
        self, request: ModelAgentSenderRegistryRequest
    ) -> ModelAgentSenderRegistryResult:
        try:
            if request.operation is EnumAgentSenderRegistryOperation.LIST:
                return ModelAgentSenderRegistryResult(
                    outcome=Out.LISTED, senders=await self._store.names(self._senders)
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
        if await self._store.read(self._senders, name) is not None:
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
        sender_gone = await self._store.delete(self._senders, name)
        key_gone = await self._store.delete(self._keys, name)
        if not (sender_gone or key_gone):
            return ModelAgentSenderRegistryResult(
                outcome=Out.NOT_REGISTERED, sender=name
            )
        return ModelAgentSenderRegistryResult(outcome=Out.REVOKED, sender=name)


__all__: list[str] = ["HandlerAgentSenderRegistry", "ProtocolAgentRegistryStore"]
