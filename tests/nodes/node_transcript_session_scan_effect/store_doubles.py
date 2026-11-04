# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Test doubles of the transcript store port's read side: an in-memory store and a directory on disk.

The omnimarket store handlers belong to another task; these doubles exist only so the scan and
the read are exercised against the spi port's own shape.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from omnibase_spi.protocols.storage import ProtocolTranscriptObjectRecord


@dataclass(frozen=True)
class ObjectRecord:
    key: str
    size_bytes: int
    revision: int
    sha256: str


def record_of(key: str, data: bytes) -> ObjectRecord:
    """The port's record of a copy: its byte length, its record count and the sha256 of its bytes."""
    lines = [line for line in data.decode("utf-8").split("\n") if line.strip()]
    return ObjectRecord(
        key=key,
        size_bytes=len(data),
        revision=len(lines),
        sha256=hashlib.sha256(data).hexdigest(),
    )


class InMemoryTranscriptSource:
    """Keyed bytes held in a dict; read side only."""

    def __init__(self, objects: Mapping[str, bytes] | None = None) -> None:
        self.objects: dict[str, bytes] = dict(objects or {})

    async def list_objects(self, prefix: str) -> list[ProtocolTranscriptObjectRecord]:
        return [
            record_of(key, self.objects[key])
            for key in sorted(self.objects)
            if key.startswith(prefix)
        ]

    async def read(self, key: str) -> bytes:
        if key not in self.objects:
            raise KeyError(key)
        return self.objects[key]


class DirectoryTranscriptSource:
    """Files under a directory on disk; read side only."""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def list_objects(self, prefix: str) -> list[ProtocolTranscriptObjectRecord]:
        keys = sorted(
            path.relative_to(self._root).as_posix()
            for path in self._root.rglob("*")
            if path.is_file()
        )
        return [
            record_of(key, (self._root / key).read_bytes())
            for key in keys
            if key.startswith(prefix)
        ]

    async def read(self, key: str) -> bytes:
        if key.startswith("/") or ".." in key.split("/"):
            raise ValueError(key)
        path = self._root / key
        if not path.is_file():
            raise KeyError(key)
        return path.read_bytes()
