# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the ledger MSG topic with one stable group per host; spool by lane.

The group id is ``{prefix}-{host}`` on every call. The reader never mints a
group per call and never subscribes ungrouped, which starts at the log end and
cannot supply the MSG rows appended before the reader started. Earliest applies
only where the broker holds no committed offset for the group.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

from ..models import (
    ModelAgentInboxSpoolOverlay,
    ModelAgentInboxSpoolRequest,
    ModelAgentInboxSpoolResult,
)
from ..protocols import ProtocolAgentInboxSpoolBus
from .parse_ledger_msg import (
    MalformedLedgerMsgError,
    addressed_lanes,
    decode_msg_record,
)
from .write_spool import UNREAD_MARKER, spool_entry


class HandlerAgentInboxSpool:
    """Subscribe to the contract's MSG topic, then write spool entries and markers.

    The caller owns the bus lifecycle and selects its lane from the overlay.
    """

    def __init__(
        self,
        *,
        bus: ProtocolAgentInboxSpoolBus | None = None,
        clock: Callable[[], datetime] | None = None,
        topics: tuple[str, ...] | None = None,
    ) -> None:
        from ..node import load_topics

        self._topics = load_topics() if topics is None else topics
        if len(self._topics) != 1:
            raise ValueError("inbox spool reads exactly one MSG topic")
        # The runtime builds a handler with no arguments; the bus is then bound by
        # the caller before ``handle`` runs, and a call with none refuses.
        self._bus = bus
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)

    def _group_id(
        self, request: ModelAgentInboxSpoolRequest, overlay: ModelAgentInboxSpoolOverlay
    ) -> str:
        """The single group identity derivation point."""
        return f"{overlay.consumer_group_prefix}-{request.host}"

    async def handle(
        self, request: ModelAgentInboxSpoolRequest
    ) -> ModelAgentInboxSpoolResult:
        if self._bus is None:
            raise RuntimeError("inbox spool: no bus injected; construct with bus=")
        bus = self._bus
        overlay = load_structured_overlay(
            "agent_inbox_spool", ModelAgentInboxSpoolOverlay, explicit=request.overlay
        )
        group_id = self._group_id(request, overlay)
        queue: asyncio.Queue[bytes] = asyncio.Queue()
        events_read = 0
        error = ""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + overlay.read_window_seconds
        wall_deadline = self._clock() + timedelta(seconds=overlay.read_window_seconds)
        quiet_gap = overlay.read_window_seconds / 8

        def remaining() -> float:
            return max(
                0.0,
                min(
                    deadline - loop.time(),
                    (wall_deadline - self._clock()).total_seconds(),
                ),
            )

        async def on_message(message: ModelEventMessage) -> None:
            nonlocal events_read
            events_read += 1
            queue.put_nowait(bytes(message.value))

        records: list[bytes] = []
        try:
            async with AsyncExitStack() as stack:
                stop = await asyncio.wait_for(
                    bus.subscribe(
                        self._topics[0],
                        on_message=on_message,
                        group_id=group_id,
                        auto_offset_reset="earliest",
                    ),
                    timeout=remaining(),
                )
                stack.push_async_callback(stop)
                while remaining() > 0:
                    try:
                        records.append(
                            await asyncio.wait_for(
                                queue.get(), timeout=min(quiet_gap, remaining())
                            )
                        )
                    except TimeoutError:
                        break
        except Exception as exc:
            error = (
                f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
            )
        # Teardown may deliver final records; drain only after the unsubscribe.
        while not queue.empty():
            records.append(queue.get_nowait())
        spooled: list[str] = []
        duplicates = unaddressed = malformed = 0
        unread: dict[str, None] = {}
        spool_root = Path(overlay.spool_root)
        for value in records:
            try:
                entry = decode_msg_record(value, group_id)
            except MalformedLedgerMsgError:
                malformed += 1
                continue
            lanes = addressed_lanes(entry, request.lanes)
            if not lanes:
                unaddressed += 1
                continue
            for lane in lanes:
                if spool_entry(spool_root, lane, entry):
                    spooled.append(f"{lane}/{entry.row_id}")
                else:
                    duplicates += 1
        for lane in request.lanes:
            if (spool_root / lane / UNREAD_MARKER).exists():
                unread[lane] = None
        return ModelAgentInboxSpoolResult(
            consumer_group=group_id,
            groups_used=(group_id,),
            events_read=events_read,
            spooled=tuple(spooled),
            duplicates=duplicates,
            unaddressed=unaddressed,
            malformed=malformed,
            unread_lanes=tuple(unread),
            read_ok=not error,
            error=error,
        )
