# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Revoke through the real adapter chain: registry handler -> secret store handler -> Infisical adapter.

The in-memory store of ``test_sender_registry`` allows delete, which the dev store does not: its adapter is create-only
and refuses delete. These tests drive the same chain the CLI builds, with only the HTTP exchange faked as a create-only
Infisical (create answers 200 or 409, read answers 200 or 404, delete is never taken), so revoke must work with no delete.
"""

from __future__ import annotations

import urllib.parse
from typing import Any

import pytest
from pydantic import SecretStr

from omnimarket.models.agent_message.model_agent_sender_registry_request import (
    EnumAgentSenderRegistryOperation,
    ModelAgentSenderRegistryRequest,
)
from omnimarket.models.agent_message.model_agent_sender_registry_result import (
    EnumAgentSenderRegistryOutcome,
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
from omnimarket.nodes.node_secret_store_effect.adapters.handler_infisical_http import (
    HandlerInfisicalHttp,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_secret_store import (
    HandlerSecretStore,
)

Op = EnumAgentSenderRegistryOperation
Out = EnumAgentSenderRegistryOutcome
_LOGIN = "/api/v1/auth/universal-auth/login"
_RAW = "/api/v3/secrets/raw"


class CreateOnlyInfisical:
    """An HTTP exchange that behaves like the dev store: create-only identity, delete forbidden."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], str] = {}
        self.methods: list[str] = []

    async def __call__(
        self, method: str, url: str, **kwargs: Any
    ) -> tuple[int, dict[str, Any]]:
        self.methods.append(method)
        path = urllib.parse.urlsplit(url).path
        if path == _LOGIN:
            return 200, {"accessToken": "t", "expiresIn": 300}
        if method == "DELETE":
            return 403, {"message": "forbidden"}
        scope = (kwargs.get("params") or kwargs.get("payload") or {}).get(
            "secretPath", "/"
        )
        if path == _RAW and method == "GET":
            names = [n for (f, n) in self.rows if f == scope]
            return 200, {"secrets": [{"secretKey": n} for n in names]}
        name = urllib.parse.unquote(path.removeprefix(_RAW + "/"))
        if method == "GET":
            row = self.rows.get((scope, name))
            if row is None:
                return 404, {"message": "not found"}
            return 200, {"secret": {"secretKey": name, "secretValue": row}}
        if method == "POST":
            if (scope, name) in self.rows:
                return 409, {"message": "already exists"}
            self.rows[(scope, name)] = kwargs["payload"]["secretValue"]
            return 200, {}
        return 405, {}


@pytest.fixture
def exchange() -> CreateOnlyInfisical:
    return CreateOnlyInfisical()


@pytest.fixture
def layout() -> dict[str, str]:
    return load_contract()["message_contract"]["settings_store"]


@pytest.fixture
def folders(exchange: CreateOnlyInfisical) -> SecretStoreSenderFolders:
    adapter = HandlerInfisicalHttp(
        address="https://store.example.test",
        project_id="00000000-0000-0000-0000-000000000001",
        environment="dev",
        client_id=SecretStr("id"),
        client_secret=SecretStr("secret"),
        exchange=exchange,
        timeout_seconds=5.0,
    )
    return SecretStoreSenderFolders(
        handler=HandlerSecretStore(
            store=adapter, overlay=ModelSecretStoreOverlay(provider="infisical")
        )
    )


def _req(op: Op, name: str = "alice") -> ModelAgentSenderRegistryRequest:
    if op is Op.REGISTER:
        return ModelAgentSenderRegistryRequest(
            operation=op, sender=name, principal="login-a", key_id=f"{name}-k1"
        )
    return ModelAgentSenderRegistryRequest(operation=op, sender=name)


async def test_revoke_on_create_only_store_makes_the_sender_unknown(
    folders: SecretStoreSenderFolders,
    layout: dict[str, str],
    exchange: CreateOnlyInfisical,
) -> None:
    registry = HandlerAgentSenderRegistry(folders, layout)
    directory = SettingsStoreAgentDirectory(folders, layout)
    assert (await registry.handle(_req(Op.REGISTER))).outcome is Out.REGISTERED
    assert await directory.lookup("alice") is not None

    result = await registry.handle(_req(Op.REVOKE))

    assert result.outcome is Out.REVOKED, result
    assert await directory.lookup("alice") is None
    assert await directory.signing_key("alice") is None
    assert "DELETE" not in exchange.methods


async def test_revoked_name_is_not_listed_and_cannot_be_registered_again(
    folders: SecretStoreSenderFolders, layout: dict[str, str]
) -> None:
    registry = HandlerAgentSenderRegistry(folders, layout)
    await registry.handle(_req(Op.REGISTER))
    await registry.handle(_req(Op.REGISTER, "bob"))
    await registry.handle(_req(Op.REVOKE))

    listed = await registry.handle(ModelAgentSenderRegistryRequest(operation=Op.LIST))
    assert listed.senders == ("bob",)
    again = await registry.handle(_req(Op.REGISTER))
    assert again.outcome is Out.ALREADY_REGISTERED


async def test_second_revoke_and_revoke_of_unknown_are_not_registered(
    folders: SecretStoreSenderFolders, layout: dict[str, str]
) -> None:
    registry = HandlerAgentSenderRegistry(folders, layout)
    await registry.handle(_req(Op.REGISTER))
    await registry.handle(_req(Op.REVOKE))
    assert (await registry.handle(_req(Op.REVOKE))).outcome is Out.NOT_REGISTERED
    assert (
        await registry.handle(_req(Op.REVOKE, "ghost"))
    ).outcome is Out.NOT_REGISTERED
