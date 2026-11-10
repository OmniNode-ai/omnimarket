# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pick the lanes the lab fill resends first, from the ledger alone (OMN-17427).

Moved from the frozen repository by OMN-23250 (RULING 2026-11-03T18:40:27Z): the lab fill's
enumerate read takes its lane-resend source from this handler. The code is the old handler
unchanged; ``live_claims`` and ``live_claim_owners`` come with it because the resend rules
read them.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime, timedelta

from ..models.model_lane_resend import (
    ModelLaneResendCandidate,
    ModelLaneResendRequest,
    ModelLaneResendResult,
)
from .handler_lab_fill_selection import _pr, _Row, _rows


def live_claims(lines: tuple[str, ...], now: datetime) -> tuple[_Row, ...]:
    """Open exact claims with any lane activity in the last 45 minutes (OMN-20864).

    A PR candidate matches PR cells, not another PR's ticket or prose. A shared cause
    matches its cause cell; a per-PR claim cannot own it. TERMINAL ends the lane.
    """
    claims: dict[str, list[_Row]] = {}
    last: dict[str, datetime] = {}
    for row in _rows(lines):
        if not row.lane or row.ts > now:
            continue
        for author in {row.lane, row.values.get("from", "").lower()} - {""}:
            last[author] = row.ts
        if row.kind == "CLAIM":
            claims.setdefault(row.lane, []).append(row)
        elif row.kind == "TERMINAL":
            claims.pop(row.lane, None)
        elif row.kind == "RELEASE" and (
            row.subjects or row.prs or row.values.get("cause")
        ):
            claims[row.lane] = [
                claim
                for claim in claims.get(row.lane, [])
                if not (
                    claim.subjects & row.subjects
                    or claim.prs & row.prs
                    or (
                        row.values.get("cause")
                        and row.values["cause"] == claim.values.get("cause", "")
                    )
                )
            ]

    return tuple(
        row
        for lane, rows in claims.items()
        if now - last[lane] <= timedelta(minutes=45)
        for row in rows
    )


def live_claim_owners(
    claims: tuple[_Row, ...], *, ticket: str = "", pr: str = "", cause: str = ""
) -> tuple[str, ...]:
    """Match a batch's live claims to one exact PR, cause or ticket."""

    def matches(row: _Row) -> bool:
        if cause:
            return row.values.get("cause") == cause
        if pr:
            return _pr(pr) in row.prs
        return ticket in row.subjects

    return tuple(sorted({row.lane for row in claims if matches(row)}))


# --- The resend backlog (OMN-17427) ---
# Pick the lanes lab-fill resends first, from the ledger alone (OMN-17427).
#
# RULING 2026-10-14T21:46:25Z: fill the free lab slots rather than wait. A lane whose newest
# TERMINAL in the last 48 hours is partial, blocked or failed left work a new lane can finish, so
# its ticket is the first thing a free slot takes. Three things keep a resend from being a second
# copy: a ticket with a live lane (a CLAIM with activity in the last 45 minutes), a ticket whose
# newest row after that TERMINAL is a CLAIM (a peer already took it) or a done or handed-off
# TERMINAL, and the one-per-ticket rule. A blocker that names a credential or an operator decision
# is not fixable by a lane and is dropped. The umbrella ticket is never a lane's ticket.

RESEND_OUTCOMES = frozenset({"partial", "blocked", "failed"})
FINISHED_OUTCOMES = frozenset({"done", "handed-off", "skipped-owned"})
_TICKET_RE = re.compile(r"OMN-[0-9]+")
_UNFIXABLE_RE = re.compile(
    r"credential|token|secret|password|api[ _-]?key|login|auth(?:entication|orization)? "
    r"(?:expired|failed|required)|permission denied|\b40[13]\b|operator decision|operator "
    r"ruling|needs? (?:a )?(?:ruling|decision|consent)|awaiting (?:the )?(?:operator|ruling|"
    r"decision|consent)|consent",
    re.IGNORECASE,
)


def _ticket(row: _Row) -> str:
    found = _TICKET_RE.findall(row.values.get("ticket", ""))
    return found[0] if found else ""


class HandlerLaneResend:
    """Read the resend backlog from supplied ledger rows without I/O."""

    def handle(self, request: ModelLaneResendRequest) -> ModelLaneResendResult:
        """Return one candidate per ticket, newest TERMINAL first, capped at the limit."""
        now = datetime.fromisoformat(request.now.replace("Z", "+00:00"))
        if now.tzinfo is None:
            raise ValueError("now must carry a timezone")
        now = now.astimezone(UTC)
        cutoff = now - timedelta(hours=request.window_hours)
        rows = [row for row in _rows(request.ledger_lines) if row.ts <= now]
        claims = live_claims(request.ledger_lines, now)

        newest: dict[str, _Row] = {}
        for row in rows:
            if row.kind == "TERMINAL" and row.lane:
                newest[row.lane] = row

        dropped: Counter[str] = Counter()
        by_ticket: dict[str, _Row] = {}
        for lane, row in newest.items():
            outcome = row.values.get("outcome", "").lower()
            ticket = _ticket(row)
            if row.ts < cutoff or outcome not in RESEND_OUTCOMES:
                continue
            reason = self._drop_reason(request, rows, claims, lane, row, ticket)
            if reason:
                dropped[reason] += 1
            elif ticket not in by_ticket or row.ts > by_ticket[ticket].ts:
                by_ticket[ticket] = row

        ordered = sorted(by_ticket.values(), key=lambda row: row.ts, reverse=True)
        candidates = tuple(self._candidate(row) for row in ordered[: request.limit])
        dropped["over-limit"] += max(0, len(ordered) - request.limit)
        return ModelLaneResendResult(
            candidates, tuple(sorted((r, n) for r, n in dropped.items() if n))
        )

    @staticmethod
    def _drop_reason(
        request: ModelLaneResendRequest,
        rows: list[_Row],
        claims: tuple[_Row, ...],
        lane: str,
        terminal: _Row,
        ticket: str,
    ) -> str:
        if not ticket:
            return "no-ticket"
        if ticket == request.parent_ticket:
            return "umbrella"
        if _UNFIXABLE_RE.search(terminal.values.get("blocker", "")):
            return "credential-or-operator-decision"
        if live_claim_owners(claims, ticket=ticket):
            return "running"
        for row in rows:
            if row.ts <= terminal.ts or ticket not in row.subjects or row.lane == lane:
                continue
            if row.kind == "CLAIM":
                return "resent"
            if (
                row.kind == "TERMINAL"
                and row.values.get("outcome", "").lower() in FINISHED_OUTCOMES
            ):
                return "finished-by-peer"
        return ""

    @staticmethod
    def _candidate(row: _Row) -> ModelLaneResendCandidate:
        outcome = row.values.get("outcome", "").lower()
        blocker = " ".join(row.values.get("blocker", "").split())[:160]
        ended = row.ts.strftime("%Y-%m-%dT%H:%M:%SZ")
        evidence = f"lane {row.lane} ended {outcome} at {ended}" + (
            f": {blocker}" if blocker else ""
        )
        return ModelLaneResendCandidate(
            _ticket(row), row.lane, outcome, ended, blocker, evidence
        )
