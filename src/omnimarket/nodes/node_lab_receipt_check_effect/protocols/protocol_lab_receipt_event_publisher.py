# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Event bus seam of node_lab_receipt_check_effect (OMN-21826)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ProtocolLabReceiptEventPublisher(Protocol):
    """Publish one serialized event on a topic (the event bus client's shape)."""

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> None:
        """Deliver ``value`` on ``topic``."""
        ...
