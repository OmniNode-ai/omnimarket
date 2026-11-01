# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Injected bus and projection storage boundaries."""

from collections.abc import Awaitable, Callable
from typing import Protocol

from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from ..models import ModelHostDriftFleetView


class ProtocolHostDriftFleetBus(Protocol):
    async def subscribe(
        self,
        topic: str,
        *,
        on_message: Callable[[ModelEventMessage], Awaitable[None]],
        group_id: str,
        auto_offset_reset: str,
    ) -> Callable[[], Awaitable[None]]: ...


class ProtocolHostDriftFleetStateStore(Protocol):
    def load(self) -> ModelHostDriftFleetView | None: ...

    def save(self, view: ModelHostDriftFleetView) -> None: ...
