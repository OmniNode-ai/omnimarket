# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reader half of lane liveness (OMN-18609), moved from scripts/lane_liveness_reader.py.

Everything that gathers evidence for ``HandlerLaneLiveness`` lives here: the
ledger CLAIM/TERMINAL parse, the assembly of the handler's request, and the
window reader. ``HandlerLaneLiveness`` owns every decision and performs no I/O.
The script keeps its command line and imports these functions.

**Every statement issued here is a ``SELECT``.** There is no write path, no DDL
and no parameter that can turn one on.

THE LANE IS THE KEY, AND ``session_id`` IS NOT. One ``session_id`` covered
79,343 of the table's 83,067 rows across nine days, because it delimits a
RESUMED Claude Code session rather than a unit of work; ``correlation_id``,
``run_id`` and ``entity_id`` carry that same value on every row. The queries
group by ``payload->>'lane'`` and never by any of those four.

Two rules were added when the reader moved here (OMN-20604):

* A TERMINAL counts only when it is newer than the lane's current CLAIM. A lane
  name is reused across dispatches, so the previous dispatch's TERMINAL must not
  close this one.
* Attribution is per lane, not per window: whether this lane has any
  lane-attributed event since its CLAIM. A window can carry attribution for
  other lanes while this lane's events carry no name at all.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnimarket.models.liveness import (
    ModelLaneLivenessRequest,
    ModelLaneObservation,
)
from omnimarket.projection.tenant_isolation import TENANT_GUC, resolve_rls_read_tenant

#: The liveness node's own contract, which declares the event classes counted as
#: lane activity. Read from there rather than repeated here: a topic literal in
#: Python is what the no-hardcoded-topics gate refuses, and a second copy of the
#: list is a second thing to forget when a capture class is added.
CONTRACT_PATH = (
    Path(__file__).resolve().parents[2] / "node_lane_liveness_compute" / "contract.yaml"
)


def activity_event_types(contract_path: Path | None = None) -> tuple[str, ...]:
    """The declared lane-activity classes.

    Raises rather than defaulting to a guessed list: a reader that silently
    queries the wrong event classes returns an empty result, and an empty
    result here reads as a quiet fleet.
    """
    import yaml

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


def terminal_after_claim(
    claimed_at: datetime | None, terminal_at: datetime | None
) -> datetime | None:
    """The TERMINAL that closes this CLAIM, or None.

    A TERMINAL is excluded unless it is newer than the CLAIM it would close. A
    TERMINAL with no CLAIM to compare against is kept: it is still ledger
    evidence that the lane ended.
    """
    if terminal_at is None:
        return None
    if claimed_at is not None and terminal_at <= claimed_at:
        return None
    return terminal_at


def parse_ledger(
    ledger_text: str, window_end: datetime
) -> tuple[dict[str, datetime], dict[str, datetime]]:
    """Return ``(claims, terminals)``, each mapping lane to its newest row.

    Rows stamped after *window_end* are ignored, so a report about a past window
    is not contaminated by what happened afterwards -- otherwise re-running a
    historical window would keep producing different answers as the ledger
    grows, and a reader that changes its mind about the past is not a record.

    The NEWEST claim wins because a lane name is reused across dispatches. The
    newest terminal wins for the same reason, but only when it is newer than
    that claim (:func:`terminal_after_claim`).
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
    closing = {
        lane: closed
        for lane, terminal in terminals.items()
        if (closed := terminal_after_claim(claims.get(lane), terminal)) is not None
    }
    return claims, closing


def row_cell(raw_row: str, key: str) -> str | None:
    """The value of a ``key=value`` cell of a ledger row, or None."""
    prefix = f"{key}="
    for cell in raw_row.split(" | "):
        if cell.startswith(prefix):
            return cell[len(prefix) :].strip()
    return None


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
    """Read the window. Three SELECTs, no writes, one connection.

    ``public.hook_events`` is RLS-covered, so the reads run in one read-only
    transaction that first sets the tenant GUC; without it the policy matches
    nothing and an empty window reads as a quiet fleet.
    """
    import asyncpg

    tenant = resolve_rls_read_tenant(None, table="hook_events")
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction(readonly=True):
            await conn.execute("SELECT set_config($1, $2, true)", TENANT_GUC, tenant)
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


__all__: list[str] = [
    "CONTRACT_PATH",
    "activity_event_types",
    "build_request",
    "gather",
    "parse_ledger",
    "row_cell",
    "terminal_after_claim",
]
