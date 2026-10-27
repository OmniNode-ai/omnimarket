"""Container-owned pure compute node; all execution stays with the lab runner."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_compute import NodeCompute

from .models.model_check import ModelCheckRequest, ModelCheckVerdict

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeModelWorkContractCompute(NodeCompute[ModelCheckRequest, ModelCheckVerdict]):
    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)
