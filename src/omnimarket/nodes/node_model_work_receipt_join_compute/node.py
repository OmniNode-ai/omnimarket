# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-declared pure attempt receipt join."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_compute import NodeCompute

from .models.model_work_attempt_receipt import (
    ModelReceiptJoinRequest,
    ModelReceiptJoinResult,
)

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeModelWorkReceiptJoinCompute(
    NodeCompute[ModelReceiptJoinRequest, ModelReceiptJoinResult]
):
    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)
