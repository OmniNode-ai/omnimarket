# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-driven compaction probe node; behavior belongs to the handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_compute import NodeCompute

from .models.model_compaction_probe import (
    ModelCompactionProbeRequest,
    ModelCompactionProbeResult,
)

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeCompactionProbeCompute(
    NodeCompute[ModelCompactionProbeRequest, ModelCompactionProbeResult]
):
    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)
