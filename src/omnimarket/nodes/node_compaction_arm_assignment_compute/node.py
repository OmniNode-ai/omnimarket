# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative compute entrypoint; all decisions live in the handler."""

from .handlers.handler_arm_assignment import HandlerCompactionArmAssignment


class NodeCompactionArmAssignmentCompute(HandlerCompactionArmAssignment):
    """Runtime-discovered arm assignment compute node."""
