# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative node entrypoint; routing is declared by contract."""

from omnimarket.nodes.node_ticket_contract_merge_compute.handlers.handler_ticket_contract_merge import (
    HandlerTicketContractMerge,
)


class NodeTicketContractMergeCompute(HandlerTicketContractMerge):
    """Merge three caller-supplied contract texts without reading live state."""
