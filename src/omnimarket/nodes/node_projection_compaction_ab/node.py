# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-driven node for the compaction A/B read model."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_reducer import NodeReducer

from omnimarket.nodes.node_projection_compaction_ab.models import (
    ModelAbProjectionRequest,
    ModelAbProjectionResult,
)

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeProjectionCompactionAb(
    NodeReducer[ModelAbProjectionRequest, ModelAbProjectionResult]
):
    """The contract routes boundary events and build entries to pure handlers."""

    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)


__all__ = ["NodeProjectionCompactionAb"]
