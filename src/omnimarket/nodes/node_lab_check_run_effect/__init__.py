# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lab check-run effect: the posting side of J8 (OMN-22950)."""

from omnimarket.nodes.node_lab_check_run_effect.handlers.handler_lab_check_run_effect import (
    HandlerLabCheckRunEffect,
)


class NodeLabCheckRunEffect(HandlerLabCheckRunEffect):
    """ONEX entry point."""


__all__ = ["HandlerLabCheckRunEffect", "NodeLabCheckRunEffect"]
