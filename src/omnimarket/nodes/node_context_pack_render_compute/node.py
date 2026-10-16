# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeContextPackRenderCompute - contract-driven; all behaviour is in contract.yaml."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.models.context_pack_render import (
    ModelContextPackRenderRequest,
    ModelContextPackRenderResult,
)

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeContextPackRenderCompute(
    NodeCompute[ModelContextPackRenderRequest, ModelContextPackRenderResult]
):
    """Compute node: renders a ModelContextPack into the stable-first message list."""

    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)


__all__ = ["NodeContextPackRenderCompute"]
