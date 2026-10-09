# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLAIM/TERMINAL pairing of the reconciler (OMN-17466, moved by OMN-20677)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import datetime

from .ledger_rows import (
    DUPLICATE_WINDOW,
    EXPLICIT_BIND_RE,
    FULL_TOKEN_RE,
    LANE_REF_RE,
    LINE_REF_RE,
    MIN_SCOPE_KEY_CHARS,
    SCOPE_NOISE_CELL_RE,
    TS_REF_RE,
    Row,
    normalize_lane,
    parse_ts,
)


def _before(claim: Row, closer: Row) -> bool:
    if claim is closer:
        return False
    if claim.seq != closer.seq:
        return claim.seq < closer.seq
    return claim.ts <= closer.ts


def _related(claim: Row, closer: Row) -> bool:
    if claim.lane and closer.lane and claim.lane == closer.lane:
        return True
    return bool(claim.tickets & closer.tickets)


class ClaimIndex:
    """Resolves explicit close references to the CLAIM rows they name."""

    def __init__(self, claims: list[Row]) -> None:
        self.by_digest: dict[str, list[Row]] = {}
        self.by_line: dict[tuple[str, int], Row] = {}
        self.by_ts: dict[datetime, list[Row]] = {}
        self.by_minute: dict[datetime, list[Row]] = {}
        self.by_lane: dict[str, list[Row]] = {}
        digest = claim_digest_function()
        for claim in claims:
            self.by_digest.setdefault(digest(claim.raw), []).append(claim)
            self.by_line[(claim.file, claim.lineno)] = claim
            self.by_ts.setdefault(claim.ts, []).append(claim)
            self.by_minute.setdefault(claim.ts.replace(second=0), []).append(claim)
            if claim.lane:
                self.by_lane.setdefault(claim.lane, []).append(claim)

    def resolve(self, closer: Row, closed: set[int]) -> list[Row]:
        bound: list[Row] = []
        for ref in closer.refs:
            for claim in self._resolve_one(ref, closer, closed):
                if all(claim is not b for b in bound):
                    bound.append(claim)
        return bound

    def _resolve_one(self, ref: str, closer: Row, closed: set[int]) -> list[Row]:
        # 1. A full LCT1 token: the row digest is exact and survives a roll.
        hits = [
            claim
            for token in FULL_TOKEN_RE.finditer(ref)
            for claim in self.by_digest.get(token.group(1), [])
            if _before(claim, closer)
        ]
        if hits:
            return hits
        # 2. lane=<lane>: that lane's latest open claim, or the one whose
        #    timestamp the reference also quotes.
        lane_ref = LANE_REF_RE.match(ref)
        if lane_ref:
            lane = normalize_lane(lane_ref.group(1))
            candidates = [c for c in self.by_lane.get(lane, []) if _before(c, closer)]
            hinted = {parse_ts(t) for t in TS_REF_RE.findall(ref)}
            exact = [c for c in candidates if c.ts in hinted]
            still_open = [c for c in candidates if id(c) not in closed]
            pick = exact or still_open or candidates
            if pick:
                return [pick[-1]]
        # 3. A line number, in the closer's own file only (a roll renumbers
        #    lines, so a cited line may now hold some other claim): bind only
        #    a claim of the same lane or a shared ticket.
        hits = []
        for line_ref in LINE_REF_RE.finditer(ref):
            lineno = int(line_ref.group(1) or line_ref.group(2))
            claim = self.by_line.get((closer.file, lineno))
            if claim is not None and _before(claim, closer) and _related(claim, closer):
                hits.append(claim)
        if hits:
            return hits
        # 4. The claim's own timestamp.
        for raw_ts in TS_REF_RE.findall(ref):
            when = parse_ts(raw_ts)
            if when is None:
                continue
            table = self.by_ts if raw_ts.count(":") == 2 else self.by_minute
            candidates = [c for c in table.get(when, []) if _before(c, closer)]
            related = [c for c in candidates if _related(c, closer)]
            hits.extend(related or (candidates if len(candidates) == 1 else []))
        return hits


_LEADING_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\s*")
_BULLET = re.compile(r"^[-*]\s+")
_LEADING_CELL_STAMP = re.compile(r"^\|\s*\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\s*(?=\|)")
_LEADING_BOLD_STAMP = re.compile(r"^\*\*\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\*\*\s*")


def _normalize_line(line: str) -> str:
    """One line with its bullet and leading self-stamp removed: a retry whose stamp
    shifted still digests the same."""
    stripped = _BULLET.sub("", line.strip(), count=1)
    stripped = _LEADING_STAMP.sub("", stripped, count=1)
    stripped = _LEADING_BOLD_STAMP.sub("", stripped, count=1)
    stripped = _LEADING_CELL_STAMP.sub("|", stripped, count=1)
    return stripped.strip()


def claim_row_digest(row: str) -> str:
    """The 12-hex digest an LCT1 claim token carries, computed exactly as the ledger
    writer mints it, so a token is matched by the normalization that made it."""
    lines = [n for line in row.splitlines() if (n := _normalize_line(line))]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:12]


def claim_digest_function() -> Callable[[str], str]:
    return claim_row_digest


def pair_rows(rows: list[Row], bindings: dict[int, Row] | None = None) -> list[Row]:
    """Return the CLAIM rows left open after applying every close.

    Pass 1 binds explicit references (`closes-CLAIM=` / `supersedes-claim=`,
    carried by any row class, a CLAIM that supersedes an earlier one
    included). Pass 2 binds by PR within a lane: a TERMINAL naming PRs closes
    EVERY earlier open CLAIM of its lane taken for one of those PRs, whatever
    their tickets or typed timestamps say — lanes that claim per PR and close
    with one TERMINAL per PR list were otherwise mispaired by LIFO. Pass 3
    pairs the TERMINAL/CLOSEOUT rows still unbound by lane, LIFO, never
    across disjoint `pr=` fields; a TERMINAL whose PRs were read from its prose
    takes part in pass 3 even after it matched in pass 2. A REF row never
    closes anything by lane.
    `bindings`, when given, is filled with id(claim) -> the row that closed it."""
    if bindings is None:
        bindings = {}
    claims = [r for r in rows if r.kind == "CLAIM"]
    closed: set[int] = set()
    index = ClaimIndex(claims) if any(r.refs for r in rows) else None
    lane_closers: list[Row] = []
    for row in sorted(rows, key=lambda r: r.seq):
        bound = index.resolve(row, closed) if (index and row.refs) else []
        for claim in bound:
            closed.add(id(claim))
            bindings.setdefault(id(claim), row)
        if not bound and row.kind == "TERMINAL":
            lane_closers.append(row)
    by_lane: dict[str, list[Row]] = {}
    for claim in claims:
        if claim.lane and claim.prs:
            by_lane.setdefault(claim.lane, []).append(claim)
    lifo_closers: list[Row] = []
    for row in sorted(lane_closers, key=lambda r: r.seq):
        matched = [
            c
            for c in by_lane.get(row.lane or "", [])
            if row.prs and id(c) not in closed and c.prs & row.prs and _before(c, row)
        ]
        for claim in matched:
            closed.add(id(claim))
            bindings.setdefault(id(claim), row)
        if not (matched and row.pr_field):
            lifo_closers.append(row)
    pending = [c for c in claims if id(c) not in closed] + lifo_closers
    ordered = sorted(pending, key=lambda r: (r.ts, 0 if r.kind == "CLAIM" else 1))
    open_claims: list[Row] = []
    for row in ordered:
        if row.kind == "CLAIM":
            open_claims.append(row)
            continue
        candidates = [
            c
            for c in open_claims
            if c.ts <= row.ts
            and _compatible(c, row)
            and not (c.prs and row.pr_field and not (c.prs & row.prs))
        ]
        if not candidates:
            continue
        bind = EXPLICIT_BIND_RE.search(row.body)
        chosen: Row | None = None
        if bind:
            bound_ts = parse_ts(bind.group(1))
            for c in candidates:
                if bound_ts is not None and c.ts == bound_ts:
                    chosen = c
                    break
        if chosen is None:
            chosen = candidates[-1]  # LIFO: latest matching open claim
        open_claims.remove(chosen)
        bindings[id(chosen)] = row
    return _close_duplicate_claims(claims, open_claims, bindings)


def scope_key(claim: Row) -> str:
    """The claim's scope text with the cells a re-appended duplicate may vary
    in (its own lane/actor/ticket fields, an estimate cell) removed, or '' when
    too little text is left to call two claims the same work."""
    cells = [c.strip() for c in claim.body.split("|")]
    kept = [
        re.sub(r"^scope=", "", c, flags=re.IGNORECASE)
        for c in cells
        if c and not SCOPE_NOISE_CELL_RE.match(c)
    ]
    key = " ".join(" | ".join(kept).split()).lower()
    return key if len(key) >= MIN_SCOPE_KEY_CHARS else ""


def _close_duplicate_claims(
    claims: list[Row], open_claims: list[Row], bindings: dict[int, Row]
) -> list[Row]:
    """A lane that appended the same CLAIM twice closes both with one row: an
    open claim whose lane and scope text match a closed claim's, written
    within DUPLICATE_WINDOW of it, is closed by that claim's closer, provided
    it was written before the closer."""
    closed_twins: dict[tuple[str, str], list[tuple[Row, Row]]] = {}
    for claim in claims:
        closer = bindings.get(id(claim))
        key = scope_key(claim) if claim.lane and closer is not None else ""
        if key and closer is not None:
            closed_twins.setdefault((claim.lane or "", key), []).append((claim, closer))
    still_open: list[Row] = []
    for claim in open_claims:
        key = scope_key(claim) if claim.lane else ""
        closer = next(
            (
                c
                for twin, c in closed_twins.get((claim.lane or "", key), [])
                if abs(twin.ts - claim.ts) <= DUPLICATE_WINDOW and _before(claim, c)
            ),
            None,
        )
        if key and closer is not None:
            bindings[id(claim)] = closer
        else:
            still_open.append(claim)
    return still_open


def _compatible(claim: Row, terminal: Row) -> bool:
    if claim.lane and terminal.lane:
        if claim.lane != terminal.lane:
            return False
        return not (
            claim.tickets
            and terminal.tickets
            and not (claim.tickets & terminal.tickets)
        )
    # Heading rows carry no lane: pair by ticket overlap.
    return bool(claim.tickets and terminal.tickets and claim.tickets & terminal.tickets)
