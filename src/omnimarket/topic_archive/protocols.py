# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The I/O boundary of the archive and replay nodes, one protocol per side effect."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from omnimarket.topic_archive.models import EnumArchiveEncryption, ModelArchivedRecord


class ProtocolTopicReader(Protocol):
    """Read-only access to a broker: no consumer group, no commits, no produce."""

    async def partitions(self, topic: str) -> list[int]: ...

    async def watermarks(self, topic: str, partition: int) -> tuple[int, int]:
        """(log start offset, high watermark) of one partition."""
        ...

    def read_range(
        self, topic: str, partition: int, start: int, end_exclusive: int
    ) -> AsyncIterator[ModelArchivedRecord]:
        """Every retained record with ``start <= offset < end_exclusive``."""
        ...


class ProtocolArchiveSink(Protocol):
    """Where archive objects and manifests are stored."""

    @property
    def requires_encryption(self) -> bool:
        """True for a sink that leaves the host (a shared drive); the archiver
        then refuses to write anything a cipher has not encrypted."""
        ...

    @property
    def location(self) -> str: ...

    def put(self, name: str, data: bytes) -> None: ...

    def get(self, name: str) -> bytes: ...

    def exists(self, name: str) -> bool: ...

    def list_names(self, prefix: str) -> list[str]: ...


class ProtocolArchiveCipher(Protocol):
    @property
    def encryption(self) -> EnumArchiveEncryption: ...

    @property
    def recipient(self) -> str | None: ...

    def encrypt(self, data: bytes) -> bytes: ...

    def decrypt(self, data: bytes) -> bytes: ...


class ProtocolReplayWriter(Protocol):
    async def publish(
        self,
        topic: str,
        *,
        key: bytes | None,
        value: bytes | None,
        headers: list[tuple[str, bytes | None]],
        timestamp_ms: int,
    ) -> None: ...
