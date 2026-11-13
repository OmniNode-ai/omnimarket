# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Event sinks of the pause check findings: in memory by default, a journal file on a host."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_pause_check_findings_compute.models import (
    ModelPauseCheckEvent,
)


class InMemoryPauseCheckSink:
    """Keeps the events in a list; the default and the tests' sink."""

    def __init__(self) -> None:
        self.events: list[ModelPauseCheckEvent] = []

    def emit(self, event: ModelPauseCheckEvent) -> None:
        self.events.append(event)


class JournalPauseCheckSink:
    """Appends each event as one JSON line; the floor and a reading read the journal."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def emit(self, event: ModelPauseCheckEvent) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(event.model_dump_json() + "\n")


__all__ = ["InMemoryPauseCheckSink", "JournalPauseCheckSink"]
