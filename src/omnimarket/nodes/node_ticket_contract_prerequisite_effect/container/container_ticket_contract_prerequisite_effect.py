# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler factories for node_ticket_contract_prerequisite_effect."""

from __future__ import annotations

from omnimarket.nodes.node_ticket_contract_prerequisite_effect.handlers.handler_ticket_contract_prerequisite import (
    HandlerTicketContractPrerequisite,
)

__all__ = ["create_handler_ticket_contract_prerequisite"]


def create_handler_ticket_contract_prerequisite() -> HandlerTicketContractPrerequisite:
    """Build the handler with its default dependencies."""
    return HandlerTicketContractPrerequisite()
