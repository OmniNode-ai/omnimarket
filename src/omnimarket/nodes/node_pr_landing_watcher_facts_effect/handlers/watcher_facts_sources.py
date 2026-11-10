# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""File-backend ports of the landing watcher facts node (OMN-23507).

- WatcherStateFile: the PR watcher's state file, read once per pass.
- AtomicLandingFactsFile: the landing facts file the snapshot step's LandingFactsFileSource reads,
  replaced whole (a temporary file in the same directory, then a rename), so a reader never sees a
  half-written file.
- JsonlEventJournalPublisher: appends each written or failed event, with its topic, to a journal.

The overlay names every path; build_watcher_facts_ports wires them.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel

from omnimarket.models.landing_decision import ModelLandingFacts
from omnimarket.nodes.node_pr_landing_watcher_facts_effect.handlers.watcher_facts_errors import (
    WatcherFactsSourceError,
)
from omnimarket.nodes.node_pr_landing_watcher_facts_effect.models.model_watcher_facts_overlay import (
    EnumWatcherFactsBackend,
    ModelWatcherFactsOverlay,
)

EVENT_JOURNAL_NAME = "watcher-facts-events.jsonl"


class WatcherStateFile:
    """The PR watcher's state file."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def read_state(self) -> object:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise WatcherFactsSourceError(
                f"{self._path} unreadable: {type(exc).__name__}"
            ) from None


class AtomicLandingFactsFile:
    """The landing facts file, replaced whole on every write."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def write(self, facts: ModelLandingFacts) -> str:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=self._path.parent, prefix=f".{self._path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(facts.model_dump_json())
            os.replace(tmp, self._path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return str(self._path)


class JsonlEventJournalPublisher:
    """Appends each event, with its topic, to a journal."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def publish(self, topic: str, event: BaseModel) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"topic": topic, "event": event.model_dump(mode="json")})
                + "\n"
            )


def resolve_path(value: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(value)))


def build_watcher_facts_ports(
    overlay: ModelWatcherFactsOverlay,
) -> tuple[WatcherStateFile, AtomicLandingFactsFile, JsonlEventJournalPublisher]:
    """The file backend's ports, every path from the overlay."""
    if overlay.backend is not EnumWatcherFactsBackend.FILE:
        raise ValueError("the in-memory backend's ports are injected, not built")
    if not (
        overlay.watcher_state_path and overlay.facts_path and overlay.emission_journal
    ):
        raise ValueError("the file backend needs its overlay fields")
    return (
        WatcherStateFile(resolve_path(overlay.watcher_state_path)),
        AtomicLandingFactsFile(resolve_path(overlay.facts_path)),
        JsonlEventJournalPublisher(
            resolve_path(overlay.emission_journal).with_name(EVENT_JOURNAL_NAME)
        ),
    )


__all__: list[str] = [
    "EVENT_JOURNAL_NAME",
    "AtomicLandingFactsFile",
    "JsonlEventJournalPublisher",
    "WatcherStateFile",
    "build_watcher_facts_ports",
    "resolve_path",
]
