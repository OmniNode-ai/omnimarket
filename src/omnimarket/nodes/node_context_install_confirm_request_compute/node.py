# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeContextInstallConfirmRequestCompute - contract-driven; all behaviour is in contract.yaml."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.models.context_install_confirm_request import (
    ModelInstallConfirmAssemblyRequest,
    ModelInstallConfirmAssemblyResult,
)

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeContextInstallConfirmRequestCompute(
    NodeCompute[ModelInstallConfirmAssemblyRequest, ModelInstallConfirmAssemblyResult]
):
    """Compute node: assembles captured compact chunks and activations for install-confirm."""

    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)


__all__ = ["NodeContextInstallConfirmRequestCompute"]
