# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""PR title check compute node (OMN-20885)."""

from omnimarket.nodes.node_pr_title_check_compute.handlers.handler_pr_title_check import (
    HandlerPrTitleCheck,
)


class NodePrTitleCheckCompute(HandlerPrTitleCheck):
    """ONEX entrypoint for definition-B PR title decisions."""


__all__ = ["HandlerPrTitleCheck", "NodePrTitleCheckCompute"]
