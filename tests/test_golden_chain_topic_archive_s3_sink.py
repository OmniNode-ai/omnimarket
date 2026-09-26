# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""S3 sink, KMS envelope cipher, staged-archive reader, covered-day skip and
offset-deduplicated replay for the topic archive nodes (OMN-19513)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_topic_archive_effect.handlers.handler_topic_archive import (
    HandlerTopicArchive,
)
from omnimarket.nodes.node_topic_archive_replay_effect.handlers.handler_topic_archive_replay import (
    HandlerTopicArchiveReplay,
)
from omnimarket.topic_archive.live import NoArchiveCipher
from omnimarket.topic_archive.live_aws import (
    KmsEnvelopeArchiveCipher,
    S3ArchiveSink,
)
from omnimarket.topic_archive.models import (
    EnumArchiveEncryption,
    EnumArchiveVerdict,
    ModelArchiveManifest,
    ModelTopicArchiveReplayRequest,
    ModelTopicArchiveRequest,
)
from omnimarket.topic_archive.staged import StagedArchiveReader
from tests.topic_archive_fakes import (
    DAY1,
    DAY2,
    KMS_KEY_ARN,
    FakeKms,
    FakeS3,
    FakeTopic,
    MemorySink,
    MemoryWriter,
    T,
    rec,
    source,
)

pytestmark = pytest.mark.unit

BUCKET = "archive-bucket"


def _s3(fake: FakeS3 | None = None) -> tuple[S3ArchiveSink, FakeS3]:
    fake = fake or FakeS3()
    return (
        S3ArchiveSink(
            client=fake, bucket=BUCKET, prefix="topic-archive/", kms_key_arn=KMS_KEY_ARN
        ),
        fake,
    )


def _kms(fake: FakeKms | None = None) -> KmsEnvelopeArchiveCipher:
    return KmsEnvelopeArchiveCipher(client=fake or FakeKms(), key_arn=KMS_KEY_ARN)


# --- KMS envelope cipher ---------------------------------------------------


def test_envelope_round_trip_carries_the_wrapped_key_and_needs_kms_to_open() -> None:
    kms = FakeKms()
    cipher = _kms(kms)
    blob = cipher.encrypt(b"hook record bytes")
    assert b"hook record bytes" not in blob
    assert b"wrapped|" in blob  # the wrapped data key travels with the object
    assert cipher.encryption is EnumArchiveEncryption.KMS_ENVELOPE_AES256GCM
    assert cipher.recipient == KMS_KEY_ARN
    assert cipher.decrypt(blob) == b"hook record bytes"
    assert (kms.generated, kms.decrypted) == (1, 1)


def test_each_object_gets_its_own_data_key() -> None:
    cipher = _kms()
    assert cipher.encrypt(b"same") != cipher.encrypt(b"same")


def test_tampered_envelope_is_rejected() -> None:
    cipher = _kms()
    blob = bytearray(cipher.encrypt(b"payload"))
    blob[-1] ^= 0x01
    with pytest.raises(ValueError, match="envelope"):
        cipher.decrypt(bytes(blob))


def test_not_an_envelope_is_rejected() -> None:
    with pytest.raises(ValueError, match="envelope"):
        _kms().decrypt(b"plain gzip bytes")


# --- S3 sink ---------------------------------------------------------------


def test_s3_sink_writes_sse_kms_under_its_prefix_and_reads_back() -> None:
    sink, fake = _s3()
    assert sink.requires_encryption is True
    assert sink.location == f"s3://{BUCKET}/topic-archive/"
    sink.put("t/partition=0/day=2026-09-18/a.bin", b"x")
    stored = fake.store[f"{BUCKET}/topic-archive/t/partition=0/day=2026-09-18/a.bin"]
    assert stored["ServerSideEncryption"] == "aws:kms"
    assert stored["SSEKMSKeyId"] == KMS_KEY_ARN
    assert sink.get("t/partition=0/day=2026-09-18/a.bin") == b"x"
    assert sink.exists("t/partition=0/day=2026-09-18/a.bin")
    assert not sink.exists("t/partition=0/day=2026-09-18/a")
    sink.put("t/partition=0/day=2026-09-19/b.bin", b"y")
    sink.put("u/c.bin", b"z")
    assert sink.list_names("t/") == [
        "t/partition=0/day=2026-09-18/a.bin",
        "t/partition=0/day=2026-09-19/b.bin",
    ]


def test_s3_sink_fails_a_put_whose_head_reports_another_key() -> None:
    sink, _ = _s3(FakeS3(report_key="arn:aws:kms:us-east-1:000000000000:key/other"))
    with pytest.raises(RuntimeError, match="SSE-KMS"):
        sink.put("t/a.bin", b"x")


def test_s3_sink_rejects_escaping_names() -> None:
    sink, _ = _s3()
    with pytest.raises(ValueError, match="inside the sink"):
        sink.put("../escape", b"x")


# --- archive through S3 ----------------------------------------------------


async def test_off_host_s3_sink_still_refuses_plaintext() -> None:
    sink, fake = _s3()
    result = await HandlerTopicArchive(
        reader=source(), sink=sink, cipher=NoArchiveCipher()
    ).handle(ModelTopicArchiveRequest(topics=[T]))
    assert result.verdict is EnumArchiveVerdict.REFUSED
    assert fake.store == {}


async def test_archive_to_s3_is_envelope_encrypted_and_verified_by_decrypt() -> None:
    sink, fake = _s3()
    kms = FakeKms()
    result = await HandlerTopicArchive(
        reader=source(), sink=sink, cipher=_kms(kms)
    ).handle(ModelTopicArchiveRequest(topics=[T]))
    assert result.verdict is EnumArchiveVerdict.VERIFIED
    m = result.files[0].manifest
    assert m.object_name.endswith(".jsonl.gz.kmsenv")
    assert m.encryption is EnumArchiveEncryption.KMS_ENVELOPE_AES256GCM
    assert m.recipient == KMS_KEY_ARN
    # readback decrypted every object through KMS
    assert kms.decrypted == len(result.files) == 2
    assert all(v["SSEKMSKeyId"] == KMS_KEY_ARN for v in fake.store.values())


# --- staged archive as the source ------------------------------------------


async def _staged_plaintext() -> MemorySink:
    staging = MemorySink()
    r = await HandlerTopicArchive(
        reader=source(), sink=staging, cipher=NoArchiveCipher()
    ).handle(ModelTopicArchiveRequest(topics=[T]))
    assert r.verdict is EnumArchiveVerdict.VERIFIED
    return staging


async def test_staged_archive_moves_to_s3_with_identical_plaintext_checksums() -> None:
    staging = await _staged_plaintext()
    staged = {
        n: ModelArchiveManifest.model_validate_json(staging.get(n))
        for n in staging.list_names("")
        if n.endswith(".manifest.json")
    }
    sink, _ = _s3()
    result = await HandlerTopicArchive(
        reader=StagedArchiveReader(staging, NoArchiveCipher()),
        sink=sink,
        cipher=_kms(),
    ).handle(ModelTopicArchiveRequest(topics=[T]))
    assert result.verdict is EnumArchiveVerdict.VERIFIED
    assert all(f.source_verified is True for f in result.files)
    moved = {
        (f.manifest.day, f.manifest.plaintext_sha256, f.manifest.record_count)
        for f in result.files
    }
    assert moved == {
        (m.day, m.plaintext_sha256, m.record_count) for m in staged.values()
    }
    # the original log start and high watermark carry over from the staged manifests
    first = min(result.files, key=lambda f: f.manifest.day).manifest
    assert (first.source_log_start_offset, first.source_high_watermark) == (10, 13)
    assert first.day_start_truncated is True


async def test_staged_reader_lists_topics_partitions_and_ignores_unknown_topics() -> (
    None
):
    reader = StagedArchiveReader(await _staged_plaintext(), NoArchiveCipher())
    assert await reader.partitions(T) == [0]
    assert await reader.partitions(T + ".absent") == []


async def test_staged_reader_refuses_a_corrupted_staged_object() -> None:
    staging = await _staged_plaintext()
    name = next(n for n in staging.list_names("") if n.endswith(".jsonl.gz"))
    staging.objects[name] = staging.objects[name][:-1] + b"?"
    reader = StagedArchiveReader(staging, NoArchiveCipher())
    with pytest.raises(ValueError, match="object_sha256"):
        async for _ in reader.read_range(T, 0, 0, 100):
            pass


async def test_staged_reader_yields_each_offset_once_across_overlapping_partials() -> (
    None
):
    staging = MemorySink()
    partial = FakeTopic({T: [rec(T, 10, DAY1, b"1"), rec(T, 11, DAY1, b"2")]})
    await HandlerTopicArchive(
        reader=partial, sink=staging, cipher=NoArchiveCipher()
    ).handle(ModelTopicArchiveRequest(topics=[T]))
    await HandlerTopicArchive(
        reader=source(), sink=staging, cipher=NoArchiveCipher()
    ).handle(ModelTopicArchiveRequest(topics=[T]))
    reader = StagedArchiveReader(staging, NoArchiveCipher())
    offsets = [r.offset async for r in reader.read_range(T, 0, 0, 100)]
    assert offsets == [10, 11, 12]


# --- covered days are not written twice ------------------------------------


async def test_a_day_already_covered_in_the_sink_is_skipped_not_rewritten() -> None:
    sink, fake = _s3()
    kms = FakeKms()
    first = await HandlerTopicArchive(
        reader=source(), sink=sink, cipher=_kms(kms)
    ).handle(ModelTopicArchiveRequest(topics=[T]))
    assert first.verdict is EnumArchiveVerdict.VERIFIED
    before = dict(fake.store)
    # retention moved the log start past offset 10: day 1 now reads 11..11,
    # a sub-range of the archived 10..11, so nothing new is written
    later = source()
    later.low[T] = 11
    second = await HandlerTopicArchive(
        reader=later, sink=sink, cipher=_kms(kms)
    ).handle(ModelTopicArchiveRequest(topics=[T]))
    assert second.files == []
    assert len(second.already_archived) == 2
    assert second.verdict is EnumArchiveVerdict.NOTHING_TO_ARCHIVE
    assert fake.store == before


async def test_a_grown_open_day_is_written_again() -> None:
    sink = MemorySink()
    grown = source()
    grown.records[T].append(rec(T, 13, DAY2, b"4"))
    await HandlerTopicArchive(reader=source(), sink=sink, cipher=_kms()).handle(
        ModelTopicArchiveRequest(topics=[T])
    )
    result = await HandlerTopicArchive(reader=grown, sink=sink, cipher=_kms()).handle(
        ModelTopicArchiveRequest(topics=[T])
    )
    assert [
        (f.manifest.first_offset, f.manifest.last_offset) for f in result.files
    ] == [(12, 13)]
    assert len(result.already_archived) == 1


# --- replay from S3 --------------------------------------------------------


async def test_replay_from_s3_decrypts_through_kms_and_replays_each_offset_once() -> (
    None
):
    sink, _ = _s3()
    kms = FakeKms()
    await HandlerTopicArchive(reader=source(), sink=sink, cipher=_kms(kms)).handle(
        ModelTopicArchiveRequest(topics=[T])
    )
    grown = source()
    grown.records[T].append(rec(T, 13, DAY2, b"4"))
    await HandlerTopicArchive(reader=grown, sink=sink, cipher=_kms(kms)).handle(
        ModelTopicArchiveRequest(topics=[T])
    )
    writer = MemoryWriter()
    before = kms.decrypted
    result = await HandlerTopicArchiveReplay(
        sink=sink, cipher=_kms(kms), writer=writer
    ).handle(ModelTopicArchiveReplayRequest(manifest_prefix=f"{T}/"))
    assert result.refused_files == 0
    assert result.verified_files == 3  # day1, day2 12..12, day2 12..13
    offsets = [
        int(dict(h)["onex-archive-source-offset"] or b"-1")
        for (_t, _k, _v, h, _ts) in writer.sent
    ]
    assert offsets == [10, 11, 12, 13]
    assert result.replayed_records == 4
    assert kms.decrypted - before == 3


# --- entry point wiring ----------------------------------------------------


def test_entry_point_moves_a_staged_dir_to_s3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omnimarket.topic_archive.live_aws as live_aws
    from omnimarket.nodes.node_topic_archive_effect.__main__ import main
    from omnimarket.topic_archive.live import LocalDirArchiveSink

    base = tmp_path
    staging = LocalDirArchiveSink(base / "staged")
    asyncio.run(
        HandlerTopicArchive(
            reader=source(), sink=staging, cipher=NoArchiveCipher()
        ).handle(ModelTopicArchiveRequest(topics=[T]))
    )
    sink, fake = _s3()
    seen: dict[str, object] = {}

    def boundary(
        *, s3_uri: str, kms_key: str, profile: str | None = None
    ) -> tuple[S3ArchiveSink, KmsEnvelopeArchiveCipher]:
        seen.update(s3_uri=s3_uri, kms_key=kms_key, profile=profile)
        return sink, _kms()

    monkeypatch.setattr(live_aws, "aws_archive_boundary", boundary)
    args = [
        "--from-staging",
        str(base / "staged"),
        "--s3-uri",
        f"s3://{BUCKET}/topic-archive/",
        "--kms-key",
        "alias/test",
        "--report-dir",
        str(base / "runs"),
        "--topic",
        T,
    ]
    assert main(args) == 0
    assert seen == {
        "s3_uri": f"s3://{BUCKET}/topic-archive/",
        "kms_key": "alias/test",
        "profile": None,
    }
    assert sum(k.endswith(".kmsenv") for k in fake.store) == 2
    (report,) = (base / "runs").iterdir()
    assert json.loads(report.read_text())["verdict"] == "verified"
    # a second run finds every day already archived and still exits 0
    assert main(args) == 0


def test_entry_point_requires_a_kms_key_for_s3() -> None:
    from omnimarket.nodes.node_topic_archive_effect.__main__ import main

    with pytest.raises(SystemExit):
        main(["--bootstrap", "b:1", "--s3-uri", "s3://b/p/", "--report-dir", "r"])
