# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seams of node_memory_pilot_effect (OMN-19998).

The backend protocol lives here while the pilot is a pilot; it moves to
omnibase_spi if the pilot's bar passes and the node is adopted.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot import (
    ModelMemoryPilotContextRef,
    ModelMemoryPilotDocument,
)


class LabModelUnavailableError(Exception):
    """A lab model endpoint refused, timed out or answered badly."""


class BackendAnswer:
    """What a backend returns for a question."""

    __slots__ = ("answer", "context")

    def __init__(
        self, answer: str, context: tuple[ModelMemoryPilotContextRef, ...]
    ) -> None:
        self.answer = answer
        self.context = context


@runtime_checkable
class ProtocolMemoryBackend(Protocol):
    """Where documents go in and answers come out."""

    name: str

    async def ingest(
        self, dataset: str, documents: tuple[ModelMemoryPilotDocument, ...]
    ) -> int:
        """Store the documents; return how many were stored."""
        ...

    async def query(self, dataset: str, question: str) -> BackendAnswer:
        """Answer a question from a dataset already ingested."""
        ...


@runtime_checkable
class ProtocolChatAnswerer(Protocol):
    """Answer a question from a context with a lab chat model."""

    async def answer(self, system_prompt: str, question: str, context: str) -> str:
        """Return the model's answer text; raise LabModelUnavailableError when it is down."""
        ...


@runtime_checkable
class ProtocolEventPublisher(Protocol):
    """Publish one serialized event on a topic (the event bus client's shape)."""

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> None:
        """Deliver ``value`` on ``topic``."""
        ...


@runtime_checkable
class ProtocolFailureRecorder(Protocol):
    """Record a failure row where the floor can see it."""

    async def record_failure(
        self, correlation_id: str, error_code: str, detail: str
    ) -> None:
        """Append one failure row."""
        ...


@runtime_checkable
class ProtocolConnectionAudit(Protocol):
    """Record every connection a run makes to an address outside loopback and the allowed hosts."""

    def start(self, allowed_hosts: tuple[str, ...]) -> None:
        """Begin recording."""
        ...

    def stop(self) -> tuple[str, ...]:
        """Stop; return the sorted distinct ``host:port`` of each outside connection."""
        ...


@runtime_checkable
class ProtocolPilotResultSink(Protocol):
    """Where the run's measured result goes: the measurement node's result port."""

    def write(self, measured_at: datetime, metrics: dict[str, float]) -> None:
        """Write the result; raise OSError when it did not land."""
        ...
