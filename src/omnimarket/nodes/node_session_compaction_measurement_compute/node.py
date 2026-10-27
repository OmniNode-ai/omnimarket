# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-driven offline measurement node; behavior belongs to the handler."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_compute import NodeCompute

from .models.model_measurement import ModelMeasurementRequest, ModelMeasurementResult

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeSessionCompactionMeasurementCompute(
    NodeCompute[ModelMeasurementRequest, ModelMeasurementResult]
):
    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)
