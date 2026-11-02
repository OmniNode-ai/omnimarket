# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Synthetic fleet fixtures and a recording adapter for the in-memory bus."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.nodes.node_host_drift_fleet.models import (
    ModelHostDriftFleetOverlay,
    ModelHostDriftFleetView,
    ModelHostDriftHeartbeatEvent,
    ModelHostDriftResolvedEvent,
)

NOW = datetime(2026, 10, 31, 12, tzinfo=UTC)


def overlay(**updates: object) -> ModelHostDriftFleetOverlay:
    return ModelHostDriftFleetOverlay.model_validate(
        {
            "hosts": ("host-a", "host-b"),
            "read_only_hosts": (),
            "heartbeat_stale_seconds": 300,
            "consumer_group_prefix": "test.host_drift_fleet",
            "read_window_seconds": 0.016,
            "bus_lane": "test",
            **updates,
        }
    )


def heartbeat(host: str = "host-a", **updates: object) -> ModelHostDriftHeartbeatEvent:
    return ModelHostDriftHeartbeatEvent.model_validate(
        {
            "host": host,
            "check_id": "check-new",
            "checked_at": NOW,
            "open": (),
            **updates,
        }
    )


def resolved(host: str = "host-a", **updates: object) -> ModelHostDriftResolvedEvent:
    return ModelHostDriftResolvedEvent.model_validate(
        {
            "host": host,
            "role": "test",
            "check_id": "check-old",
            "key": "unit:service",
            "kind": "unit",
            "declared": "running",
            "last_observed": "stopped",
            "first_seen_at": NOW,
            "resolved_at": NOW,
            "duration_seconds": 0,
            "resolution": "OBSERVED_CONVERGED",
            **updates,
        }
    )


class MemoryStore:
    def __init__(self, view: ModelHostDriftFleetView | None = None) -> None:
        self.view = view
        self.loads = 0
        self.saves = 0

    def load(self) -> ModelHostDriftFleetView | None:
        self.loads += 1
        return self.view

    def save(self, view: ModelHostDriftFleetView) -> None:
        self.saves += 1
        self.view = view


class RecordingBus:
    """Record actual calls; adapt public history to earliest semantics.

    The installed in-memory bus has neither an auto_offset_reset argument nor
    subscription history delivery. This adapter replays its public history on
    each earliest subscription, deliberately stressing fold deduplication. It
    does not simulate Kafka commits; the broker test covers group persistence.
    """

    def __init__(self, bus: EventBusInmemory) -> None:
        self.bus = bus
        self.subscriptions: list[tuple[str, str, str]] = []
        self.ungrouped: list[str] = []
        self.stopped: list[tuple[str, str]] = []

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
                for record in await self.bus.get_event_history(topic=topic):
                    await on_message(
                        ModelEventMessage.model_validate(record.model_dump())
                    )
        except BaseException:
            await stop()
            raise

        async def unsubscribe() -> None:
            self.stopped.append((topic, group_id))
            await stop()

        return unsubscribe

    async def subscribe_ungrouped(
        self,
        topic: str,
        on_message: Callable[[ModelEventMessage], Awaitable[None]],
    ) -> Callable[[], Awaitable[None]]:
        self.ungrouped.append(topic)
        return await self.bus.subscribe_ungrouped(topic, on_message)
