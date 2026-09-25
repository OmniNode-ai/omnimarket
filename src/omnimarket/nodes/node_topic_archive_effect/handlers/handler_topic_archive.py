# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EFFECT: archive event-bus topics to cold storage, one verified file per UTC day.

For each topic partition the handler reads every retained record once, groups
the records by the UTC day of their broker timestamp, and writes per day one
gzip JSONL object (encrypted by the injected cipher) plus a manifest. Then it
verifies each file twice:

1. readback -- the stored object's sha256 must equal the one computed before the
   write, the manifest must read back equal, and when the cipher can decrypt,
   the decompressed plaintext must hash to the manifest's plaintext_sha256;
2. source -- the source offsets first..last are read again from the broker and
   re-encoded, and must hash to the same plaintext_sha256. When the log start
   has already moved past first_offset, the source comparison is reported as
   not possible (None), never as passed.

A day whose offset range is already covered by a manifest in the sink (the
same day archived by an earlier run, before retention moved the log start
into it) is not written again and is listed in ``already_archived``. Broker
offsets are immutable, so a covering range holds the same records. A day that
grew since the earlier run (an open day) is written again as a new object.

Nothing here prunes or changes retention: a verified archive is the premise for
that decision, not the decision.
"""

from __future__ import annotations

import datetime as dt
import gzip
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from omnimarket.nodes.node_topic_archive_effect.handlers._kafka_topic_reader import (
    LazyKafkaTopicReader,
)
from omnimarket.topic_archive.codec import (
    day_prefix,
    gzip_deterministic,
    manifest_name,
    object_name,
    record_line,
    sha256_hex,
    utc_day,
)
from omnimarket.topic_archive.live import live_archive_boundary
from omnimarket.topic_archive.models import (
    EnumArchiveEncryption,
    EnumArchiveVerdict,
    ModelArchiveFileResult,
    ModelArchiveManifest,
    ModelTopicArchiveRequest,
    ModelTopicArchiveResult,
)
from omnimarket.topic_archive.protocols import (
    ProtocolArchiveCipher,
    ProtocolArchiveSink,
    ProtocolTopicReader,
)

_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"


def contract_source_topics() -> list[str]:
    """The topics this node archives by default, as its contract declares them."""
    data = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    topics: list[str] = list(data["config"]["topic_archive"]["source_topics"])
    return topics


@dataclass
class _DayBucket:
    lines: list[bytes] = field(default_factory=list)
    first_offset: int = -1
    last_offset: int = -1
    first_ts: int = 0
    last_ts: int = 0


class HandlerTopicArchive:
    """Archive topics through injected reader, sink and cipher."""

    def __init__(
        self,
        *,
        reader: ProtocolTopicReader | None = None,
        sink: ProtocolArchiveSink | None = None,
        cipher: ProtocolArchiveCipher | None = None,
        source_topics: list[str] | None = None,
        now: dt.datetime | None = None,
    ) -> None:
        if reader is None or sink is None or cipher is None:
            # Runtime dispatch constructs the handler with no arguments; the
            # live boundary is composed here and resolves its addressing when
            # first used, so construction itself never touches the network.
            live_sink, live_cipher = live_archive_boundary()
            reader = reader or LazyKafkaTopicReader()
            sink = sink or live_sink
            cipher = cipher or live_cipher
        self._reader: ProtocolTopicReader = reader
        self._sink: ProtocolArchiveSink = sink
        self._cipher: ProtocolArchiveCipher = cipher
        self._source_topics = source_topics
        self._now = now

    async def handle(
        self, request: ModelTopicArchiveRequest
    ) -> ModelTopicArchiveResult:
        topics = request.topics or self._source_topics or contract_source_topics()
        plaintext = self._cipher.encryption is EnumArchiveEncryption.NONE
        if plaintext and (request.require_encryption or self._sink.requires_encryption):
            return ModelTopicArchiveResult(
                verdict=EnumArchiveVerdict.REFUSED,
                sink_location=self._sink.location,
                topics=topics,
                detail=(
                    "refused: the sink or the request requires encryption and the "
                    "cipher is 'none'; nothing was read or written"
                ),
            )
        now = self._now or dt.datetime.now(dt.UTC)
        wanted = set(request.days) if request.days else None
        files: list[ModelArchiveFileResult] = []
        covered: list[str] = []
        for topic in topics:
            for partition in await self._reader.partitions(topic):
                written, skipped = await self._archive_partition(
                    topic, partition, wanted, now, request
                )
                files.extend(written)
                covered.extend(skipped)
        return ModelTopicArchiveResult(
            verdict=_verdict(files),
            sink_location=self._sink.location,
            topics=topics,
            files=files,
            already_archived=covered,
        )

    async def _archive_partition(
        self,
        topic: str,
        partition: int,
        wanted: set[dt.date] | None,
        now: dt.datetime,
        request: ModelTopicArchiveRequest,
    ) -> tuple[list[ModelArchiveFileResult], list[str]]:
        low, high = await self._reader.watermarks(topic, partition)
        if high <= low:
            return [], []
        buckets: dict[dt.date, _DayBucket] = {}
        first_day: dt.date | None = None
        async for rec in self._reader.read_range(topic, partition, low, high):
            day = utc_day(rec.timestamp_ms)
            if first_day is None:
                first_day = day
            if wanted is not None and day not in wanted:
                continue
            b = buckets.setdefault(day, _DayBucket())
            b.lines.append(record_line(rec))
            if b.first_offset < 0:
                b.first_offset, b.first_ts = rec.offset, rec.timestamp_ms
            b.last_offset, b.last_ts = rec.offset, rec.timestamp_ms
        results: list[ModelArchiveFileResult] = []
        skipped: list[str] = []
        for day in sorted(buckets):
            b = buckets[day]
            covering = self._covering_manifest(topic, partition, day, b)
            if covering is not None:
                skipped.append(covering)
                continue
            manifest = self._write(topic, partition, day, b, low, high, first_day, now)
            results.append(await self._verify(manifest, request.verify_against_source))
        return results, skipped

    def _covering_manifest(
        self, topic: str, partition: int, day: dt.date, b: _DayBucket
    ) -> str | None:
        """A manifest already in the sink whose offsets cover this day's, if any."""
        for name in self._sink.list_names(day_prefix(topic, partition, day)):
            if not name.endswith(".manifest.json"):
                continue
            m = ModelArchiveManifest.model_validate_json(self._sink.get(name))
            if (
                m.encryption is self._cipher.encryption
                and m.first_offset <= b.first_offset
                and m.last_offset >= b.last_offset
            ):
                return name
        return None

    def _write(
        self,
        topic: str,
        partition: int,
        day: dt.date,
        b: _DayBucket,
        low: int,
        high: int,
        first_day: dt.date | None,
        now: dt.datetime,
    ) -> ModelArchiveManifest:
        plain = b"".join(b.lines)
        obj = self._cipher.encrypt(gzip_deterministic(plain))
        name = object_name(
            topic,
            partition,
            day,
            b.first_offset,
            b.last_offset,
            self._cipher.encryption,
        )
        manifest = ModelArchiveManifest(
            topic=topic,
            partition=partition,
            day=day,
            first_offset=b.first_offset,
            last_offset=b.last_offset,
            record_count=len(b.lines),
            first_timestamp_ms=b.first_ts,
            last_timestamp_ms=b.last_ts,
            plaintext_sha256=sha256_hex(plain),
            object_sha256=sha256_hex(obj),
            object_bytes=len(obj),
            object_name=name,
            manifest_name=manifest_name(name),
            encryption=self._cipher.encryption,
            recipient=self._cipher.recipient,
            source_log_start_offset=low,
            source_high_watermark=high,
            day_start_truncated=(day == first_day and low != 0),
            day_open=day >= now.date(),
            archived_at=now,
        )
        self._sink.put(name, obj)
        self._sink.put(
            manifest.manifest_name, manifest.model_dump_json(indent=2).encode("utf-8")
        )
        return manifest

    async def _verify(
        self, m: ModelArchiveManifest, against_source: bool
    ) -> ModelArchiveFileResult:
        problems: list[str] = []
        stored = self._sink.get(m.object_name)
        if sha256_hex(stored) != m.object_sha256:
            problems.append("object_sha256 mismatch on readback")
        elif (
            ModelArchiveManifest.model_validate_json(self._sink.get(m.manifest_name))
            != m
        ):
            problems.append("manifest readback differs")
        else:
            try:
                plain = gzip.decompress(self._cipher.decrypt(stored))
            except RuntimeError:
                plain = None  # the archiver holds only the public recipient
            if plain is not None and sha256_hex(plain) != m.plaintext_sha256:
                problems.append(
                    "plaintext_sha256 mismatch after decrypt and decompress"
                )
        readback_ok = not problems
        source_ok: bool | None = None
        if against_source and readback_ok:
            source_ok = await self._source_matches(m)
            if source_ok is False:
                problems.append("source re-read of the offset range hashes differently")
            elif source_ok is None:
                problems.append(
                    "source offsets already pruned; compared by readback only"
                )
        return ModelArchiveFileResult(
            manifest=m,
            readback_verified=readback_ok,
            source_verified=source_ok,
            detail="; ".join(problems),
        )

    async def _source_matches(self, m: ModelArchiveManifest) -> bool | None:
        low, _high = await self._reader.watermarks(m.topic, m.partition)
        if low > m.first_offset:
            return None
        lines = [
            record_line(r)
            async for r in self._reader.read_range(
                m.topic, m.partition, m.first_offset, m.last_offset + 1
            )
            if utc_day(r.timestamp_ms) == m.day
        ]
        return (
            len(lines) == m.record_count
            and sha256_hex(b"".join(lines)) == m.plaintext_sha256
        )


def _verdict(files: list[ModelArchiveFileResult]) -> EnumArchiveVerdict:
    if not files:
        return EnumArchiveVerdict.NOTHING_TO_ARCHIVE
    if any(not f.readback_verified or f.source_verified is False for f in files):
        return EnumArchiveVerdict.FAILED
    if any(f.source_verified is None for f in files):
        return EnumArchiveVerdict.READBACK_ONLY
    return EnumArchiveVerdict.VERIFIED
