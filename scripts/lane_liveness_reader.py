#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only gatherer for the cloud hook ledger (OMN-18609).

This is the I/O half of the first reader. It performs exactly two reads --
``SELECT`` against ``public.hook_events`` and a parse of the hand ledger's
CLAIM/TERMINAL rows -- assembles them into a ``ModelLaneLivenessRequest``, and
hands that to ``HandlerLaneLiveness``, which owns every decision and performs no
I/O at all. That split is deliberate: the verdict rules have to be testable
against a window that has already happened, with no cluster and no database.

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
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from omnimarket.nodes.node_lane_liveness_compute.handlers.handler_lane_liveness import (
    HandlerLaneLiveness,
)
from omnimarket.nodes.node_lane_liveness_compute.models.model_lane_liveness import (
    ModelLaneLivenessReport,
    ModelLaneLivenessRequest,
    ModelLaneObservation,
)

#: The environment variable the onex-dev projection workload already carries.
DSN_ENV = "OMNIDASH_ANALYTICS_DB_URL"

#: The node's own contract, which declares the event classes counted as lane
#: activity. Read from there rather than repeated here: a topic literal in
#: Python is what the no-hardcoded-topics gate refuses, and a second copy of the
#: list is a second thing to forget when a capture class is added.
CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_lane_liveness_compute"
    / "contract.yaml"
)


def activity_event_types(contract_path: Path | None = None) -> tuple[str, ...]:
    """The declared lane-activity classes.

    Raises rather than defaulting to a guessed list: a reader that silently
    queries the wrong event classes returns an empty result, and an empty
    result here reads as a quiet fleet.
    """
    import yaml  # noqa: PLC0415 -- not needed to import this module

    contract = yaml.safe_load(
        (contract_path or CONTRACT_PATH).read_text(encoding="utf-8")
    )
    declared = contract["lane_activity_event_types"]
    if not declared:
        raise ValueError(
            "lane_activity_event_types is empty in the node contract; refusing "
            "to query with no event classes, which would return a false zero"
        )
    return tuple(str(entry) for entry in declared)

#: A ledger row leads with its timestamp and names its lane in a `lane=` field.
_ROW = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s*\|\s*"
    r"(?P<kind>CLAIM|TERMINAL)\b.*?\blane=(?P<lane>[A-Za-z0-9][A-Za-z0-9._-]*)"
)


def parse_ledger(
    ledger_text: str, window_end: datetime
) -> tuple[dict[str, datetime], dict[str, datetime]]:
    """Return ``(claims, terminals)``, each mapping lane to its newest row.

    Rows stamped after *window_end* are ignored, so a report about a past window
    is not contaminated by what happened afterwards -- otherwise re-running a
    historical window would keep producing different answers as the ledger
    grows, and a reader that changes its mind about the past is not a record.

    The NEWEST claim wins because a lane name is reused across dispatches; the
    newest terminal wins for the same reason.
    """
    claims: dict[str, datetime] = {}
    terminals: dict[str, datetime] = {}
    for line in ledger_text.splitlines():
        match = _ROW.match(line.strip())
        if match is None:
            continue
        stamped = datetime.strptime(match.group("ts"), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
        if stamped > window_end:
            continue
        target = claims if match.group("kind") == "CLAIM" else terminals
        lane = match.group("lane")
        if lane not in target or stamped > target[lane]:
            target[lane] = stamped
    return claims, terminals


def build_request(
    *,
    window_start: datetime,
    window_end: datetime,
    lane_rows: list[dict[str, Any]],
    relay_last_event_at: datetime | None,
    relay_event_count: int,
    attributed_event_count: int,
    claims: dict[str, datetime],
    terminals: dict[str, datetime],
    silence_threshold_seconds: int,
    relay_silence_threshold_seconds: int,
) -> ModelLaneLivenessRequest:
    """Assemble the handler's input from both surfaces.

    The observation set is the UNION of lanes seen on the wire and lanes named
    by a ledger row. A lane that only appears in the ledger is exactly the case
    drop detection exists for, so building the set from the wire alone would
    make the detector blind to its own subject.
    """
    activity = {
        str(row["lane"]): (row["last_event_at"], int(row["event_count"]))
        for row in lane_rows
        if row.get("lane")
    }
    lanes = sorted(set(activity) | set(claims) | set(terminals))
    observations = tuple(
        ModelLaneObservation(
            lane=lane,
            claimed_at=claims.get(lane),
            terminal_at=terminals.get(lane),
            last_hook_event_at=activity.get(lane, (None, 0))[0],
            hook_event_count=activity.get(lane, (None, 0))[1],
        )
        for lane in lanes
    )
    return ModelLaneLivenessRequest(
        window_start=window_start,
        window_end=window_end,
        observations=observations,
        relay_last_event_at=relay_last_event_at,
        relay_event_count=relay_event_count,
        # False unless at least one event in the window actually carried a lane.
        # A window of pre-emitter rows must not be read by lane at any
        # threshold, and this flag is how the handler is told so.
        lane_attribution_available=attributed_event_count > 0,
        silence_threshold_seconds=silence_threshold_seconds,
        relay_silence_threshold_seconds=relay_silence_threshold_seconds,
    )


async def gather(
    dsn: str, window_start: datetime, window_end: datetime
) -> tuple[list[dict[str, Any]], datetime | None, int, int]:
    """Read the window. Three SELECTs, no writes, one connection."""
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        lane_rows = [
            dict(row)
            for row in await conn.fetch(
                """
                SELECT payload->>'lane'      AS lane,
                       MAX(occurred_at)      AS last_event_at,
                       COUNT(*)              AS event_count
                  FROM public.hook_events
                 WHERE occurred_at >= $1 AND occurred_at < $2
                   AND event_type = ANY($3::text[])
                   AND COALESCE(payload->>'lane', '') <> ''
                 GROUP BY 1
                """,
                window_start,
                window_end,
                list(activity_event_types()),
            )
        ]
        relay = await conn.fetchrow(
            """
            SELECT MAX(occurred_at) AS last_event_at, COUNT(*) AS event_count
              FROM public.hook_events
             WHERE occurred_at >= $1 AND occurred_at < $2
            """,
            window_start,
            window_end,
        )
        attributed = await conn.fetchval(
            """
            SELECT COUNT(*)
              FROM public.hook_events
             WHERE occurred_at >= $1 AND occurred_at < $2
               AND COALESCE(payload->>'lane', '') <> ''
            """,
            window_start,
            window_end,
        )
    finally:
        await conn.close()
    return lane_rows, relay["last_event_at"], int(relay["event_count"]), int(attributed)


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
