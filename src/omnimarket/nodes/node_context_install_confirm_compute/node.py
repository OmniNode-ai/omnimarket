# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeContextInstallConfirmCompute - contract-driven; all behaviour is in contract.yaml."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.models.context_install_confirm import (
    ModelInstallConfirmRequest,
    ModelInstallConfirmResult,
)

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeContextInstallConfirmCompute(
    NodeCompute[ModelInstallConfirmRequest, ModelInstallConfirmResult]
):
    """Compute node: derives installation facts and the current context from captured records."""

    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)


__all__ = ["NodeContextInstallConfirmCompute"]
