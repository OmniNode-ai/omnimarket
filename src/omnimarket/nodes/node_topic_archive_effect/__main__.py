# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Operator entry point for the topic archive node.

    uv run python -m omnimarket.nodes.node_topic_archive_effect \\
        --bootstrap HOST:PORT --staging-dir DIR [--topic T ...] [--day YYYY-MM-DD ...] \\
        [--age-recipient AGE1...]

Broker auth is read by the shared runtime Kafka auth builder (the standard
KAFKA_* environment of the lane). Without --age-recipient the archive is staged
unencrypted in the local directory, which is owner-only; a sink that leaves the
host refuses that. Writes the full typed result to <staging-dir>/runs/ and a
per-file summary to stdout. Exit 0 when every file verified by readback (and by
source re-read where the offsets are still retained), 1 otherwise.
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
from omnimarket.topic_archive.protocols import ProtocolArchiveCipher

_OK = {EnumArchiveVerdict.VERIFIED, EnumArchiveVerdict.READBACK_ONLY}


async def _run(args: argparse.Namespace) -> ModelTopicArchiveResult:
    reader = AiokafkaTopicReader(args.bootstrap)
    cipher: ProtocolArchiveCipher = (
        AgeArchiveCipher(recipient=args.age_recipient)
        if args.age_recipient
        else NoArchiveCipher()
    )
    try:
        return await HandlerTopicArchive(
            reader=reader,
            sink=LocalDirArchiveSink(Path(args.staging_dir)),
            cipher=cipher,
        ).handle(
            ModelTopicArchiveRequest(
                topics=args.topic or None,
                days=[dt.date.fromisoformat(d) for d in args.day] if args.day else None,
                verify_against_source=not args.no_source_verify,
            )
        )
    finally:
        await reader.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="node_topic_archive_effect")
    p.add_argument("--bootstrap", required=True)
    p.add_argument("--staging-dir", required=True)
    p.add_argument("--topic", action="append", default=[])
    p.add_argument("--day", action="append", default=[])
    p.add_argument(
        "--age-recipient", default=None, help="age public recipient (not a secret)"
    )
    p.add_argument("--no-source-verify", action="store_true")
    args = p.parse_args(argv)
    result = asyncio.run(_run(args))
    runs = Path(args.staging_dir) / "runs"
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
                "sink": result.sink_location,
                "detail": result.detail,
            }
        )
        + "\n"
    )
    return 0 if result.verdict in _OK else 1


if __name__ == "__main__":
    sys.exit(main())
