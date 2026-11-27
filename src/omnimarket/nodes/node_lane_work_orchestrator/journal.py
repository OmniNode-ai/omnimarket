# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The orchestrator's publish and read-back port, and an in-memory journal (OMN-26721).

A role is a topic named by the contract (event_topics.py); the journal never sees a topic.
The recovery procedure of plan section 3.6 reads each role back before it resumes.
"""

from __future__ import annotations

from typing import Any, Protocol


class ProtocolLaneWorkJournal(Protocol):
    """Publish one message to a role, and read every message of a role back in order."""

    def publish(self, role: str, output_id: str, payload: dict[str, Any]) -> None: ...

    def read(self, role: str) -> list[tuple[str, dict[str, Any]]]: ...


class InMemoryLaneWorkJournal:
    """Append-only lists per role; what the broker holds once a publish returns."""

    def __init__(self) -> None:
        self._messages: dict[str, list[tuple[str, dict[str, Any]]]] = {}

    def publish(self, role: str, output_id: str, payload: dict[str, Any]) -> None:
        self._messages.setdefault(role, []).append((output_id, payload))

    def read(self, role: str) -> list[tuple[str, dict[str, Any]]]:
        return list(self._messages.get(role, ()))


__all__: list[str] = ["InMemoryLaneWorkJournal", "ProtocolLaneWorkJournal"]
