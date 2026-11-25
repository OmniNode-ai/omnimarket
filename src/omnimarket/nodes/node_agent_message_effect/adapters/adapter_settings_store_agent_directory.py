# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The sender registry, the signing keys and the cutover flag, read from the settings store.

Nothing about a sender is spelled in code: the store folders and names come from the node contract's
``message_contract.settings_store`` block, and every value (the registered principal, key id and public key of an
agent name, the key a host holds for it, whether the legacy wire shape is still accepted) is a store row.
"""

from __future__ import annotations

import json
from typing import Protocol

from pydantic import SecretStr, ValidationError

from omnimarket.models.agent_message.model_agent_signing_key import (
    ModelAgentSigningKey,
)
from omnimarket.models.agent_message.model_registered_agent_sender import (
    ModelRegisteredAgentSender,
)


class ProtocolAgentSettingStore(Protocol):
    """The store seam: one value by folder and name, in process."""

    async def read(self, folder: str, name: str) -> SecretStr | None: ...


class SettingsStoreAgentDirectory:
    """Registry, signing keys and cutover flag over one settings store."""

    def __init__(
        self, store: ProtocolAgentSettingStore, layout: dict[str, str]
    ) -> None:
        self._store = store
        self._senders_folder = layout["senders_folder"]
        self._keys_folder = layout["keys_folder"]
        self._revoked_prefix = layout["revoked_prefix"]
        self._policy_folder = layout["policy_folder"]
        self._accept_legacy_name = layout["accept_legacy_name"]

    async def _revoked(self, name: str) -> bool:
        """A tombstone row marks a revoked name; the store is create-only, so the sender row itself stays."""
        return (
            await self._store.read(self._senders_folder, self._revoked_prefix + name)
            is not None
        )

    async def lookup(self, name: str) -> ModelRegisteredAgentSender | None:
        if await self._revoked(name):
            return None
        raw = await self._store.read(self._senders_folder, name)
        if raw is None:
            return None
        try:
            row = json.loads(raw.get_secret_value())
            return ModelRegisteredAgentSender.model_validate({**row, "name": name})
        except (ValueError, TypeError, ValidationError) as exc:
            raise ValueError(f"sender registry row for {name!r} is unreadable") from exc

    async def accepts_legacy(self) -> bool:
        """True until the store row says otherwise: the legacy shape stays readable until the cutover."""
        raw = await self._store.read(self._policy_folder, self._accept_legacy_name)
        if raw is None:
            return True
        return raw.get_secret_value().strip().lower() not in {"false", "0", "no"}

    async def signing_key(self, name: str) -> ModelAgentSigningKey | None:
        if await self._revoked(name):
            return None
        raw = await self._store.read(self._keys_folder, name)
        if raw is None:
            return None
        try:
            return ModelAgentSigningKey.model_validate(
                json.loads(raw.get_secret_value())
            )
        except (ValueError, TypeError, ValidationError) as exc:
            raise ValueError(f"signing key row for {name!r} is unreadable") from exc


__all__: list[str] = ["ProtocolAgentSettingStore", "SettingsStoreAgentDirectory"]
