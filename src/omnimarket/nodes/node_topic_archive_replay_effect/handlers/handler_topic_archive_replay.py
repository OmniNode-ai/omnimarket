# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EFFECT: read verified topic archives back onto a replay topic.

Each manifest under the requested prefix is checked before a single record is
published: the stored object must hash to the manifest's object_sha256, the
decrypted and decompressed plaintext to its plaintext_sha256, and the decoded
record count must match. A file failing any check is refused whole and named.

Records keep their key, value, headers and broker timestamp, and gain three
provenance headers (source topic, partition, offset). The replay target is the
contract's replay topic; a request naming the archive's own source topic is
refused, so a replay can never write back into live capture.

Archives of one day can overlap (the day archived while open and again once
closed, or after retention moved its start): manifests are taken in offset
order, largest range first, and each source offset is published once.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import yaml

from omnimarket.topic_archive.codec import decode_lines, sha256_hex
from omnimarket.topic_archive.live import live_replay_boundary
from omnimarket.topic_archive.models import (
    ModelArchivedRecord,
    ModelArchiveManifest,
    ModelTopicArchiveReplayRequest,
    ModelTopicArchiveReplayResult,
)
from omnimarket.topic_archive.protocols import (
    ProtocolArchiveCipher,
    ProtocolArchiveSink,
    ProtocolReplayWriter,
)

_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
_MANIFEST_SUFFIX = ".manifest.json"


def contract_replay_topic() -> str:
    data = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    topic: str = data["config"]["topic_archive_replay"]["replay_topic"]
    return topic


def _provenance_headers() -> tuple[str, str, str]:
    data = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    h = data["config"]["topic_archive_replay"]["provenance_headers"]
    return (h["source_topic"], h["source_partition"], h["source_offset"])


class HandlerTopicArchiveReplay:
    def __init__(
        self,
        *,
        sink: ProtocolArchiveSink | None = None,
        cipher: ProtocolArchiveCipher | None = None,
        writer: ProtocolReplayWriter | None = None,
    ) -> None:
        if sink is None or cipher is None or writer is None:
            live_sink, live_cipher, live_writer = live_replay_boundary()
            sink = sink or live_sink
            cipher = cipher or live_cipher
            writer = writer or live_writer
        self._sink: ProtocolArchiveSink = sink
        self._cipher: ProtocolArchiveCipher = cipher
        self._writer: ProtocolReplayWriter = writer

    async def handle(
        self, request: ModelTopicArchiveReplayRequest
    ) -> ModelTopicArchiveReplayResult:
        replay_topic = request.replay_topic or contract_replay_topic()
        h_topic, h_part, h_off = _provenance_headers()
        verified = refused = replayed = 0
        details: list[str] = []
        manifests = sorted(
            (
                ModelArchiveManifest.model_validate_json(self._sink.get(n))
                for n in self._sink.list_names(request.manifest_prefix)
                if n.endswith(_MANIFEST_SUFFIX)
            ),
            key=lambda m: (m.topic, m.partition, m.first_offset, -m.last_offset),
        )
        seen: set[tuple[str, int, int]] = set()
        for m in manifests:
            problem = self._check_target(m, replay_topic)
            records = None
            if problem is None:
                problem, records = self._open(m)
            if problem is not None or records is None:
                refused += 1
                details.append(f"refused {m.object_name}: {problem}")
                continue
            verified += 1
            if request.dry_run:
                continue
            for r in records:
                if (r.topic, r.partition, r.offset) in seen:
                    continue
                seen.add((r.topic, r.partition, r.offset))
                await self._writer.publish(
                    replay_topic,
                    key=r.key_bytes(),
                    value=r.value_bytes(),
                    headers=[
                        *r.header_pairs(),
                        (h_topic, r.topic.encode()),
                        (h_part, str(r.partition).encode()),
                        (h_off, str(r.offset).encode()),
                    ],
                    timestamp_ms=r.timestamp_ms,
                )
                replayed += 1
        return ModelTopicArchiveReplayResult(
            replay_topic=replay_topic,
            verified_files=verified,
            refused_files=refused,
            replayed_records=replayed,
            details=details,
        )

    @staticmethod
    def _check_target(m: ModelArchiveManifest, replay_topic: str) -> str | None:
        if replay_topic == m.topic:
            return "the replay topic is the archive's source topic"
        return None

    def _open(
        self, m: ModelArchiveManifest
    ) -> tuple[str | None, list[ModelArchivedRecord] | None]:
        if m.encryption is not self._cipher.encryption:
            return (
                f"archive is {m.encryption}, cipher is {self._cipher.encryption}",
                None,
            )
        stored = self._sink.get(m.object_name)
        if sha256_hex(stored) != m.object_sha256:
            return "object_sha256 mismatch", None
        plain = gzip.decompress(self._cipher.decrypt(stored))
        if sha256_hex(plain) != m.plaintext_sha256:
            return "plaintext_sha256 mismatch", None
        records = decode_lines(plain)
        if len(records) != m.record_count:
            return f"record_count {len(records)} != manifest {m.record_count}", None
        return None, records
