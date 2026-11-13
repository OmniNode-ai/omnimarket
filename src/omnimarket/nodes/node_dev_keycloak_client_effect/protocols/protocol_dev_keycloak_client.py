# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The transport, key generator and bootstrap credential seams the dev keycloak client handler asks of (OMN-24237).

The transport answers every HTTP status as a response; only when no response arrives does it raise ``ConnectionError``,
naming the exception type alone. The key generator wraps the seed node's generate-key operation. The bootstrap
credential source answers with only the wanted names the host env file holds, and never logs a value.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from pydantic import SecretStr

from omnimarket.models.secret_store.model_generate_key_result import (
    ModelGenerateKeyResult,
)
from omnimarket.nodes.node_dev_keycloak_client_effect.models.model_keycloak_client_http import (
    ModelKeycloakHttpRequest,
    ModelKeycloakHttpResponse,
)


@runtime_checkable
class ProtocolKeycloakTransport(Protocol):
    """The HTTP seam: one request at a time, a response for every status, a ConnectionError when none arrives."""

    async def send(
        self, request: ModelKeycloakHttpRequest
    ) -> ModelKeycloakHttpResponse: ...


@runtime_checkable
class ProtocolKeyGenerator(Protocol):
    """The seed node's generate-key operation, asked by the declared key id."""

    async def generate(self, name: str) -> ModelGenerateKeyResult: ...


@runtime_checkable
class ProtocolBootstrapCredentialSource(Protocol):
    """The host env file seam: only the wanted names found, each value carried as a secret."""

    async def read(self, names: Sequence[str]) -> dict[str, SecretStr]: ...


__all__ = [
    "ProtocolBootstrapCredentialSource",
    "ProtocolKeyGenerator",
    "ProtocolKeycloakTransport",
]
