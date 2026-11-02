# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative arm producer; effects are wired by the contract and container."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_effect import NodeEffect

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeCompactionArmRunEffect(NodeEffect):
    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)
