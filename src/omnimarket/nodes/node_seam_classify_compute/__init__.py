# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_seam_classify_compute: operation-routed seam classification (OMN-21459)."""

from omnimarket.nodes.node_seam_classify_compute.handlers.classify_backlog_engine_seams import (
    BACKLOG_ENGINE_OPERATIONS,
)
from omnimarket.nodes.node_seam_classify_compute.handlers.handler_seam_classify import (
    HandlerSeamClassify,
    SeamClassifier,
)

__all__ = ["HandlerSeamClassify", "NodeSeamClassifyCompute"]


class NodeSeamClassifyCompute(HandlerSeamClassify):
    """ONEX entry-point wrapper for HandlerSeamClassify.

    Registers the readers this node ships: SM-00 (OMN-21475) and the engine reads of SM-01 and SM-02.
    """

    def __init__(self, operations: dict[str, SeamClassifier] | None = None) -> None:
        super().__init__({**BACKLOG_ENGINE_OPERATIONS, **(operations or {})})
