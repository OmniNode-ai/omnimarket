# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ticket write node bound as the ticket closer's reader (OMN-24301).

``BusTicketReader`` reads one ticket's own state and description through the ticket write
node's ``read`` command, over the same ``call`` the done writer takes: the request is the
one ``onex ticket read`` publishes. A reading that cannot be taken — a raise, a failed
terminal, a result without a state — is ``None``, never an empty answer: the candidate
source drops the ticket, it does not read it as a ticket with no state.
"""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from omnimarket.models.ticket_write_io import (
    ModelTicketWriteRequest,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.ticket_done_writer import (
    ProtocolTicketWriteCall,
)


class BusTicketReader:
    """The real backend ticket reader: the write node's ``read`` command, terminal awaited."""

    def __init__(self, call: ProtocolTicketWriteCall, lane: str) -> None:
        self._call = call
        self._lane = lane

    def read(self, ticket_id: str) -> tuple[str, str] | None:
        identity = {"operation": "read", "ticket_id": ticket_id}
        # The key carries a fresh uuid: a read is not a retry of the last one, so
        # the write node answers it again instead of replaying an earlier answer.
        key = hashlib.sha256(
            (json.dumps(identity, sort_keys=True) + str(uuid4())).encode()
        ).hexdigest()
        try:
            request = ModelTicketWriteRequest.model_validate(
                {
                    **identity,
                    "requested_by_lane": self._lane,
                    "idempotency_key": key,
                    "correlation_id": uuid4(),
                }
            )
            terminal = self._call(request)
        except Exception:
            return None
        result = terminal.payload.get("result")
        if terminal.failed or not isinstance(result, dict):
            return None
        status = result.get("ticket_status")
        if not isinstance(status, str) or not status:
            return None
        description = result.get("description_body")
        return status, (description if isinstance(description, str) else "")
