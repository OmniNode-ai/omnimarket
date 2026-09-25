# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Operator entry point for the topic archive node.

    uv run python -m omnimarket.nodes.node_topic_archive_effect \\
        (--bootstrap HOST:PORT | --from-staging DIR) \\
        (--staging-dir DIR [--age-recipient AGE1...] \\
         | --s3-uri s3://BUCKET/PREFIX/ --kms-key KEY [--aws-profile P] --report-dir DIR) \\
        [--topic T ...] [--day YYYY-MM-DD ...]

Source: the broker (--bootstrap; auth from the shared runtime Kafka auth
builder over the lane's standard KAFKA_* environment), or a plaintext archive
staged by an earlier run (--from-staging), which moves those days to the sink
and verifies each against the staged records.

Sink: a local owner-only directory (the default binding; unencrypted unless
--age-recipient is given), or S3 with a per-object KMS data key (envelope
encryption, wrapped key in the object header) and SSE-KMS on the object; the
S3 sink leaves the host, so a plaintext cipher is refused before anything is
read. AWS credentials come from the process's credential chain (--aws-profile).

Writes the full typed result to <report-dir> (default <staging-dir>/runs/) and
a per-file summary to stdout. Exit 0 when every written file verified by
readback (and by source re-read where the offsets are still retained), or when
nothing was new because every day read was already archived; 1 otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from pathlib import Path

from omnimarket.nodes.node_topic_archive_effect.handlers.handler_topic_archive import (
    HandlerTopicArchive,
)
from omnimarket.topic_archive.live import (
    AgeArchiveCipher,
    AiokafkaTopicReader,
    LocalDirArchiveSink,
    NoArchiveCipher,
)
from omnimarket.topic_archive.models import (
    EnumArchiveVerdict,
    ModelTopicArchiveRequest,
    ModelTopicArchiveResult,
)
from omnimarket.topic_archive.protocols import (
    ProtocolArchiveCipher,
    ProtocolArchiveSink,
    ProtocolTopicReader,
)
from omnimarket.topic_archive.staged import StagedArchiveReader

_OK = {EnumArchiveVerdict.VERIFIED, EnumArchiveVerdict.READBACK_ONLY}


def _sink_and_cipher(
    args: argparse.Namespace,
) -> tuple[ProtocolArchiveSink, ProtocolArchiveCipher]:
    if args.s3_uri:
        from omnimarket.topic_archive.live_aws import aws_archive_boundary

        return aws_archive_boundary(
            s3_uri=args.s3_uri, kms_key=args.kms_key, profile=args.aws_profile
        )
    cipher: ProtocolArchiveCipher = (
        AgeArchiveCipher(recipient=args.age_recipient)
        if args.age_recipient
        else NoArchiveCipher()
    )
    return LocalDirArchiveSink(Path(args.staging_dir)), cipher


async def _run(args: argparse.Namespace) -> ModelTopicArchiveResult:
    sink, cipher = _sink_and_cipher(args)
    reader: ProtocolTopicReader
    broker: AiokafkaTopicReader | None = None
    if args.from_staging:
        reader = StagedArchiveReader(
            LocalDirArchiveSink(Path(args.from_staging)), NoArchiveCipher()
        )
    else:
        broker = reader = AiokafkaTopicReader(args.bootstrap)
    try:
        return await HandlerTopicArchive(
            reader=reader,
            sink=sink,
            cipher=cipher,
        ).handle(
            ModelTopicArchiveRequest(
                topics=args.topic or None,
                days=[dt.date.fromisoformat(d) for d in args.day] if args.day else None,
                verify_against_source=not args.no_source_verify,
            )
        )
    finally:
        if broker is not None:
            await broker.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="node_topic_archive_effect")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--bootstrap")
    src.add_argument(
        "--from-staging", help="a plaintext archive staged by an earlier run"
    )
    dst = p.add_mutually_exclusive_group(required=True)
    dst.add_argument("--staging-dir")
    dst.add_argument("--s3-uri", help="s3://BUCKET/PREFIX/")
    p.add_argument("--kms-key", help="KMS key id, ARN or alias (with --s3-uri)")
    p.add_argument("--aws-profile", default=None)
    p.add_argument("--report-dir", default=None)
    p.add_argument("--topic", action="append", default=[])
    p.add_argument("--day", action="append", default=[])
    p.add_argument(
        "--age-recipient", default=None, help="age public recipient (not a secret)"
    )
    p.add_argument("--no-source-verify", action="store_true")
    args = p.parse_args(argv)
    if args.s3_uri and not (args.kms_key and args.report_dir):
        p.error("--s3-uri needs --kms-key and --report-dir")
    if args.s3_uri and args.age_recipient:
        p.error("--age-recipient applies to --staging-dir only")
    result = asyncio.run(_run(args))
    runs = Path(args.report_dir) if args.report_dir else Path(args.staging_dir) / "runs"
    runs.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    (runs / f"archive-{stamp}.json").write_text(
        result.model_dump_json(indent=2), encoding="utf-8"
    )
    for f in result.files:
        m = f.manifest
        sys.stdout.write(
            json.dumps(
                {
                    "topic": m.topic,
                    "day": m.day.isoformat(),
                    "offsets": [m.first_offset, m.last_offset],
                    "count": m.record_count,
                    "bytes": m.object_bytes,
                    "readback": f.readback_verified,
                    "source": f.source_verified,
                    "truncated": m.day_start_truncated,
                    "open": m.day_open,
                }
            )
            + "\n"
        )
    sys.stdout.write(
        json.dumps(
            {
                "verdict": result.verdict,
                "files": len(result.files),
                "records": result.record_count,
                "already_archived": len(result.already_archived),
                "sink": result.sink_location,
                "detail": result.detail,
            }
        )
        + "\n"
    )
    ok = result.verdict in _OK or (
        result.verdict is EnumArchiveVerdict.NOTHING_TO_ARCHIVE
        and bool(result.already_archived)
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
