# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI for database or offline work-ledger sequence gap reports."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from omnimarket.handlers.work_ledger_seq_gap import (
    PostgresWorkLedgerSeqReader,
    redact_database_error,
    report_from_seqs,
)
from omnimarket.models.model_work_ledger_seq_gap_report import (
    ModelWorkLedgerSeqGapReport,
)
from omnimarket.nodes.node_work_ledger_seq_gap_effect.handlers import (
    HandlerWorkLedgerSeqGap,
)
from omnimarket.nodes.node_work_ledger_seq_gap_effect.models import (
    ModelWorkLedgerSeqGapRequest,
)


def _utc_stamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _text(report: ModelWorkLedgerSeqGapReport) -> str:
    first = report.first_missing_seq
    return (
        f"ledger_seq gap check: exact={'yes' if report.exact else 'no'} "
        f"first_missing_seq={first if first is not None else 'none'} "
        f"missing={report.missing_count} duplicates={report.duplicate_count} "
        f"range={report.from_seq}..{report.to_seq if report.to_seq is not None else 'none'} "
        f"rows_with_seq={report.rows_with_seq} rows_without_seq={report.rows_without_seq}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn-env")
    parser.add_argument("--seqs-json", type=Path)
    parser.add_argument("--ledger-id", default="rolling-work-ledger")
    parser.add_argument("--from-seq", type=int, default=1)
    parser.add_argument("--utc-day")
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--expect-max-seq", type=int)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    args = parser.parse_args(argv)
    try:
        if not args.dsn_env and args.seqs_json is None:
            raise ValueError("--dsn-env is required unless --seqs-json is supplied")
        if args.utc_day and (args.since or args.until):
            raise ValueError("--utc-day cannot be combined with --since/--until")
        since = _utc_stamp(args.since) if args.since else None
        until = _utc_stamp(args.until) if args.until else None
        if args.utc_day:
            since = datetime.strptime(args.utc_day, "%Y-%m-%d").replace(tzinfo=UTC)
            until = since + timedelta(days=1) - timedelta(microseconds=1)
        request = ModelWorkLedgerSeqGapRequest(
            correlation_id=uuid4(),
            ledger_id=args.ledger_id,
            from_seq=args.from_seq,
            since=since,
            until=until,
            expected_max_seq=args.expect_max_seq,
        )
        if args.seqs_json is not None:
            seqs = json.loads(args.seqs_json.read_text())
            if not isinstance(seqs, list):
                raise ValueError("--seqs-json must contain a JSON list of integers")
            report = report_from_seqs(
                seqs,
                ledger_id=request.ledger_id,
                from_seq=request.from_seq,
                window_since=request.since,
                window_until=request.until,
                expected_max_seq=request.expected_max_seq,
            )
        else:
            report = HandlerWorkLedgerSeqGap(
                PostgresWorkLedgerSeqReader(dsn_env=args.dsn_env)
            ).handle(request)
        output = report.model_dump_json() if args.format == "json" else _text(report)
        sys.stdout.write(output + "\n")
        return 0 if report.exact else 1
    except Exception as exc:
        sys.stderr.write(redact_database_error(exc, args.dsn_env or "") + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
