# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocols of node_ci_checkout_effect."""

from omnimarket.nodes.node_ci_checkout_effect.protocols.protocol_ci_checkout import (
    ProtocolCheckoutGit,
    ProtocolCloneSync,
)

__all__ = ["ProtocolCheckoutGit", "ProtocolCloneSync"]
