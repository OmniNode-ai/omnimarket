# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Claim index compute node: who holds a ticket, according to the rows lanes wrote about themselves."""

from omnimarket.nodes.node_claim_index_compute.handlers.handler_claim_index import (
    HandlerClaimIndex,
)


class NodeClaimIndexCompute(HandlerClaimIndex):
    """ONEX entrypoint for the claim index."""


__all__ = ["HandlerClaimIndex", "NodeClaimIndexCompute"]
