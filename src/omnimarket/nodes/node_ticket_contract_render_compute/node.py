# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative node entrypoint; routing is declared by contract."""

from omnimarket.nodes.node_ticket_contract_render_compute.handlers.handler_ticket_contract_render import (
    HandlerTicketContractRender,
)


class NodeTicketContractRenderCompute(HandlerTicketContractRender):
    """Render or parse one ticket body from the request alone; reads no live state."""
