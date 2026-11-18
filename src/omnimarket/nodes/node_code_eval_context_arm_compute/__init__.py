# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Context-arm select and judge for code-eval context scoring (OMN-23629)."""

from omnimarket.nodes.node_code_eval_context_arm_compute.handlers.handler_code_eval_context_arm import (
    HandlerCodeEvalContextArm,
)


class NodeCodeEvalContextArmCompute(HandlerCodeEvalContextArm):
    """ONEX entrypoint for the code-eval context arm decision."""


__all__ = [
    "HandlerCodeEvalContextArm",
    "NodeCodeEvalContextArmCompute",
]
