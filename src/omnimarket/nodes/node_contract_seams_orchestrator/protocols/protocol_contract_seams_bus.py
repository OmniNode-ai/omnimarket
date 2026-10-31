# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The slice of the event bus protocol a contracts-and-seams run uses (BUS in the contract)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from omnibase_core.models.event_bus.model_event_headers import ModelEventHeaders
from omnibase_core.models.event_bus.model_event_message import ModelEventMessage


class ProtocolContractSeamsBus(Protocol):
    """Start, publish, subscribe and close: what the runner and the chain drive call."""

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: ModelEventHeaders | None = None,
    ) -> None: ...

    async def subscribe(
        self,
        topic: str,
        *,
        on_message: Callable[[ModelEventMessage], Awaitable[None]],
        group_id: str,
    ) -> Callable[[], Awaitable[None]]: ...


__all__ = ["ProtocolContractSeamsBus"]
