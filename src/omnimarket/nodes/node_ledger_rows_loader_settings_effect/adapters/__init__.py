# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Event sinks of the ledger rows loader settings node."""

from __future__ import annotations

from pathlib import Path

from omnimarket.nodes.node_ledger_rows_loader_settings_effect.models import (
    ModelLoaderSettingsEvent,
)


class InMemoryLoaderSettingsSink:
    """Keeps the events in a list; the default and the tests' sink."""

    def __init__(self) -> None:
        self.events: list[ModelLoaderSettingsEvent] = []

    def emit(self, event: ModelLoaderSettingsEvent) -> None:
        self.events.append(event)


class JournalLoaderSettingsSink:
    """Appends each event as one JSON line to the journal file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def emit(self, event: ModelLoaderSettingsEvent) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(event.model_dump_json() + "\n")


__all__ = ["InMemoryLoaderSettingsSink", "JournalLoaderSettingsSink"]
