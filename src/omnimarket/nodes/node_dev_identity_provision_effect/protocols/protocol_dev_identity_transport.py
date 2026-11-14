# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The transport and key generator seams the dev identity provision handler asks of (OMN-24115).

The transport answers every HTTP status as a response; only when no response arrives does it raise ``ConnectionError``,
naming the exception type alone. The key generator wraps the seed node's generate-key operation.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.events.dev_identity_http import (
    ModelIdentityHttpRequest,
    ModelIdentityHttpResponse,
)
from omnimarket.models.secret_store.model_generate_key_result import (
    ModelGenerateKeyResult,
)


@runtime_checkable
class ProtocolDevIdentityTransport(Protocol):
    """The HTTP seam: one request at a time, a response for every status, a ConnectionError when none arrives."""

    async def send(
        self, request: ModelIdentityHttpRequest
    ) -> ModelIdentityHttpResponse: ...


@runtime_checkable
class ProtocolKeyGenerator(Protocol):
    """The seed node's generate-key operation, asked by the declared key id."""

    async def generate(self, name: str) -> ModelGenerateKeyResult: ...


__all__ = ["ProtocolDevIdentityTransport", "ProtocolKeyGenerator"]
