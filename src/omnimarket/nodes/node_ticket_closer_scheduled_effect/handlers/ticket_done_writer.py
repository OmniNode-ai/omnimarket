# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ticket write node bound as the ticket closer's writer (OMN-24138).

``BusTicketDoneWriter`` moves one ticket to the done state through the ticket write node: first a comment
that carries the evidence, then the transition, each as the same typed request ``onex ticket`` publishes. The
comment goes first, so a write that stops half way leaves the evidence and no Done. The adapter opens no bus:
the runtime binds ``call`` to the bus call ``handlers/ticket/ticket_cli.py`` makes with ``ScmBusCaller``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Protocol
from uuid import uuid4

from omnimarket.events.ticket_write_io import (
    ModelTicketWriteRequest,
)
from omnimarket.models.scm import ModelScmTerminal
from omnimarket.models.ticket_closer import ModelCloserWriteReceipt


class ProtocolTicketWriteCall(Protocol):
    """One request published to the ticket write node and its terminal event awaited."""

    def __call__(self, request: ModelTicketWriteRequest) -> ModelScmTerminal: ...


def _refusal_reason(terminal: ModelScmTerminal, default: str) -> str:
    reason = terminal.payload.get("reason")
    text = reason if isinstance(reason, str) and reason else default
    codes = terminal.payload.get("finding_codes")
    if isinstance(codes, list) and codes:
        text = f"{text} [{', '.join(str(code) for code in codes)}]"
    return text


class BusTicketDoneWriter:
    """The real backend writer: comment the evidence, then transition, through the ticket write node."""

    def __init__(self, call: ProtocolTicketWriteCall) -> None:
        self._call = call

    def _request(
        self, operation: str, ticket_id: str, lane: str, fields: dict[str, str]
    ) -> ModelTicketWriteRequest:
        identity = {"operation": operation, "ticket_id": ticket_id, **fields}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        return ModelTicketWriteRequest.model_validate(
            {
                **identity,
                "requested_by_lane": lane,
                "idempotency_key": key,
                "correlation_id": uuid4(),
            }
        )

    def _send(
        self, operation: str, ticket_id: str, lane: str, fields: dict[str, str]
    ) -> ModelCloserWriteReceipt:
        try:
            terminal = self._call(self._request(operation, ticket_id, lane, fields))
        except Exception as exc:
            return ModelCloserWriteReceipt(
                ok=False, reason=f"{operation} write raised {type(exc).__name__}"
            )
        if terminal.failed:
            return ModelCloserWriteReceipt(
                ok=False,
                reason=_refusal_reason(terminal, f"{operation} refused"),
            )
        return ModelCloserWriteReceipt(ok=True)

    def mark_done(
        self, ticket_id: str, *, state: str, lane: str, evidence: str
    ) -> ModelCloserWriteReceipt:
        commented = self._send(
            "comment", ticket_id, lane, {"body": f"actor: {lane} (node)\n{evidence}"}
        )
        if not commented.ok:
            return commented
        return self._send("transition", ticket_id, lane, {"state": state})
