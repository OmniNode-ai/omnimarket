# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeEntrypointLibCompute - contract-driven; all behaviour is in contract.yaml."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.nodes.node_entrypoint_lib_compute.models.model_entrypoint_lib import (
    ModelEntrypointLibRequest,
    ModelEntrypointLibResult,
)

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeEntrypointLibCompute(
    NodeCompute[ModelEntrypointLibRequest, ModelEntrypointLibResult]
):
    """Compute node: Decide the output lines, exit codes and budgets of the one-command skill entrypoint helpers from caller-supplied facts."""

    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)


__all__ = ["NodeEntrypointLibCompute"]
