# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_lab_sli_collect_effect: lab SLI readings from existing sources (OMN-17427)."""

from omnimarket.nodes.node_lab_sli_collect_effect.handlers.handler_lab_sli_collect import (
    HandlerLabSliCollect,
)

__all__ = ["HandlerLabSliCollect", "NodeLabSliCollectEffect"]


class NodeLabSliCollectEffect(HandlerLabSliCollect):
    """ONEX entry-point wrapper for HandlerLabSliCollect."""
