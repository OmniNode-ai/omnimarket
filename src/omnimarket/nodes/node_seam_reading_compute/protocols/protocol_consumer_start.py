# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The consumer start port: the one call that runs a consumer on a message of its topic (OMN-23151)."""

from __future__ import annotations

from typing import Protocol


class ProtocolConsumerStart(Protocol):
    """Start ``consumer`` on ``payload`` that arrived on ``topic``."""

    def start(self, consumer: str, topic: str, payload: str) -> None: ...


__all__ = ["ProtocolConsumerStart"]
