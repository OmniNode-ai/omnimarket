# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One concrete event model per routing entry, so the runtime's typed arm resolves each entry.

Each entry annotates its handle() with one concrete pydantic model and delegates to
HandlerTicketHandEditAlarm by composition. Every handle() returns None: the node publishes
its own events through its event_publisher, and a returned model would be published by the
runtime to the first entry of publish_topics.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from omnimarket.models.model_ticket_contract_written import ModelTicketContractWritten
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.handlers.handler_ticket_hand_edit_alarm import (
    HandlerTicketHandEditAlarm,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models import (
    ModelTicketHandEditSweepTrigger,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.protocols import (
    ProtocolLinearIssueReader,
)
from omnimarket.protocols.protocol_projection_database_sync import DatabaseAdapter

logger = logging.getLogger(__name__)


class _HandlerTicketHandEditEntry:
    """Base for the two routing entries: one concrete model, one delegate."""

    def __init__(
        self,
        *,
        event_publisher: Callable[[str, bytes], None] | None = None,
        database: DatabaseAdapter | None = None,
        reader: ProtocolLinearIssueReader | None = None,
        clock: Callable[[], float] | None = None,
        contract_path: Path | None = None,
    ) -> None:
        self._core = HandlerTicketHandEditAlarm(
            event_publisher=event_publisher,
            database=database,
            reader=reader,
            clock=clock,
            contract_path=contract_path,
        )

    def _run(
        self, request: ModelTicketHandEditSweepTrigger | ModelTicketContractWritten
    ) -> None:
        result = self._core.handle(request)
        logger.info(
            "ticket hand-edit alarm handled %s: %s",
            type(request).__name__,
            result.outcome.value,
        )
        if result.error is not None:
            logger.warning("ticket hand-edit alarm error: %s", result.error)


class HandlerTicketHandEditSweep(_HandlerTicketHandEditEntry):
    """The runtime heartbeat: a throttled read of the issues Linear changed."""

    def handle(self, request: ModelTicketHandEditSweepTrigger) -> None:
        self._run(request)


class HandlerTicketHandEditWritten(_HandlerTicketHandEditEntry):
    """The ticket node's write record: kept per ticket, and it closes matching findings."""

    def handle(self, request: ModelTicketContractWritten) -> None:
        self._run(request)


__all__ = ["HandlerTicketHandEditSweep", "HandlerTicketHandEditWritten"]
