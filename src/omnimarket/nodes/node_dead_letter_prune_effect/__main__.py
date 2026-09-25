# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Operator entry point for the dead-letter prune node (OMN-17001).

    uv run python -m omnimarket.nodes.node_dead_letter_prune_effect \\
        (--dry-run | --archive-dir DIR | --s3-uri s3://BUCKET/PREFIX/ --kms-key KEY) \\
        --report-dir DIR [--retention-days N] [--as-of ISO8601] [--max-days N] \\
        [--aws-profile P]

The database is the one the contract's dsn_env names (OMNIBASE_INFRA_DB_URL);
the process fails fast when it is unset. Nothing is ever passed on the command
line that is a credential.

Sink: a local owner-only directory, or S3 with a per-object KMS data key
(envelope encryption, wrapped key in the object header) and SSE-KMS on the
object; the S3 sink leaves the host, so a plaintext cipher is refused before
anything is read. --dry-run lists eligible days and counts and touches nothing.

Writes the full typed result to <report-dir> and one summary line per day to
stdout. Exit 0 on PRUNED, DRY_RUN or NOTHING_TO_PRUNE; 1 otherwise.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

from omnimarket.nodes.node_dead_letter_prune_effect.handlers.handler_dead_letter_prune import (
    HandlerDeadLetterPrune,
    contract_config,
)
from omnimarket.nodes.node_dead_letter_prune_effect.handlers.postgres_dead_letter_store import (
    PostgresDeadLetterStore,
)
from omnimarket.nodes.node_dead_letter_prune_effect.models import (
    EnumDeadLetterPruneVerdict,
    ModelDeadLetterPruneRequest,
)
from omnimarket.topic_archive.live import LocalDirArchiveSink, NoArchiveCipher
from omnimarket.topic_archive.protocols import (
    ProtocolArchiveCipher,
    ProtocolArchiveSink,
)

_OK = {
    EnumDeadLetterPruneVerdict.PRUNED,
    EnumDeadLetterPruneVerdict.DRY_RUN,
    EnumDeadLetterPruneVerdict.NOTHING_TO_PRUNE,
}


def _sink_and_cipher(
    args: argparse.Namespace,
) -> tuple[ProtocolArchiveSink, ProtocolArchiveCipher]:
    if args.s3_uri:
        from omnimarket.topic_archive.live_aws import aws_archive_boundary

        return aws_archive_boundary(
            s3_uri=args.s3_uri, kms_key=args.kms_key, profile=args.aws_profile
        )
    root = Path(args.archive_dir or args.report_dir) / "archive"
    return LocalDirArchiveSink(root), NoArchiveCipher()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="node_dead_letter_prune_effect")
    dst = p.add_mutually_exclusive_group(required=True)
    dst.add_argument("--dry-run", action="store_true")
    dst.add_argument("--archive-dir")
    dst.add_argument("--s3-uri", help="s3://BUCKET/PREFIX/")
    p.add_argument("--kms-key", help="KMS key id, ARN or alias (with --s3-uri)")
    p.add_argument("--aws-profile", default=None)
    p.add_argument("--report-dir", required=True)
    p.add_argument("--retention-days", type=int, default=None)
    p.add_argument("--as-of", default=None)
    p.add_argument("--max-days", type=int, default=None)
    args = p.parse_args(argv)
    if args.s3_uri and not args.kms_key:
        p.error("--s3-uri needs --kms-key")

    cfg = contract_config()
    store = PostgresDeadLetterStore(os.environ[cfg.dsn_env])
    sink, cipher = _sink_and_cipher(args)
    try:
        result = HandlerDeadLetterPrune(store=store, sink=sink, cipher=cipher).handle(
            ModelDeadLetterPruneRequest(
                retention_days=args.retention_days,
                as_of=dt.datetime.fromisoformat(args.as_of) if args.as_of else None,
                dry_run=args.dry_run,
                max_days=args.max_days,
            )
        )
    finally:
        store.close()

    runs = Path(args.report_dir)
    runs.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    (runs / f"dead-letter-prune-{stamp}.json").write_text(
        result.model_dump_json(indent=2), encoding="utf-8"
    )
    for d in result.days:
        sys.stdout.write(
            json.dumps(
                {
                    "topic": d.topic,
                    "partition": d.partition,
                    "day": d.day.isoformat(),
                    "eligible": d.rows_eligible,
                    "archived": d.rows_archived,
                    "pruned": d.rows_pruned,
                    "written": len(d.manifests),
                    "reused": len(d.reused_manifests),
                    "status": d.status,
                    "detail": d.detail,
                }
            )
            + "\n"
        )
    sys.stdout.write(
        json.dumps(
            {
                "verdict": result.verdict,
                "cutoff_day": result.cutoff_day.isoformat(),
                "eligible": result.rows_eligible,
                "pruned": result.rows_pruned,
                "sink": result.sink_location,
                "detail": result.detail,
            }
        )
        + "\n"
    )
    return 0 if result.verdict in _OK else 1


if __name__ == "__main__":
    raise SystemExit(main())
