# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_onex_installer_render_compute: the customer installer rendered from a release's pinned values (OMN-25070)."""

from omnimarket.nodes.node_onex_installer_render_compute.handlers.handler_onex_installer_render import (
    HandlerOnexInstallerRender,
)

__all__ = ["HandlerOnexInstallerRender", "NodeOnexInstallerRenderCompute"]


class NodeOnexInstallerRenderCompute(HandlerOnexInstallerRender):
    """ONEX entry-point wrapper for HandlerOnexInstallerRender."""
