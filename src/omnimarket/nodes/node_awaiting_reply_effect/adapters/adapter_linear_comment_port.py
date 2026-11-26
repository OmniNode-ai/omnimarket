# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Linear comment transport for the awaiting-reply effect, over the existing ticket gateway."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from omnimarket.models.model_ticket_io import (
    EnumTicketOperation,
    ModelCreateTicketRequest,
    ModelCreateTicketResult,
)


class _TicketGateway(Protocol):
    def handle(self, request: ModelCreateTicketRequest) -> ModelCreateTicketResult: ...


def _default_gateway(lane: str) -> _TicketGateway:
    from omnimarket.nodes.node_create_ticket import HandlerCreateTicket

    return HandlerCreateTicket(lane_id=lane, require_description_stamp=True)


class LinearCommentPort:
    """Posts one comment through the guarded ticket gateway; a refusal raises."""

    def __init__(
        self,
        lane: str,
        *,
        gateway_factory: Callable[[str], _TicketGateway] = _default_gateway,
    ) -> None:
        self._lane = lane
        self._factory = gateway_factory

    def comment(self, ticket_id: str, body: str) -> str:
        result = self._factory(self._lane).handle(
            ModelCreateTicketRequest(
                operation=EnumTicketOperation.COMMENT, ticket_id=ticket_id, body=body
            )
        )
        if result.status == "error" or not result.comment_id:
            codes = ",".join(result.finding_codes) or result.status
            raise RuntimeError(f"linear comment refused: {codes}")
        return result.comment_id
