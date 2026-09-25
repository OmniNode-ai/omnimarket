# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_topic_archive_effect: codec, archive, readback and source verification."""

from __future__ import annotations

import datetime as dt
import gzip
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_topic_archive_effect.handlers.handler_topic_archive import (
    HandlerTopicArchive,
)
from omnimarket.topic_archive.codec import (
    decode_lines,
    encode_lines,
    gzip_deterministic,
    object_name,
    record_line,
    utc_day,
)
from omnimarket.topic_archive.live import LocalDirArchiveSink, NoArchiveCipher
from omnimarket.topic_archive.models import (
    EnumArchiveEncryption,
    EnumArchiveVerdict,
    ModelArchivedRecord,
    ModelArchiveManifest,
    ModelTopicArchiveRequest,
)
from tests.topic_archive_fakes import (
    DAY1,
    DAY2,
    NODES,
    FakeTopic,
    MemorySink,
    T,
    XorCipher,
    ms,
    rec,
    source,
)

pytestmark = pytest.mark.unit

# --- codec -----------------------------------------------------------------


def test_utc_day_splits_at_midnight_utc() -> None:
    assert utc_day(ms(DAY1)) == dt.date(2026, 9, 18)
    assert utc_day(ms(DAY2)) == dt.date(2026, 9, 19)


def test_record_round_trip_is_byte_exact_including_null_header_values() -> None:
    r = rec(T, 1, DAY1, b"\x00\xffnot-utf8")
    (back,) = decode_lines(encode_lines([r]))
    assert back == r
    assert back.value_bytes() == b"\x00\xffnot-utf8"
    assert back.header_pairs() == [("correlation_id", b"c-1"), ("empty", None)]


def test_gzip_is_deterministic_so_checksums_are_reproducible() -> None:
    data = encode_lines([rec(T, 1, DAY1, b"x")])
    assert gzip_deterministic(data) == gzip_deterministic(data)
    assert gzip.decompress(gzip_deterministic(data)) == data


def test_record_line_is_one_compact_line() -> None:
    line = record_line(rec(T, 1, DAY1, b"x"))
    assert line.endswith(b"\n")
    assert line.count(b"\n") == 1


def test_object_name_is_partitioned_by_topic_partition_and_day() -> None:
    name = object_name(
        T, 0, dt.date(2026, 9, 18), 10, 11, EnumArchiveEncryption.AGE_X25519
    )
    assert name == f"{T}/partition=0/day=2026-09-18/offsets-10-11.jsonl.gz.age"


# --- archive ---------------------------------------------------------------


async def test_archive_writes_one_verified_file_per_day_with_manifest() -> None:
    sink = MemorySink()
    result = await HandlerTopicArchive(
        reader=source(), sink=sink, cipher=XorCipher()
    ).handle(ModelTopicArchiveRequest(topics=[T]))
    assert result.verdict is EnumArchiveVerdict.VERIFIED
    assert [f.manifest.day for f in result.files] == [
        dt.date(2026, 9, 18),
        dt.date(2026, 9, 19),
    ]
    first = result.files[0]
    m = first.manifest
    assert (m.first_offset, m.last_offset, m.record_count) == (10, 11, 2)
    assert m.encryption is EnumArchiveEncryption.AGE_X25519
    assert m.object_sha256 == hashlib.sha256(sink.get(m.object_name)).hexdigest()
    # The log start (10) is the first archived offset and is not 0, so older
    # records of that day may already be gone: the manifest says so.
    assert m.day_start_truncated is True
    assert result.files[1].manifest.day_start_truncated is False
    assert first.source_verified is True
    assert ModelArchiveManifest.model_validate_json(sink.get(m.manifest_name)) == m


async def test_archive_detects_a_corrupted_object_on_readback() -> None:
    class CorruptingSink(MemorySink):
        def put(self, name: str, data: bytes) -> None:
            super().put(name, data if name.endswith(".json") else data[:-1] + b"?")

    result = await HandlerTopicArchive(
        reader=source(), sink=CorruptingSink(), cipher=XorCipher()
    ).handle(ModelTopicArchiveRequest(topics=[T]))
    assert result.verdict is EnumArchiveVerdict.FAILED
    assert all(not f.readback_verified for f in result.files)
    assert "object_sha256" in result.files[0].detail


async def test_archive_detects_source_drift_between_read_and_verify() -> None:
    class DriftingTopic(FakeTopic):
        async def read_range(
            self, topic: str, partition: int, start: int, end_exclusive: int
        ) -> AsyncIterator[ModelArchivedRecord]:
            self.reads += 1
            for r in self.records[topic]:
                if start <= r.offset < end_exclusive:
                    yield (
                        r
                        if self.reads == 1
                        else r.model_copy(update={"value_b64": "AA=="})
                    )

    result = await HandlerTopicArchive(
        reader=DriftingTopic(source().records), sink=MemorySink(), cipher=XorCipher()
    ).handle(ModelTopicArchiveRequest(topics=[T]))
    assert result.verdict is EnumArchiveVerdict.FAILED
    assert result.files[0].source_verified is False


async def test_source_already_pruned_is_reported_not_passed() -> None:
    src = source()

    class PruneAfterFirstRead(FakeTopic):
        async def read_range(
            self, topic: str, partition: int, start: int, end_exclusive: int
        ) -> AsyncIterator[ModelArchivedRecord]:
            async for r in super().read_range(topic, partition, start, end_exclusive):
                yield r
            self.low[topic] = 13

    result = await HandlerTopicArchive(
        reader=PruneAfterFirstRead(src.records), sink=MemorySink(), cipher=XorCipher()
    ).handle(ModelTopicArchiveRequest(topics=[T]))
    assert result.verdict is EnumArchiveVerdict.READBACK_ONLY
    assert all(f.readback_verified and f.source_verified is None for f in result.files)


async def test_a_sink_that_requires_encryption_refuses_plaintext() -> None:
    sink = MemorySink(requires_encryption=True)
    result = await HandlerTopicArchive(
        reader=source(), sink=sink, cipher=NoArchiveCipher()
    ).handle(ModelTopicArchiveRequest(topics=[T]))
    assert result.verdict is EnumArchiveVerdict.REFUSED
    assert sink.objects == {}


async def test_request_encryption_requirement_refuses_plaintext() -> None:
    sink = MemorySink()
    result = await HandlerTopicArchive(
        reader=source(), sink=sink, cipher=NoArchiveCipher()
    ).handle(ModelTopicArchiveRequest(topics=[T], require_encryption=True))
    assert result.verdict is EnumArchiveVerdict.REFUSED
    assert sink.objects == {}


async def test_day_filter_archives_only_requested_days() -> None:
    result = await HandlerTopicArchive(
        reader=source(), sink=MemorySink(), cipher=XorCipher()
    ).handle(ModelTopicArchiveRequest(topics=[T], days=[dt.date(2026, 9, 19)]))
    assert [f.manifest.day for f in result.files] == [dt.date(2026, 9, 19)]


async def test_empty_topic_archives_nothing_and_says_so() -> None:
    result = await HandlerTopicArchive(
        reader=FakeTopic({"onex.evt.omniclaude.empty.v1": []}),
        sink=MemorySink(),
        cipher=XorCipher(),
    ).handle(ModelTopicArchiveRequest(topics=["onex.evt.omniclaude.empty.v1"]))
    assert result.files == []
    assert result.verdict is EnumArchiveVerdict.NOTHING_TO_ARCHIVE


async def test_default_topics_come_from_the_contract() -> None:
    contract = yaml.safe_load(
        (NODES / "node_topic_archive_effect" / "contract.yaml").read_text()
    )
    declared = contract["config"]["topic_archive"]["source_topics"]
    assert "onex.evt.omniclaude.tool-executed.v1" in declared
    reader = FakeTopic({t: [] for t in declared})
    result = await HandlerTopicArchive(
        reader=reader, sink=MemorySink(), cipher=XorCipher()
    ).handle(ModelTopicArchiveRequest())
    assert sorted(result.topics) == sorted(declared)


def test_local_sink_writes_owner_only_files_atomically(tmp_path: Path) -> None:
    sink = LocalDirArchiveSink(tmp_path / "staging")
    sink.put("a/b/c.bin", b"123")
    assert sink.get("a/b/c.bin") == b"123"
    assert (tmp_path / "staging" / "a" / "b" / "c.bin").stat().st_mode & 0o777 == 0o600
    assert sink.list_names("a/") == ["a/b/c.bin"]
    for d in ("staging", "staging/a", "staging/a/b"):
        assert (tmp_path / d).stat().st_mode & 0o777 == 0o700
    with pytest.raises(ValueError, match="inside the sink"):
        sink.put("../escape", b"x")


def test_age_cipher_round_trip() -> None:
    pyrage = pytest.importorskip("pyrage")
    from omnimarket.topic_archive.live import AgeArchiveCipher

    ident = pyrage.x25519.Identity.generate()
    enc = AgeArchiveCipher(recipient=str(ident.to_public()))
    blob = enc.encrypt(b"hello")
    assert blob != b"hello"
    with pytest.raises(RuntimeError):
        enc.decrypt(blob)  # the archiver holds only the public recipient
    dec = AgeArchiveCipher(recipient=str(ident.to_public()), identity=str(ident))
    assert dec.decrypt(blob) == b"hello"
