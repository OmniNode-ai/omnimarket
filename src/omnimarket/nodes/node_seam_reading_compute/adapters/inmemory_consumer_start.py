# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory consumer start, the default backend: it records each start (OMN-23151)."""

from __future__ import annotations


class InMemoryConsumerStart:
    """Every start as (consumer, topic, payload), in the order they happened."""

    def __init__(self) -> None:
        self.starts: list[tuple[str, str, str]] = []

    def start(self, consumer: str, topic: str, payload: str) -> None:
        self.starts.append((consumer, topic, payload))
