# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_contract_seams_reducer: the contracts-and-seams run lifecycle (OMN-21627)."""

from omnimarket.nodes.node_contract_seams_reducer.handlers.handler_contract_seams import (
    HandlerContractSeams,
)

__all__ = ["HandlerContractSeams", "NodeContractSeamsReducer"]


class NodeContractSeamsReducer(HandlerContractSeams):
    """ONEX entry-point wrapper for HandlerContractSeams."""
