# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_depends_on_overlay_check_effect: refuses a node whose declared overlay row is absent from the overlays repository (OMN-24537)."""

from omnimarket.nodes.node_depends_on_overlay_check_effect.handlers.handler_depends_on_overlay_check import (
    HandlerDependsOnOverlayCheck,
)

__all__ = ["HandlerDependsOnOverlayCheck"]
