# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds node_claim_resolver handlers."""

from omnimarket.nodes.node_claim_resolver.handlers.handler_claim_resolver import (
    HandlerClaimResolver,
)


def build_handler_claim_resolver() -> HandlerClaimResolver:
    """Build the handler with its constructor defaults."""
    return HandlerClaimResolver()
