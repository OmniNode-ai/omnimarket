# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Operator entry point for the topic archive replay node.

    uv run python -m omnimarket.nodes.node_topic_archive_replay_effect \\
        --bootstrap HOST:PORT --prefix '<topic>/' [--replay-topic T] [--dry-run] \\
        (--staging-dir DIR [--age-recipient AGE1... --age-identity-env VAR]
         | --s3-uri s3://BUCKET/PREFIX/ --kms-key KEY [--aws-profile P])

With --s3-uri each object's wrapped data key is unwrapped by KMS Decrypt under
the process's AWS credentials; no key material is read from anywhere else.

The age identity is read from the environment variable NAMED by
--age-identity-env (populated from the secret store), never from the command
line, so it does not appear in a process listing. Exit 0 when no file was
refused.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from omnimarket.nodes.node_topic_archive_replay_effect.handlers.handler_topic_archive_replay import (
    HandlerTopicArchiveReplay,
)
from omnimarket.topic_archive.live import (
    AgeArchiveCipher,
    AiokafkaReplayWriter,
    LocalDirArchiveSink,
    NoArchiveCipher,
)
from omnimarket.topic_archive.models import (
    ModelTopicArchiveReplayRequest,
    ModelTopicArchiveReplayResult,
)
from omnimarket.topic_archive.protocols import (
    ProtocolArchiveCipher,
    ProtocolArchiveSink,
)


async def _run(args: argparse.Namespace) -> ModelTopicArchiveReplayResult:
    cipher: ProtocolArchiveCipher = NoArchiveCipher()
    sink: ProtocolArchiveSink
    if args.s3_uri:
        from omnimarket.topic_archive.live_aws import aws_archive_boundary

        sink, cipher = aws_archive_boundary(
            s3_uri=args.s3_uri, kms_key=args.kms_key, profile=args.aws_profile
        )
    else:
        sink = LocalDirArchiveSink(Path(args.staging_dir))
    if args.age_identity_env:
        cipher = AgeArchiveCipher(
            recipient=args.age_recipient, identity=os.environ[args.age_identity_env]
        )
    writer = AiokafkaReplayWriter(args.bootstrap)
    try:
        return await HandlerTopicArchiveReplay(
            sink=sink,
            cipher=cipher,
            writer=writer,
        ).handle(
            ModelTopicArchiveReplayRequest(
                manifest_prefix=args.prefix,
                replay_topic=args.replay_topic,
                dry_run=args.dry_run,
            )
        )
    finally:
        await writer.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="node_topic_archive_replay_effect")
    p.add_argument("--bootstrap", required=True)
    dst = p.add_mutually_exclusive_group(required=True)
    dst.add_argument("--staging-dir")
    dst.add_argument("--s3-uri", help="s3://BUCKET/PREFIX/")
    p.add_argument("--kms-key", help="KMS key id, ARN or alias (with --s3-uri)")
    p.add_argument("--aws-profile", default=None)
    p.add_argument("--prefix", required=True)
    p.add_argument("--replay-topic", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--age-recipient", default=None)
    p.add_argument("--age-identity-env", default=None)
    args = p.parse_args(argv)
    if args.age_identity_env and not args.age_recipient:
        p.error("--age-identity-env needs --age-recipient")
    if args.s3_uri and not args.kms_key:
        p.error("--s3-uri needs --kms-key")
    if args.s3_uri and args.age_identity_env:
        p.error("--age-identity-env applies to --staging-dir only")
    result = asyncio.run(_run(args))
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    return 0 if result.refused_files == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
