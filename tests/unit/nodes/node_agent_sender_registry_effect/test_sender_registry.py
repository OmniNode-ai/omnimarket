# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The write path of the sender registry: register, list and revoke one sender and its key in the settings store."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr, ValidationError

from omnimarket.handlers.agent_message.signature import sign_message, verify_signature
from omnimarket.models.agent_message.model_agent_message import ModelAgentMessage
from omnimarket.models.agent_message.model_agent_sender_registry_request import (
    EnumAgentSenderRegistryOperation,
    ModelAgentSenderRegistryRequest,
)
from omnimarket.models.agent_message.model_agent_sender_registry_result import (
    EnumAgentSenderRegistryOutcome,
)
from omnimarket.nodes.node_agent_message_effect.adapters.adapter_settings_store_agent_directory import (
    SettingsStoreAgentDirectory,
)
from omnimarket.nodes.node_agent_message_effect.handlers.handler_agent_message import (
    load_contract,
)
from omnimarket.nodes.node_agent_sender_registry_effect.handlers.handler_agent_sender_registry import (
    HandlerAgentSenderRegistry,
)

Op = EnumAgentSenderRegistryOperation
Out = EnumAgentSenderRegistryOutcome


class MemoryStore:
    """In-memory store: create-only put, delete, list and read, by folder and name."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], str] = {}

    async def read(self, folder: str, name: str) -> SecretStr | None:
        value = self.rows.get((folder, name))
        return None if value is None else SecretStr(value)

    async def create(self, folder: str, name: str, value: SecretStr) -> bool:
        if (folder, name) in self.rows:
            return False
        self.rows[(folder, name)] = value.get_secret_value()
        return True

    async def delete(self, folder: str, name: str) -> bool:
        return self.rows.pop((folder, name), None) is not None

    async def names(self, folder: str) -> tuple[str, ...]:
        return tuple(sorted(n for f, n in self.rows if f == folder))


@pytest.fixture
def layout() -> dict[str, str]:
    return load_contract()["message_contract"]["settings_store"]


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def registry(store: MemoryStore, layout: dict[str, str]) -> HandlerAgentSenderRegistry:
    return HandlerAgentSenderRegistry(store, layout)


def _register(name: str, principal: str = "login-a") -> ModelAgentSenderRegistryRequest:
    return ModelAgentSenderRegistryRequest(
        operation=Op.REGISTER, sender=name, principal=principal, key_id=f"{name}-k1"
    )


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


async def test_register_writes_registry_and_key_rows(
    registry: HandlerAgentSenderRegistry,
    store: MemoryStore,
    layout: dict[str, str],
) -> None:
    result = await registry.handle(_register("alice"))
    assert result.outcome is Out.REGISTERED
    assert (layout["senders_folder"], "alice") in store.rows
    assert (layout["keys_folder"], "alice") in store.rows


async def test_registered_sender_signs_and_verifies_through_the_existing_directory(
    registry: HandlerAgentSenderRegistry,
    store: MemoryStore,
    layout: dict[str, str],
) -> None:
    await registry.handle(_register("alice", "login-a"))
    directory = SettingsStoreAgentDirectory(store, layout)
    sender = await directory.lookup("alice")
    key = await directory.signing_key("alice")
    assert sender is not None
    assert key is not None
    assert sender.principal == "login-a"
    msg = _message("alice")
    proof = sign_message(msg, key)
    assert verify_signature(msg, proof, sender.public_key)


async def test_forged_sender_stays_refused(
    registry: HandlerAgentSenderRegistry,
    store: MemoryStore,
    layout: dict[str, str],
) -> None:
    await registry.handle(_register("alice", "login-a"))
    await registry.handle(_register("mallory", "login-m"))
    directory = SettingsStoreAgentDirectory(store, layout)
    alice = await directory.lookup("alice")
    mallory_key = await directory.signing_key("mallory")
    assert alice is not None
    assert mallory_key is not None
    forged = _message("alice")
    proof = sign_message(forged, mallory_key)
    assert not verify_signature(forged, proof, alice.public_key)


async def test_register_twice_is_refused_and_keeps_the_first_key(
    registry: HandlerAgentSenderRegistry, store: MemoryStore, layout: dict[str, str]
) -> None:
    await registry.handle(_register("alice"))
    before = dict(store.rows)
    result = await registry.handle(_register("alice", "login-other"))
    assert result.outcome is Out.ALREADY_REGISTERED
    assert store.rows == before


async def test_result_never_carries_the_key(
    registry: HandlerAgentSenderRegistry, store: MemoryStore, layout: dict[str, str]
) -> None:
    result = await registry.handle(_register("alice"))
    seed = await store.read(layout["keys_folder"], "alice")
    assert seed is not None
    seed_text = seed.get_secret_value()
    seed_b64 = json.loads(seed_text)["seed"]
    assert base64.b64decode(seed_b64)  # a real seed was stored
    dumped = result.model_dump_json() + repr(result) + str(result)
    assert seed_b64 not in dumped
    assert result.public_key  # the public half is reported, the seed never


async def test_list_returns_names_only(
    registry: HandlerAgentSenderRegistry,
) -> None:
    await registry.handle(_register("bob"))
    await registry.handle(_register("alice"))
    result = await registry.handle(ModelAgentSenderRegistryRequest(operation=Op.LIST))
    assert result.outcome is Out.LISTED
    assert result.senders == ("alice", "bob")


async def test_revoke_removes_both_rows_and_the_sender_is_unknown_again(
    registry: HandlerAgentSenderRegistry, store: MemoryStore, layout: dict[str, str]
) -> None:
    await registry.handle(_register("alice"))
    result = await registry.handle(
        ModelAgentSenderRegistryRequest(operation=Op.REVOKE, sender="alice")
    )
    assert result.outcome is Out.REVOKED
    directory = SettingsStoreAgentDirectory(store, layout)
    assert await directory.lookup("alice") is None
    assert await directory.signing_key("alice") is None


async def test_revoke_unknown_sender_is_typed(
    registry: HandlerAgentSenderRegistry,
) -> None:
    result = await registry.handle(
        ModelAgentSenderRegistryRequest(operation=Op.REVOKE, sender="ghost")
    )
    assert result.outcome is Out.NOT_REGISTERED


@pytest.mark.parametrize("bad", ["a/b", "", "x y", "../k"])
def test_register_request_rejects_a_bad_sender_name(bad: str) -> None:
    with pytest.raises(ValidationError):
        ModelAgentSenderRegistryRequest(
            operation=Op.REGISTER, sender=bad, principal="p", key_id="k"
        )


def test_register_needs_principal_and_key_id() -> None:
    with pytest.raises(ValidationError):
        ModelAgentSenderRegistryRequest(operation=Op.REGISTER, sender="alice")
