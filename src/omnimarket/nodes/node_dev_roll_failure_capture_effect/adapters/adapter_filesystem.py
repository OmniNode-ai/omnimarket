# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Filesystem artifact store of node_dev_roll_failure_capture_effect (OMN-24013).

Writes files under its root, refuses names that escape the root (path
traversal), creates directories, and prunes files older than the retention at
write time.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from omnimarket.nodes.node_dev_roll_failure_capture_effect.protocols.protocol_roll_artifact_store import (
    RollArtifactStoreError,
)


class FilesystemRollArtifactStore:
    """Write artifacts under ``root``; prune by retention; refuse traversal."""

    def __init__(
        self,
        root: Path,
        retention: timedelta,
        clock: Callable[[], datetime],
    ) -> None:
        self._root = root.resolve()
        self._retention = retention
        self._clock = clock

    def write(self, name: str, content: bytes) -> str:
        target = (self._root / name).resolve()
        if not str(target).startswith(str(self._root)):
            raise RollArtifactStoreError(f"name escapes the store root: {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        self._prune()
        target.write_bytes(content)
        return str(target)

    def _prune(self) -> None:
        cutoff = self._clock() - self._retention
        for path in self._root.rglob("*"):
            if path.is_file() and datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                path.unlink()
