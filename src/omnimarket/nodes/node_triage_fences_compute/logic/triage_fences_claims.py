# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLAIM / TERMINAL pairing and lane liveness of the triage fence derivation (OMN-17757, OMN-20682).

A CLAIM with no matching TERMINAL is live, exclusive scope while its lane has written any ledger
row inside the staleness window (OMN-17427); an older one is stale, reported and never fenced.
Moved from the deleted ``triage_fences`` module without a change of rule.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass, replace
from datetime import datetime

from ..models import LedgerRow, ModelTriageFencesClaim, OpenClaim
from .triage_fences_grammar import (
    EXPLICIT_BIND_RE,
    LEADING_TS_RE,
    TS,
    iso,
    parse_fields,
    parse_ts,
    sort_tickets,
)


@dataclass
class _LiveClaim:
    row: LedgerRow
    last_update_ts: datetime
    update_count: int = 0

    @property
    def lane_key(self) -> str | None:
        return self.row.lane.lower() if self.row.lane else None

    def absorb(self, row: LedgerRow) -> None:
        self.last_update_ts = max(self.last_update_ts, row.ts)
        self.update_count += 1

    def finalize(self, now: datetime) -> OpenClaim:
        age = (now - self.row.ts).total_seconds() / 3600.0
        return OpenClaim(
            lane=self.row.lane,
            tickets=self.row.tickets,
            related=self.row.related,
            claim_ts=self.row.ts,
            last_update_ts=self.last_update_ts,
            update_count=self.update_count,
            actor=self.row.actor,
            source=self.row.source,
            form=self.row.form,
            age_hours=round(age, 3),
            raw=self.row.raw,
        )


def _compatible(claim: _LiveClaim, terminal: LedgerRow) -> bool:
    """Does this TERMINAL close this CLAIM?

    Lane is the primary key. Where BOTH rows name tickets they must intersect,
    so a reused lane handle cannot close unrelated work. A ticket-less TERMINAL
    on a matching lane is a lane close -- absent tickets are not a mismatch.
    Where either row has no lane, only a ticket intersection can bind them.
    """
    claim_lane, term_lane = (
        claim.lane_key,
        (terminal.lane.lower() if terminal.lane else None),
    )
    if claim_lane and term_lane:
        if claim_lane != term_lane:
            return False
        if claim.row.tickets and terminal.tickets:
            return bool(claim.row.tickets & terminal.tickets)
        return True
    return bool(claim.row.tickets & terminal.tickets)


_KIND_ORDER = {"CLAIM": 0, "CLAIM_UPDATE": 1, "TERMINAL": 2}


def open_claims(rows: Iterable[LedgerRow], now: datetime) -> list[OpenClaim]:
    """CLAIM rows with no matching TERMINAL, oldest first."""
    ordered = sorted(rows, key=lambda r: (r.ts, _KIND_ORDER[r.kind], r.source))
    live: list[_LiveClaim] = []
    for row in ordered:
        if row.kind == "CLAIM_UPDATE":
            target = next(
                (c for c in reversed(live) if _compatible(c, row)),
                None,
            )
            if target is not None:
                target.absorb(row)
                continue
            # A chain whose opening CLAIM was archived (or never written) still
            # marks live scope -- treat the update as the claim.
            live.append(_LiveClaim(row, row.ts))
            continue
        if row.kind == "CLAIM":
            live.append(_LiveClaim(row, row.ts))
            continue

        # A lane's TERMINAL ends the lane: a child-ticket CLAIM the lane runner wrote for the same
        # lane, naming the carrier ticket in parent_ticket, is closed with it. The lane's TERMINAL
        # names the carrier ticket only, so no ticket intersection would ever pair the child
        # (OMN-24248: 37 of 39 dead claims on 2026-11-13).
        terminal_lane = row.lane.lower() if row.lane else None
        if terminal_lane and row.tickets:
            live[:] = [
                c
                for c in live
                if not (
                    c.lane_key == terminal_lane
                    and c.row.ts <= row.ts
                    and c.row.parent_tickets & row.tickets
                )
            ]
        candidates = [c for c in live if c.row.ts <= row.ts and _compatible(c, row)]
        if not candidates:
            continue
        chosen: _LiveClaim | None = None
        bind = EXPLICIT_BIND_RE.search(row.body) or EXPLICIT_BIND_RE.search(row.raw)
        if bind:
            bound = parse_ts(bind.group(1))
            chosen = next((c for c in candidates if c.row.ts == bound), None)
        if chosen is None:
            chosen = candidates[-1]  # LIFO: the most recent matching claim
        live.remove(chosen)
    return [c.finalize(now) for c in sorted(live, key=lambda c: c.row.ts)]


# --- lane liveness (OMN-17427) ---------------------------------------------

PIPE_LEADING_TS_RE = re.compile(rf"^\|\s*({TS})")


def lane_last_seen(text: str, rows: Iterable[LedgerRow] = ()) -> dict[str, datetime]:
    """The newest row each lane wrote, of ANY kind, keyed on the lowercased lane.

    A row belongs to the lane in its OWN first `lane=` field (the same
    first-occurrence rule as `parse_fields`), so a body quoting another lane's
    handle never keeps that lane alive. Parsed lifecycle rows add the lanes of
    the positional pipe forms, which carry no `lane=` key.
    """
    seen: dict[str, datetime] = {}

    def note(lane: str | None, ts: datetime | None) -> None:
        if not lane or ts is None:
            return
        key = lane.lower().rstrip("|,;.")
        if key and (key not in seen or ts > seen[key]):
            seen[key] = ts

    for line in text.splitlines():
        stripped = line.strip()
        match = LEADING_TS_RE.match(stripped) or PIPE_LEADING_TS_RE.match(stripped)
        if not match:
            continue
        lane = parse_fields(stripped).get("lane")
        note(lane, parse_ts(match.group(1)))
    for row in rows:
        note(row.lane, row.ts)
    return seen


def split_live_and_stale(
    claims: Iterable[OpenClaim],
    *,
    last_seen: dict[str, datetime],
    now: datetime,
    stale_after_hours: float,
    running_lanes: Collection[str] = (),
) -> tuple[list[OpenClaim], list[OpenClaim]]:
    """Partition open claims by whether their lane is live.

    A lane is live while its runner receipt reads running (``running_lanes``) or while it wrote
    any ledger row inside the window. A claim whose lane is neither is stale: reported, never
    fenced (OMN-24248).
    """
    running = {lane.lower() for lane in running_lanes}
    live: list[OpenClaim] = []
    stale: list[OpenClaim] = []
    for claim in claims:
        activity = claim.last_update_ts
        if claim.lane:
            activity = max(activity, last_seen.get(claim.lane.lower(), activity))
        idle_hours = (now - activity).total_seconds() / 3600.0
        if claim.lane and claim.lane.lower() in running:
            live.append(
                replace(claim, last_activity_ts=activity, live_basis="running-receipt")
            )
        elif idle_hours > stale_after_hours:
            stale.append(replace(claim, last_activity_ts=activity))
        else:
            live.append(
                replace(claim, last_activity_ts=activity, live_basis="recent-row")
            )
    return live, stale


def claim_model(claim: OpenClaim) -> ModelTriageFencesClaim:
    """The claim as the report carries it; ``last_activity_ts`` leads, as it always has."""
    activity = claim.last_activity_ts
    return ModelTriageFencesClaim(
        last_activity_ts=iso(activity) if activity is not None else "",
        lane=claim.lane,
        tickets=tuple(sort_tickets(claim.tickets)),
        related=tuple(sort_tickets(claim.related)),
        claim_ts=iso(claim.claim_ts),
        last_update_ts=iso(claim.last_update_ts),
        update_count=claim.update_count,
        age_hours=claim.age_hours,
        actor=claim.actor,
        source=claim.source,
        form=claim.form,
        live_basis=claim.live_basis,
    )
