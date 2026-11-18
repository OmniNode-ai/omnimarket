# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The publish port of the host disk reading and its in-memory default (OMN-23865)."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel


class ProtocolHostDiskPublisher(Protocol):
    """Sends one event on one topic and returns once the bus accepted it; raises when the bus refused it."""

    def publish(self, topic: str, event: BaseModel) -> None: ...


class InMemoryHostDiskPublisher:
    """Keeps the events it is given with their topic; ``refuse`` makes every publish raise, as a refused broker does."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.published: list[tuple[str, BaseModel]] = []
        self._refuse = refuse

    def publish(self, topic: str, event: BaseModel) -> None:
        if self._refuse:
            raise ConnectionError("publish refused by the in-memory bus")
        self.published.append((topic, event))


__all__: list[str] = ["InMemoryHostDiskPublisher", "ProtocolHostDiskPublisher"]
