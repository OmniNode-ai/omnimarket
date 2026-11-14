# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The key generator adapter: the seed node's generate-key operation, asked by the declared id.

``generate(name)`` is ``HandlerGenerateKey.handle(ModelGenerateKeyRequest(name=name))``: the value is
generated inside the seed node, stored create-only and never returned, so this adapter cannot leak it
either.
"""

from __future__ import annotations

from omnimarket.models.secret_store.model_generate_key_request import (
    ModelGenerateKeyRequest,
)
from omnimarket.models.secret_store.model_generate_key_result import (
    ModelGenerateKeyResult,
)
from omnimarket.models.secret_store.model_secret_store_overlay import (
    ModelSecretStoreOverlay,
)
from omnimarket.nodes.node_secret_store_effect.handlers.handler_generate_key import (
    HandlerGenerateKey,
)


class StoreKeyGenerator:
    """``ProtocolKeyGenerator`` over the seed node's ``HandlerGenerateKey``.

    With ``overlay`` the seed node stores through that store block alone; with none it uses the
    machine's own ``secret_store`` block first and the host env file second, exactly as the seed
    node's own CLI does.
    """

    def __init__(self, *, overlay: ModelSecretStoreOverlay | None = None) -> None:
        self._handler = HandlerGenerateKey(overlay=overlay)

    async def generate(self, name: str) -> ModelGenerateKeyResult:
        return await self._handler.handle(ModelGenerateKeyRequest(name=name))


__all__ = ["StoreKeyGenerator"]
