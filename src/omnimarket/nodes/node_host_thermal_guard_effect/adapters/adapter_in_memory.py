# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory adapters: the default profile, the simulated run, and the tests' fakes."""

from __future__ import annotations

from typing import Any

from omnimarket.nodes.node_reading_writer_effect.protocols import ModelAppendOutcome

from ..models import ModelHoldRecord, ModelThermalReading


class InMemorySensors:
    """Returns the readings in order, the last one for every further read."""

    def __init__(self, *readings: ModelThermalReading) -> None:
        self.readings = list(readings)

    def read(self) -> ModelThermalReading:
        return self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]


class InMemoryHoldStore:
    """The hold file in memory; ``unreadable`` makes it read as a file that exists and cannot be used."""

    def __init__(self) -> None:
        self.record: ModelHoldRecord | None = None
        self.unreadable: str | None = None
        self.write_error: OSError | None = None

    def read(self) -> tuple[bool, ModelHoldRecord | None, str]:
        if self.unreadable is not None:
            return False, None, self.unreadable
        return True, self.record, ""

    def write(self, record: ModelHoldRecord) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.record = record

    def clear(self) -> None:
        self.record = None


class InMemoryEpisodeStore:
    """The episode file in memory; ``record`` None reads as unreadable."""

    def __init__(self) -> None:
        self.record: dict[str, Any] | None = {}
        self.save_error: OSError | None = None

    def load(self) -> dict[str, Any] | None:
        return None if self.record is None else dict(self.record)

    def save(self, episode: dict[str, Any]) -> None:
        if self.save_error is not None:
            raise self.save_error
        self.record = dict(episode)


class InMemoryLaneCutter:
    def __init__(self) -> None:
        self.cuts = 0

    def cut(self) -> tuple[str, ...]:
        self.cuts += 1
        return ("onexlane-test.scope",)


class InMemoryLedgerAppender:
    """Records every append; ``outcomes`` maps a request id to a status, ``default`` is the status of any other."""

    def __init__(self, default: str = "accepted") -> None:
        self.default = default
        self.outcomes: dict[str, str] = {}
        self.sent: list[tuple[str, str]] = []

    def append(self, rows: str, request_id: str) -> ModelAppendOutcome:
        self.sent.append((request_id, rows))
        status = self.outcomes.get(request_id, self.default)
        return ModelAppendOutcome(
            status=status,
            message="" if status in ("accepted", "duplicate") else f"injected {status}",
        )
