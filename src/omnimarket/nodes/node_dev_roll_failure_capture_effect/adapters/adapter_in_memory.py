# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory artifact store of node_dev_roll_failure_capture_effect (OMN-24013).

Deterministic references ``memory://<name>``. Tests that need a failing store
use their own stub; this adapter has no failure option.
"""

from __future__ import annotations


class InMemoryRollArtifactStore:
    """A dict-backed store with deterministic ``memory://`` references."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    def write(self, name: str, content: bytes) -> str:
        ref = f"memory://{name}"
        self._store[ref] = content
        return ref

    def get(self, ref: str) -> bytes:
        return self._store[ref]

    def has(self, ref: str) -> bool:
        return ref in self._store
