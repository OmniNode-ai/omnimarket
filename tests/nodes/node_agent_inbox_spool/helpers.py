# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Synthetic MSG records and a recording adapter for the in-memory bus."""

import json
from collections.abc import Awaitable, Callable
from pathlib import Path

from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.nodes.node_agent_inbox_spool.models import ModelAgentInboxSpoolOverlay

STAMP = "2026-11-01T22:00:00Z"


def overlay(spool_root: Path, **updates: object) -> ModelAgentInboxSpoolOverlay:
    return ModelAgentInboxSpoolOverlay.model_validate(
        {
            "consumer_group_prefix": "test-agent-inbox",
            "spool_root": str(spool_root),
            "read_window_seconds": 0.016,
            "bus_lane": "test",
            **updates,
        }
    )


def msg_row(to: str, nonce: str, *, sender: str = "orch", stamp: str = STAMP) -> str:
    return (
        f"{stamp} | MSG | from={sender} | principal=test | id={stamp}-{sender}-{nonce}"
        f" | to={to} | ticket=OMN-1 | via=bus:test | text with a | pipe and nonce {nonce}"
    )


def record(raw_row: str, *, wrapped: bool = True, row_id: str | None = None) -> bytes:
    payload = {
        "ledger_id": "test-ledger",
        "row_id": row_id if row_id is not None else work_ledger_row_id(raw_row),
        "raw_row": raw_row,
        "source": "test",
        "provenance_kind": "typed",
        "row_schema": "work-ledger-event/2",
    }
    return json.dumps({"payload": payload} if wrapped else payload).encode()


class RecordingBus:
    """Record actual calls; adapt public history to earliest semantics.

    The installed in-memory bus has neither an auto_offset_reset argument nor
    subscription history delivery. This adapter replays its public history on
    each earliest subscription, so a replay re-reads every record and the
    spool's own duplicate check is what keeps the markers single. It does not
    simulate Kafka commits; the broker test covers group persistence.
    """

    def __init__(self, bus: EventBusInmemory) -> None:
        self.bus = bus
        self.subscriptions: list[tuple[str, str, str]] = []
        self.ungrouped: list[str] = []

    async def subscribe(
        self,
        topic: str,
        on_message: Callable[[ModelEventMessage], Awaitable[None]],
        *,
        group_id: str,
        auto_offset_reset: str,
    ) -> Callable[[], Awaitable[None]]:
        self.subscriptions.append((topic, group_id, auto_offset_reset))
        stop = await self.bus.subscribe(topic, on_message=on_message, group_id=group_id)
        try:
            if auto_offset_reset == "earliest":
                for stored in await self.bus.get_event_history(topic=topic):
                    await on_message(
                        ModelEventMessage.model_validate(stored.model_dump())
                    )
        except BaseException:
            await stop()
            raise
        return stop

    async def subscribe_ungrouped(
        self,
        topic: str,
        on_message: Callable[[ModelEventMessage], Awaitable[None]],
    ) -> Callable[[], Awaitable[None]]:
        self.ungrouped.append(topic)
        return await self.bus.subscribe_ungrouped(topic, on_message)
