# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where emission events go (OMN-19998, 17a): in memory by default, an append-only JSON-lines journal as the real backend.

The journal is the durable record the receipts are read from. Publishing the same events on the bus topics the
emission compute node declares is the runtime's job once the node is attached; this sink does not publish.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from omnimarket.models.floor_17a import ModelNodeEmission


class ProtocolEmissionSink(Protocol):
    def emit(self, event: ModelNodeEmission) -> None: ...

    def read(self, since: datetime) -> tuple[ModelNodeEmission, ...]: ...


class InMemoryEmissionSink:
    def __init__(self) -> None:
        self.events: list[ModelNodeEmission] = []

    def emit(self, event: ModelNodeEmission) -> None:
        self.events.append(event)

    def read(self, since: datetime) -> tuple[ModelNodeEmission, ...]:
        return tuple(e for e in self.events if e.emitted_at >= since)


class JsonlEmissionSink:
    """Append-only journal. A line that does not parse is skipped and counted, never silently fixed."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.skipped_lines = 0

    def emit(self, event: ModelNodeEmission) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = event.model_dump_json() + "\n"
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def read(self, since: datetime) -> tuple[ModelNodeEmission, ...]:
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError:
            return ()
        events: list[ModelNodeEmission] = []
        self.skipped_lines = 0
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                event = ModelNodeEmission.model_validate(json.loads(line))
            except (ValueError, ValidationError):
                self.skipped_lines += 1
                continue
            if event.emitted_at >= since:
                events.append(event)
        return tuple(events)
