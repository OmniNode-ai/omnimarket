# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory sender directory and Ed25519 key helpers for the sender-proof tests (no credential is real)."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from omnimarket.models.agent_message.model_agent_signing_key import (
    ModelAgentSigningKey,
)
from omnimarket.models.agent_message.model_registered_agent_sender import (
    ModelRegisteredAgentSender,
)


@dataclass(frozen=True)
class TestAgent:
    __test__ = False

    name: str
    principal: str
    key_id: str
    private_key: Ed25519PrivateKey

    @property
    def public_key_b64(self) -> str:
        raw = self.private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return base64.b64encode(raw).decode()

    def registered(self) -> ModelRegisteredAgentSender:
        return ModelRegisteredAgentSender(
            name=self.name,
            principal=self.principal,
            key_id=self.key_id,
            public_key=self.public_key_b64,
        )

    def signing_key(self) -> ModelAgentSigningKey:
        from pydantic import SecretStr

        raw = self.private_key.private_bytes_raw()
        return ModelAgentSigningKey(
            key_id=self.key_id, seed=SecretStr(base64.b64encode(raw).decode())
        )


def make_agent(name: str, principal: str, key_id: str | None = None) -> TestAgent:
    return TestAgent(
        name=name,
        principal=principal,
        key_id=key_id or f"{name}-key-1",
        private_key=Ed25519PrivateKey.generate(),
    )


@dataclass
class FakeAgentDirectory:
    """Both injected seams in one fake: the registry of senders and the signing keys this host holds."""

    registered: dict[str, ModelRegisteredAgentSender] = field(default_factory=dict)
    held_keys: dict[str, ModelAgentSigningKey] = field(default_factory=dict)
    legacy_allowed: bool = True

    async def lookup(self, name: str) -> ModelRegisteredAgentSender | None:
        return self.registered.get(name)

    async def accepts_legacy(self) -> bool:
        return self.legacy_allowed

    async def signing_key(self, name: str) -> ModelAgentSigningKey | None:
        return self.held_keys.get(name)
