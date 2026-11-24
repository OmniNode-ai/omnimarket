# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Keep a run's recorded events on this machine when no hosted bus is declared (OMN-26451).

A machine whose operator declared no hosted bus (no overlay for ``node_runtime_bus_forward_effect``) has
nowhere to carry a runtime-local run's events. They are appended, one JSON object per line, to a file under the
user's ``.onex`` directory, so the run's events are on disk rather than lost with the process, and the receipt
says they were kept locally and not delivered to a hosted bus. A write that fails raises ``OSError``: the
caller reports it as a degraded reading, never as a delivery.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path

from omnimarket.nodes.node_runtime_bus_forward_effect.models.model_runtime_bus_forward import (
    ModelForwardEvent,
)

LOCAL_RECORD_FILE_NAME = "runtime_bus_events.jsonl"


class HandlerRuntimeBusLocalRecord:
    """Appends recorded events to the local record file and answers how many it kept."""

    def __init__(self, *, record_dir: Path) -> None:
        self._record_dir = record_dir

    @property
    def path(self) -> Path:
        return self._record_dir / LOCAL_RECORD_FILE_NAME

    def handle(self, events: Sequence[ModelForwardEvent]) -> int:
        """Append ``events`` in order and return the count kept; ``OSError`` when the file cannot be written."""
        self._record_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        lines = "".join(
            json.dumps(event.model_dump(mode="json"), sort_keys=True) + "\n"
            for event in events
        )
        descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as record:
            record.write(lines)
        return len(events)
