# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The run's events as an append-only JSON-lines journal."""

from __future__ import annotations

import json
import time
from pathlib import Path


class JournalEventPublisher:
    """Implements ProtocolEventPublisher over a journal file the overlay names."""

    def __init__(self, path: Path) -> None:
        self._path = path

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "t": time.time(),
                "topic": topic,
                "key": key.decode() if key else None,
                "value": json.loads(value),
            },
            sort_keys=True,
        )
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
