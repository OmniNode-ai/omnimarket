# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One concrete event model per routing entry, so the runtime's typed arm resolves each entry.

Each entry annotates its handle() with one concrete pydantic model and delegates to the unchanged
HandlerTicketHandEditAlarm by composition. A sweep waits on Linear and on the database, so each
entry is a coroutine that runs the delegate on a worker thread and never holds the runtime's event
loop (OMN-24373). Every handle() returns None: the node publishes its own events through its
event_publisher, and a returned model would be published by the runtime to the first entry of
publish_topics.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.handlers.handler_ticket_hand_edit_alarm import (
    HandlerTicketHandEditAlarm,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models import (
    ModelTicketContractWritten,
    ModelTicketHandEditInput,
    ModelTicketHandEditSweepTrigger,
)
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.protocols.protocol_linear_issue_source import (
    ProtocolLinearIssueSource,
)
from omnimarket.protocols.protocol_projection_database_sync import DatabaseAdapter

logger = logging.getLogger(__name__)


class _HandlerTicketHandEditEntry:
    """Base for the routing entries: one concrete model, one delegate."""

    def __init__(
        self,
        *,
        event_publisher: Callable[[str, bytes], None] | None = None,
        database: DatabaseAdapter | None = None,
        source: ProtocolLinearIssueSource | None = None,
        clock: Callable[[], float] | None = None,
        contract_path: Path | None = None,
    ) -> None:
        self._core = HandlerTicketHandEditAlarm(
            event_publisher=event_publisher,
            database=database,
            source=source,
            clock=clock,
            contract_path=contract_path,
        )

    async def _run(self, request: ModelTicketHandEditInput) -> None:
        result = await asyncio.to_thread(self._core.handle, request)
        logger.info(
            "ticket hand edit alarm handled %s: %s",
            type(request).__name__,
            result.outcome.value,
        )
        if result.error is not None:
            logger.warning("ticket hand edit alarm error: %s", result.error)


class HandlerTicketHandEditWritten(_HandlerTicketHandEditEntry):
    """A written event: the newest write record of a ticket, and the close of a finding it matches."""

    async def handle(self, request: ModelTicketContractWritten) -> None:
        await self._run(request)


class HandlerTicketHandEditSweep(_HandlerTicketHandEditEntry):
    """The runtime heartbeat that reads changed issues, at most once per overlay interval."""

    async def handle(self, request: ModelTicketHandEditSweepTrigger) -> None:
        await self._run(request)


__all__ = ["HandlerTicketHandEditSweep", "HandlerTicketHandEditWritten"]
