# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative effect node of the cause-ticket-unresolved detector and actor (OMN-22143)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import (
        ModelCauseTicketUnresolvedRequest,
        ModelCauseTicketUnresolvedResult,
    )

CONTRACT = Path(__file__).with_name("contract.yaml")


class NodeCauseTicketUnresolvedEffect:
    contract_path = CONTRACT

    def handle(
        self, request: ModelCauseTicketUnresolvedRequest
    ) -> ModelCauseTicketUnresolvedResult:
        from .container.container_cause_ticket_unresolved_effect import (
            build_handler_cause_ticket_unresolved,
        )

        return build_handler_cause_ticket_unresolved().handle(request)
