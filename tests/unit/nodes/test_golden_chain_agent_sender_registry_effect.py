# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: register a sender through the secret store node, sign, read it as verified, forge, revoke."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.handlers.agent_message.signature import sign_message, verify_signature
from omnimarket.models.agent_message.model_agent_message import ModelAgentMessage
from omnimarket.models.agent_message.model_agent_sender_registry_request import (
    EnumAgentSenderRegistryOperation as Op,
)
from omnimarket.models.agent_message.model_agent_sender_registry_request import (
    ModelAgentSenderRegistryRequest,
)
from omnimarket.models.agent_message.model_agent_sender_registry_result import (
    EnumAgentSenderRegistryOutcome as Out,
)
from omnimarket.models.secret_store.model_secret_store_overlay import (
    ModelSecretStoreOverlay,
)
from omnimarket.nodes.node_agent_message_effect.adapters.adapter_settings_store_agent_directory import (
    SettingsStoreAgentDirectory,
)
from omnimarket.nodes.node_agent_message_effect.handlers.handler_agent_message import (
    load_contract,
)
from omnimarket.nodes.node_agent_sender_registry_effect.adapters.adapter_secret_store_senders import (
    SecretStoreSenderFolders,
)
from omnimarket.nodes.node_agent_sender_registry_effect.handlers.handler_agent_sender_registry import (
    HandlerAgentSenderRegistry,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)

pytestmark = pytest.mark.unit


class _DictStore:
    """A dict-backed ProtocolSecretStore: create-only set, delete and prefix list."""

    def __init__(self) -> None:
        self.secrets: dict[str, str] = {}

    async def get_secret(self, key: str) -> str | None:
        return self.secrets.get(key)

    async def set_secret(self, key: str, value: str) -> bool:
        if key in self.secrets:
            return False
        self.secrets[key] = value
        return True

    async def delete_secret(self, key: str) -> bool:
        return self.secrets.pop(key, None) is not None

    async def list_keys(self, prefix: str | None = None) -> list[str]:
        base = (prefix or "/").rstrip("/")
        return [k for k in self.secrets if k.startswith(base + "/")]


def _message(sender: str) -> ModelAgentMessage:
    return ModelAgentMessage.model_validate(
        {
            "message_id": "m-1",
            "from": sender,
            "to": "all",
            "thread": "t",
            "kind": "message",
            "text": "hello",
            "sent_at": datetime(2026, 11, 25, tzinfo=UTC),
            "platform": "claude",
            "message_schema_version": 1,
        }
    )


async def test_register_sign_verify_forge_revoke_through_the_secret_store_node() -> (
    None
):
    backing = _DictStore()
    adapter = SecretStoreSenderFolders(
        handler=HandlerSecretStore(
            store=backing, overlay=ModelSecretStoreOverlay(provider="local")
        )
    )
    layout = load_contract()["message_contract"]["settings_store"]
    registry = HandlerAgentSenderRegistry(adapter, layout)
    for name, login in (("alice", "login-a"), ("mallory", "login-m")):
        done = await registry.handle(
            ModelAgentSenderRegistryRequest(
                operation=Op.REGISTER, sender=name, principal=login, key_id=f"{name}-1"
            )
        )
        assert done.outcome is Out.REGISTERED
    listed = await registry.handle(ModelAgentSenderRegistryRequest(operation=Op.LIST))
    assert listed.senders == ("alice", "mallory")

    directory = SettingsStoreAgentDirectory(adapter, layout)
    alice = await directory.lookup("alice")
    alice_key = await directory.signing_key("alice")
    mallory_key = await directory.signing_key("mallory")
    assert alice is not None
    assert alice_key is not None
    assert mallory_key is not None
    good = _message("alice")
    assert verify_signature(good, sign_message(good, alice_key), alice.public_key)
    assert not verify_signature(good, sign_message(good, mallory_key), alice.public_key)

    revoked = await registry.handle(
        ModelAgentSenderRegistryRequest(operation=Op.REVOKE, sender="alice")
    )
    assert revoked.outcome is Out.REVOKED
    assert await directory.lookup("alice") is None
