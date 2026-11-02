# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Report a mechanism ticket whose lane ended unfinished and was never picked up.

A RULING or CORRECTION names its mechanism ticket in a ``mechanism-ticket=`` cell.
When the lane working that ticket ends with a TERMINAL whose outcome is blocked,
failed or partial and no CLAIM on the ticket follows within one tick, the ruling
has no one building it: that is one owed row, keyed on the ticket and the
TERMINAL stamp. A handed-off or done TERMINAL owes nothing.

Pure over the request: no file read, no environment read and no clock read. Rows
later than the tick are ignored, so a replay at the same tick is identical.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from omnimarket.nodes.node_ruling_mechanism_owed_compute.models.model_ruling_mechanism_owed import (
    ModelOwedRow,
    ModelRulingMechanismOwedRequest,
    ModelRulingMechanismOwedResult,
)

_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_CELL_RE = re.compile(r"^([A-Za-z][\w-]*)=(.*)$")
_TICKET_RE = re.compile(r"^OMN-\d+$")
# Rows that open a ruling, and rows that may replace the mechanism ticket of one.
_RULING_TYPES = frozenset({"RULING", "OPERATOR-CONSENT"})
_LINK_TYPES = frozenset({"CORRECTION", "RULING", "OPERATOR-CONSENT"})
_LINK_KEYS = ("corrects", "amends", "re", "ruling", "rulings", "mechanism-for")
_UNFINISHED = frozenset({"blocked", "failed", "partial"})


@dataclass(frozen=True)
class _Row:
    ts: str
    kind: str
    cells: dict[str, str]
    raw: str


def _parse(raw: str) -> _Row | None:
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 2 or not _STAMP_RE.match(parts[0]):
        return None
    cells: dict[str, str] = {}
    for part in parts[2:]:
        match = _CELL_RE.match(part)
        if match and match[1] not in cells:
            cells[match[1]] = match[2]
    return _Row(parts[0], parts[1], cells, raw)


def _mechanism_tickets(row: _Row) -> tuple[str, ...]:
    value = row.cells.get("mechanism-ticket", "")
    return tuple(t for t in re.split(r"[,\s]+", value) if _TICKET_RE.match(t))


def _linked_rulings(row: _Row) -> Iterable[str]:
    for key in _LINK_KEYS:
        yield from (t for t in re.split(r"[,\s]+", row.cells.get(key, "")) if t)


def _effective_mechanisms(rows: list[_Row]) -> dict[str, tuple[str, ...]]:
    """Per ruling stamp, the mechanism tickets of the newest row that sets them."""
    newest: dict[str, tuple[tuple[str, str], tuple[str, ...]]] = {}
    ruling_stamps = {r.ts for r in rows if r.kind in _RULING_TYPES}
    for row in rows:
        tickets = _mechanism_tickets(row)
        if not tickets:
            continue
        order = (row.ts, row.raw)
        targets = [row.ts] if row.kind in _RULING_TYPES else []
        targets += [t for t in _linked_rulings(row) if t in ruling_stamps]
        if row.kind not in _LINK_TYPES:
            continue
        for stamp in targets:
            if stamp not in newest or order >= newest[stamp][0]:
                newest[stamp] = (order, tickets)
    return {stamp: tickets for stamp, (_, tickets) in newest.items()}


def _time(stamp: str) -> datetime:
    return datetime.strptime(stamp, _STAMP).replace(tzinfo=UTC)


class HandlerRulingMechanismOwed:
    """Owed rows for mechanism tickets abandoned after an unfinished TERMINAL."""

    def handle(
        self, request: ModelRulingMechanismOwedRequest
    ) -> ModelRulingMechanismOwedResult:
        tick = request.tick_at
        rows = sorted(
            (r for raw in request.rows if (r := _parse(raw)) and r.ts <= tick),
            key=lambda r: (r.ts, r.raw),
        )
        rulings_of: dict[str, list[str]] = defaultdict(list)
        for stamp, tickets in _effective_mechanisms(rows).items():
            for ticket in tickets:
                rulings_of[ticket].append(stamp)

        claims: dict[str, list[str]] = defaultdict(list)
        for row in rows:
            if row.kind == "CLAIM" and row.cells.get("ticket"):
                claims[row.cells["ticket"]].append(row.ts)

        one_tick = timedelta(seconds=request.tick_interval_seconds)
        owed: dict[str, ModelOwedRow] = {}
        awaiting: set[str] = set()
        for row in rows:
            ticket = row.cells.get("ticket", "")
            outcome = row.cells.get("outcome", "")
            if (
                row.kind != "TERMINAL"
                or outcome not in _UNFINISHED
                or ticket not in rulings_of
            ):
                continue
            key = f"mechanism-owed:{ticket}:{row.ts}"
            if any(stamp > row.ts for stamp in claims[ticket]):
                continue
            if _time(tick) - _time(row.ts) < one_tick:
                awaiting.add(key)
                continue
            owed.setdefault(
                key,
                ModelOwedRow(
                    key=key,
                    ticket=ticket,
                    terminal_at=row.ts,
                    lane=row.cells.get("lane", ""),
                    outcome=outcome,
                    blocker=row.cells.get("blocker", ""),
                    rulings=tuple(sorted(rulings_of[ticket])),
                ),
            )
        return ModelRulingMechanismOwedResult(
            tick_at=tick,
            owed=tuple(owed[k] for k in sorted(owed)),
            awaiting_tick=tuple(sorted(awaiting - owed.keys())),
            mechanism_ticket_count=len(rulings_of),
        )
