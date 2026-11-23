# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seams the dev proof tenant token handler asks of: the key reader, the signer and the seeder."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from pydantic import SecretStr

from omnimarket.nodes.node_dev_proof_tenant_token_effect.models.model_dev_proof_tenant_token import (
    EnumDevProofTokenStatus,
)


@runtime_checkable
class ProtocolSigningKeyReader(Protocol):
    """Reads one secret by folder and name from the dev secret store; ``None`` when absent."""

    async def read(self, folder: str, name: str) -> SecretStr | None: ...


@runtime_checkable
class ProtocolTokenSigner(Protocol):
    """Signs one claim set with the private key; the token comes back as a secret."""

    def sign(
        self, claims: Mapping[str, object], key: SecretStr, algorithm: str
    ) -> SecretStr: ...


@runtime_checkable
class ProtocolTokenSeeder(Protocol):
    """Writes secrets into the dev secret store through the seed node, never over an existing name."""

    async def exists(self, name: str) -> bool: ...

    async def seed(self, name: str, value: SecretStr) -> EnumDevProofTokenStatus: ...


__all__ = [
    "ProtocolSigningKeyReader",
    "ProtocolTokenSeeder",
    "ProtocolTokenSigner",
]
