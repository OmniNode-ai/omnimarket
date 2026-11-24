# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Event bus surface shared by the delegation dispatch ports."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol


class ProtocolDelegationEventBus(Protocol):
    """Event bus surface required by the runtime delegation dispatch port."""

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: object = None,
    ) -> None: ...

    async def subscribe(
        self,
        topic: str,
        node_identity: object | None = None,
        on_message: Callable[[object], Awaitable[None]] | None = None,
        **kwargs: object,
    ) -> Callable[[], Awaitable[None]]: ...


__all__ = ["ProtocolDelegationEventBus"]
