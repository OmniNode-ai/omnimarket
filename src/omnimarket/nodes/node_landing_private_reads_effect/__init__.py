# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_landing_private_reads_effect: day one of rebuild piece 9 lists the staged controller's private reads."""

from omnimarket.nodes.node_landing_private_reads_effect.handlers.handler_landing_private_reads import (
    HandlerLandingPrivateReads,
)


class NodeLandingPrivateReadsEffect(HandlerLandingPrivateReads):
    """ONEX entry-point wrapper for HandlerLandingPrivateReads."""


__all__ = ["HandlerLandingPrivateReads", "NodeLandingPrivateReadsEffect"]
