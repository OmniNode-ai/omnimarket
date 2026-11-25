# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_host_thermal_guard_effect: the sensors, the declared hold file, the episode file, the lane cutter and the ledger appender."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from omnimarket.nodes.node_reading_writer_effect.protocols import ModelAppendOutcome

from ..models import ModelHoldRecord, ModelThermalReading


@runtime_checkable
class ProtocolThermalSensors(Protocol):
    def read(self) -> ModelThermalReading: ...


@runtime_checkable
class ProtocolHoldStore(Protocol):
    """The declared hold: a file in the guard's state directory, which lab placement reads from the host."""

    def read(self) -> tuple[bool, ModelHoldRecord | None, str]:
        """(readable, record, detail): readable False when the file exists and cannot be used; record None when there is no hold."""
        ...

    def write(self, record: ModelHoldRecord) -> None: ...

    def clear(self) -> None: ...


@runtime_checkable
class ProtocolEpisodeStore(Protocol):
    def load(self) -> dict[str, Any] | None:
        """The episode record; {} when there is none yet; None when the file cannot be read."""
        ...

    def save(self, episode: dict[str, Any]) -> None: ...


@runtime_checkable
class ProtocolLaneCutter(Protocol):
    def cut(self) -> tuple[str, ...]:
        """Stop the lane scopes and the processes carrying the lane tag; the names of what was stopped."""
        ...


@runtime_checkable
class ProtocolLedgerAppender(Protocol):
    """Append rows through the declared bus append command (the signature CommandLedgerAppender has)."""

    def append(self, rows: str, request_id: str) -> ModelAppendOutcome: ...


__all__ = [
    "ProtocolEpisodeStore",
    "ProtocolHoldStore",
    "ProtocolLaneCutter",
    "ProtocolLedgerAppender",
    "ProtocolThermalSensors",
]
