# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure executor attribution stamping."""

from .handlers.handler_execution_attribution import HandlerExecutionAttribution


class NodeExecutionAttributionCompute(HandlerExecutionAttribution):
    """ONEX entry point."""


__all__ = ["HandlerExecutionAttribution", "NodeExecutionAttributionCompute"]
