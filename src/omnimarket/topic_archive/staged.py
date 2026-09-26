# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A staged archive read back as a topic, so the archiver can move it to another sink.

``StagedArchiveReader`` implements the same read-only reader protocol as the
broker reader, over the manifests and objects a previous archive run left in a
sink (typically a local staging directory). Handing it to the archive handler
with a different sink and cipher re-archives the staged days there: the handler
rebuilds each day file from the staged records, writes and reads it back, and
its source verification re-reads the staged records, so the moved file is
checked against the staged one record for record.

Every staged object is checked against its manifest (object sha256, plaintext
sha256, record count) before a record from it is yielded; a mismatch raises.
Staged runs may overlap (a day archived while open and again after it closed);
records are yielded in offset order and each offset once.
"""

from __future__ import annotations

import gzip
from collections.abc import AsyncIterator

from omnimarket.topic_archive.codec import decode_lines, sha256_hex
from omnimarket.topic_archive.models import ModelArchivedRecord, ModelArchiveManifest
from omnimarket.topic_archive.protocols import (
    ProtocolArchiveCipher,
    ProtocolArchiveSink,
)

_MANIFEST_SUFFIX = ".manifest.json"


class StagedArchiveReader:
    def __init__(
        self, sink: ProtocolArchiveSink, cipher: ProtocolArchiveCipher
    ) -> None:
        self._sink = sink
        self._cipher = cipher
        self._manifests: dict[str, list[ModelArchiveManifest]] | None = None

    def _by_topic(self) -> dict[str, list[ModelArchiveManifest]]:
        if self._manifests is None:
            found: dict[str, list[ModelArchiveManifest]] = {}
            for name in self._sink.list_names(""):
                if not name.endswith(_MANIFEST_SUFFIX):
                    continue
                m = ModelArchiveManifest.model_validate_json(self._sink.get(name))
                found.setdefault(m.topic, []).append(m)
            self._manifests = found
        return self._manifests

    def _of(self, topic: str, partition: int) -> list[ModelArchiveManifest]:
        return sorted(
            (m for m in self._by_topic().get(topic, []) if m.partition == partition),
            key=lambda m: (m.first_offset, -m.last_offset),
        )

    async def partitions(self, topic: str) -> list[int]:
        return sorted({m.partition for m in self._by_topic().get(topic, [])})

    async def watermarks(self, topic: str, partition: int) -> tuple[int, int]:
        """The broker's log start and high watermark as the staged run saw them."""
        ms = self._of(topic, partition)
        if not ms:
            return (0, 0)
        return (
            min(m.source_log_start_offset for m in ms),
            max(m.source_high_watermark for m in ms),
        )

    def _open(self, m: ModelArchiveManifest) -> list[ModelArchivedRecord]:
        if m.encryption is not self._cipher.encryption:
            raise ValueError(
                f"{m.object_name}: staged as {m.encryption}, cipher is "
                f"{self._cipher.encryption}"
            )
        stored = self._sink.get(m.object_name)
        if sha256_hex(stored) != m.object_sha256:
            raise ValueError(f"{m.object_name}: object_sha256 mismatch")
        plain = gzip.decompress(self._cipher.decrypt(stored))
        if sha256_hex(plain) != m.plaintext_sha256:
            raise ValueError(f"{m.object_name}: plaintext_sha256 mismatch")
        records = decode_lines(plain)
        if len(records) != m.record_count:
            raise ValueError(
                f"{m.object_name}: {len(records)} records, manifest says "
                f"{m.record_count}"
            )
        return records

    async def read_range(
        self, topic: str, partition: int, start: int, end_exclusive: int
    ) -> AsyncIterator[ModelArchivedRecord]:
        last = start - 1
        for m in self._of(topic, partition):
            if m.last_offset <= last or m.first_offset >= end_exclusive:
                continue
            for r in self._open(m):
                if r.offset <= last:
                    continue
                if r.offset >= end_exclusive:
                    return
                last = r.offset
                yield r
