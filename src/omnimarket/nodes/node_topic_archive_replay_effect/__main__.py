# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Operator entry point for the topic archive replay node.

    uv run python -m omnimarket.nodes.node_topic_archive_replay_effect \\
        --bootstrap HOST:PORT --staging-dir DIR --prefix '<topic>/' \\
        [--replay-topic T] [--dry-run] [--age-recipient AGE1... --age-identity-env VAR]

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
from omnimarket.topic_archive.protocols import ProtocolArchiveCipher


async def _run(args: argparse.Namespace) -> ModelTopicArchiveReplayResult:
    cipher: ProtocolArchiveCipher = NoArchiveCipher()
    if args.age_identity_env:
        cipher = AgeArchiveCipher(
            recipient=args.age_recipient, identity=os.environ[args.age_identity_env]
        )
    writer = AiokafkaReplayWriter(args.bootstrap)
    try:
        return await HandlerTopicArchiveReplay(
            sink=LocalDirArchiveSink(Path(args.staging_dir)),
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
    p.add_argument("--staging-dir", required=True)
    p.add_argument("--prefix", required=True)
    p.add_argument("--replay-topic", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--age-recipient", default=None)
    p.add_argument("--age-identity-env", default=None)
    args = p.parse_args(argv)
    if args.age_identity_env and not args.age_recipient:
        p.error("--age-identity-env needs --age-recipient")
    result = asyncio.run(_run(args))
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    return 0 if result.refused_files == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
