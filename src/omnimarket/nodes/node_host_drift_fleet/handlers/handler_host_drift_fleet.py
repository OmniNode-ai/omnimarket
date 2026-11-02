# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bounded fleet history reader using one stable consumer group per host.

``subscribe_ungrouped`` reads from latest only, so it cannot supply the earliest
history needed by this fold. Grouped subscriptions request earliest and reuse
the same host identity on every call, including replay. Earliest is Kafka's
fallback for missing offsets, not a rewind of committed offsets; replay clears
the projection and folds whatever history the bus supplies. The injected store
preserves the projection between ordinary incremental reads.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta

from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
from pydantic import BaseModel, ConfigDict, ValidationError

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

from ..models import (
    ModelHostDriftFleetOverlay,
    ModelHostDriftFleetRequest,
    ModelHostDriftFleetResult,
    ModelHostDriftFleetView,
    ModelHostDriftHeartbeatEvent,
    ModelHostDriftResolvedEvent,
)
from ..protocols import ProtocolHostDriftFleetBus, ProtocolHostDriftFleetStateStore
from .fold_host_drift_view import FleetEvent, fold_view


class _Envelope[PayloadT: BaseModel](BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")
    payload: PayloadT


class InMemoryFleetStateStore:
    """Projection store that lives as long as the handler; a caller injects a durable one."""

    def __init__(self) -> None:
        self._view: ModelHostDriftFleetView | None = None

    def load(self) -> ModelHostDriftFleetView | None:
        return self._view

    def save(self, view: ModelHostDriftFleetView) -> None:
        self._view = view


class HandlerHostDriftFleet:
    """Read contract topics, fold typed payloads and persist the projection.

    Topic overrides must be ordered heartbeat, resolved, as in the contract.
    The caller owns the bus lifecycle and selects its lane from the overlay.
    The clock bounds wall time alongside the event loop's monotonic deadline;
    request.now remains the authoritative timestamp for the projection.
    """

    def __init__(
        self,
        *,
        bus: ProtocolHostDriftFleetBus | None = None,
        clock: Callable[[], datetime] | None = None,
        state_store: ProtocolHostDriftFleetStateStore | None = None,
        topics: tuple[str, ...] | None = None,
    ) -> None:
        from ..node import load_topics

        self._topics = load_topics() if topics is None else topics
        if len(self._topics) != 2 or len(set(self._topics)) != 2:
            raise ValueError(
                "fleet reader requires distinct heartbeat and resolved topics"
            )
        # The runtime builds a handler with no arguments; the bus is then bound by
        # the caller before ``handle`` runs, and a call with none refuses.
        self._bus = bus
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._state_store: ProtocolHostDriftFleetStateStore = (
            state_store if state_store is not None else InMemoryFleetStateStore()
        )

    def _group_id(self, request: ModelHostDriftFleetRequest) -> str:
        """The single group identity derivation point; replay never changes it."""
        overlay = load_structured_overlay(
            "host_drift_fleet", ModelHostDriftFleetOverlay, explicit=request.overlay
        )
        return f"{overlay.consumer_group_prefix}.{request.host}"

    async def handle(
        self, request: ModelHostDriftFleetRequest
    ) -> ModelHostDriftFleetResult:
        if self._bus is None:
            raise RuntimeError("host drift fleet: no bus injected; construct with bus=")
        bus = self._bus
        overlay = load_structured_overlay(
            "host_drift_fleet", ModelHostDriftFleetOverlay, explicit=request.overlay
        )
        request = request.model_copy(update={"overlay": overlay})
        group_id = self._group_id(request)
        previous = (
            None
            if request.replay
            else request.previous
            if request.previous is not None
            else self._state_store.load()
        )
        queue: asyncio.Queue[FleetEvent | None] = asyncio.Queue()
        events: list[FleetEvent] = []
        malformed = 0
        events_read = 0
        groups: list[str] = []
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

        def callback(index: int) -> Callable[[ModelEventMessage], Awaitable[None]]:
            async def on_message(message: ModelEventMessage) -> None:
                nonlocal malformed, events_read
                events_read += 1
                try:
                    event: FleetEvent = (
                        _Envelope[ModelHostDriftHeartbeatEvent]
                        .model_validate_json(message.value)
                        .payload
                        if index == 0
                        else _Envelope[ModelHostDriftResolvedEvent]
                        .model_validate_json(message.value)
                        .payload
                    )
                except ValidationError:
                    malformed += 1
                    queue.put_nowait(None)
                else:
                    queue.put_nowait(event)

            return on_message

        try:
            async with AsyncExitStack() as stack:
                for index, topic in enumerate(self._topics):
                    groups.append(group_id)
                    stop = await asyncio.wait_for(
                        bus.subscribe(
                            topic,
                            on_message=callback(index),
                            group_id=group_id,
                            auto_offset_reset="earliest",
                        ),
                        timeout=remaining(),
                    )
                    stack.push_async_callback(stop)
                while remaining() > 0:
                    try:
                        event = await asyncio.wait_for(
                            queue.get(), timeout=min(quiet_gap, remaining())
                        )
                    except TimeoutError:
                        break
                    if event is not None:
                        events.append(event)
        except Exception as exc:
            error = (
                f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
            )
        # Teardown may deliver final records; drain only after all unsubscribes.
        while not queue.empty():
            event = queue.get_nowait()
            if event is not None:
                events.append(event)
        view = fold_view(
            previous=previous,
            events=events,
            overlay=overlay,
            now=request.now,
            consumer_group=group_id,
            malformed=malformed,
        )
        self._state_store.save(view)
        return ModelHostDriftFleetResult(
            view=view,
            consumer_group=group_id,
            events_read=events_read,
            read_ok=not error,
            error=error,
            groups_used=tuple(dict.fromkeys(groups)),
        )
