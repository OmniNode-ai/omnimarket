# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeContextTaskKindRulesCompute - contract-driven; all behaviour is in contract.yaml."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_compute import NodeCompute

from omnimarket.models.context_task_kind_rules import (
    ModelContextTaskKindRulesRequest,
    ModelContextTaskKindRulesResult,
)

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeContextTaskKindRulesCompute(
    NodeCompute[ModelContextTaskKindRulesRequest, ModelContextTaskKindRulesResult]
):
    """Compute node: evaluates a profile's task-kind rules over the caller scope."""

    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)


__all__ = ["NodeContextTaskKindRulesCompute"]
