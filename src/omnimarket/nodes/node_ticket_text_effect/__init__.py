# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative entry point for the dispatch-time ticket text read (OMN-17427)."""

from pathlib import Path

from omnimarket.nodes.node_ticket_text_effect.container.container_ticket_text_effect import (
    create_handler_ticket_text,
)

from .handlers.handler_ticket_text import (
    HandlerTicketText,
    TicketTextError,
    linear_url_ok,
)
from .models.model_ticket_text import ModelTicketTextRequest, ModelTicketTextResult

__all__ = [
    "HandlerTicketText",
    "ModelTicketTextRequest",
    "ModelTicketTextResult",
    "NodeTicketTextEffect",
    "TicketTextError",
    "linear_url_ok",
]


class NodeTicketTextEffect:
    contract_path = Path(__file__).with_name("contract.yaml")

    def __init__(self) -> None:
        self.handler = create_handler_ticket_text()

    def handle(self, request: ModelTicketTextRequest) -> ModelTicketTextResult:
        return self.handler.handle(request)
