# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_seam_parallel_executor — deterministic wave executor for parallel task execution."""

from omnimarket.nodes.node_seam_parallel_executor.handlers.handler_seam_parallel_executor import (
    HandlerSeamParallelExecutor,
)
from omnimarket.nodes.node_seam_parallel_executor.models.model_seam_task import (
    ModelSeamParallelInput,
    ModelSeamParallelResult,
    ModelSeamTask,
)


class NodeSeamParallelExecutor(HandlerSeamParallelExecutor):
    """ONEX entry-point wrapper for HandlerSeamParallelExecutor."""


__all__ = [
    "HandlerSeamParallelExecutor",
    "ModelSeamParallelInput",
    "ModelSeamParallelResult",
    "ModelSeamTask",
    "NodeSeamParallelExecutor",
]
