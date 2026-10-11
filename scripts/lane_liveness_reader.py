#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command line of the read-only gatherer for the cloud hook ledger (OMN-18609).

The gatherer itself -- the ``SELECT`` against ``public.hook_events``, the parse of
the hand ledger's CLAIM/TERMINAL rows and the assembly of a
``ModelLaneLivenessRequest`` -- moved to
``omnimarket.nodes.node_lab_job_check_effect.handlers.lane_evidence`` (OMN-20604),
where the lab job check effect reuses it. This script only reads its arguments,
calls that module and hands the request to ``HandlerLaneLiveness``, which owns
every decision and performs no I/O at all. That split is deliberate: the verdict
rules have to be testable against a window that has already happened, with no
cluster and no database.

**Every statement it issues is a ``SELECT``.** There is no write path here, no
DDL, and no parameter that can turn one on.

THE LANE IS THE KEY, AND ``session_id`` IS NOT. One ``session_id`` covered
79,343 of the table's 83,067 rows across nine days, because it delimits a
RESUMED Claude Code session rather than a unit of work; ``correlation_id``,
``run_id`` and ``entity_id`` carry that same value on every row. The queries
below group by ``payload->>'lane'`` and never by any of those four.

READING A HISTORICAL WINDOW. Events written before the emitter carried a lane
name have no ``lane`` key at all. The gatherer detects that and sets
``lane_attribution_available=False``, which is what makes the handler report
UNOBSERVABLE instead of inventing a per-lane reading the data cannot support.

Usage (from inside the cluster, where the database is reachable):

    python scripts/lane_liveness_reader.py --window-minutes 60
    python scripts/lane_liveness_reader.py \\
        --window-start 2026-09-17T15:21:00Z --window-end 2026-09-17T16:01:00Z
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from omnimarket.adapters.asyncpg_adapter import DB_URL_ENV as DSN_ENV
from omnimarket.nodes.node_lab_job_check_effect.handlers.lane_evidence import (
    CONTRACT_PATH,
    activity_event_types,
    build_request,
    gather,
    parse_ledger,
)
from omnimarket.nodes.node_lane_liveness_compute.handlers.handler_lane_liveness import (
    HandlerLaneLiveness,
)
from omnimarket.nodes.node_lane_liveness_compute.models.model_lane_liveness import (
    ModelLaneLivenessReport,
)

__all__ = [
    "CONTRACT_PATH",
    "DSN_ENV",
    "activity_event_types",
    "build_request",
    "gather",
    "parse_ledger",
]


def _parse_instant(raw: str) -> datetime:
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def render(report: ModelLaneLivenessReport) -> str:
    """The durable JSON a scheduled job persists and a board renders."""
    return json.dumps(
        {
            "ticket": "OMN-18609",
            "window_start": report.window_start.isoformat(),
            "window_end": report.window_end.isoformat(),
            "relay_state": report.relay_state.value,
            "relay_last_event_at": (
                report.relay_last_event_at.isoformat()
                if report.relay_last_event_at
                else None
            ),
            "relay_event_count": report.relay_event_count,
            "relay_silent_seconds": report.relay_silent_seconds,
            "lane_attribution_available": report.lane_attribution_available,
            "counts": report.counts(),
            "verdicts": [
                {
                    "lane": v.lane,
                    "verdict": v.verdict.value,
                    "evidence_basis": v.evidence_basis.value,
                    "reason": v.reason,
                    "last_hook_event_at": (
                        v.last_hook_event_at.isoformat()
                        if v.last_hook_event_at
                        else None
                    ),
                    "hook_event_count": v.hook_event_count,
                    "silent_seconds": v.silent_seconds,
                }
                for v in report.verdicts
            ],
        },
        indent=2,
        sort_keys=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-minutes", type=int, default=60)
    parser.add_argument("--window-start", default=None)
    parser.add_argument("--window-end", default=None)
    parser.add_argument("--silence-threshold-seconds", type=int, default=900)
    parser.add_argument("--relay-silence-threshold-seconds", type=int, default=300)
    parser.add_argument(
        "--ledger",
        default=None,
        help="Path to the hand ledger; omit to read only the hook surface.",
    )
    parser.add_argument("--out", default=None, help="Write the JSON here too.")
    args = parser.parse_args(argv)

    window_end = (
        _parse_instant(args.window_end) if args.window_end else datetime.now(UTC)
    )
    window_start = (
        _parse_instant(args.window_start)
        if args.window_start
        else window_end - timedelta(minutes=args.window_minutes)
    )

    dsn = os.environ.get(DSN_ENV)
    if not dsn:
        # Fail loudly rather than defaulting: a reader that silently points at
        # the wrong database reports a healthy fleet from an empty table.
        print(f"lane_liveness_reader: {DSN_ENV} is not set", file=sys.stderr)
        return 2

    lane_rows, relay_last, relay_count, attributed = asyncio.run(
        gather(dsn, window_start, window_end)
    )

    claims: dict[str, datetime] = {}
    terminals: dict[str, datetime] = {}
    if args.ledger:
        claims, terminals = parse_ledger(
            Path(args.ledger).read_text(encoding="utf-8", errors="replace"),
            window_end,
        )

    report = HandlerLaneLiveness().handle(
        build_request(
            window_start=window_start,
            window_end=window_end,
            lane_rows=lane_rows,
            relay_last_event_at=relay_last,
            relay_event_count=relay_count,
            attributed_event_count=attributed,
            claims=claims,
            terminals=terminals,
            silence_threshold_seconds=args.silence_threshold_seconds,
            relay_silence_threshold_seconds=args.relay_silence_threshold_seconds,
        )
    )

    rendered = render(report)
    print(rendered)
    if args.out:
        Path(args.out).write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
