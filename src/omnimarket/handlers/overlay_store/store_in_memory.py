# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory overlay store: the default when no overlay selects a backend."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from omnibase_core.types import JsonType


class InMemoryOverlayStore:
    """Holds one record per overlay id; a redelivered or older load changes nothing."""

    def __init__(self) -> None:
        self._records: dict[str, Mapping[str, JsonType]] = {}

    async def write_version(self, record: Mapping[str, JsonType]) -> bool:
        overlay_id = str(record["overlay_id"])
        current = self._records.get(overlay_id)
        if current is not None and _loaded_at(current) >= _loaded_at(record):
            return False
        self._records[overlay_id] = dict(record)
        return True

    async def read_current(self, overlay_id: str) -> Mapping[str, JsonType] | None:
        return self._records.get(overlay_id)


def _loaded_at(record: Mapping[str, JsonType]) -> datetime:
    return datetime.fromisoformat(str(record["loaded_at"]))


__all__ = ["InMemoryOverlayStore"]
