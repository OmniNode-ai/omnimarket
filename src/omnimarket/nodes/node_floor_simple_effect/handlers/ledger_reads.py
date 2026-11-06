# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the floor reads from the ledger (OMN-22674): the go for rebuild pieces and the events of pieces that write rows.

Two facts of the strip were values set by hand, and the floor drew them wrong: the highest piece the operator said go to,
and whether a piece had started. Both are on the ledger already. The go is a RULING row, written when the operator
answers; a piece that writes ledger rows (the code graph writes one STATUS row a run) has golden and error events there.

The rows come through the reading writer's ledger row port, so the route is the overlay's and the in-memory ledger is the
test backend. A read that fails is a failure with its reason and never an empty answer: the floor draws the go and the
events as unread, not as zero. Everything that decides what counts (the ticket, the lane, the patterns, the cells) is
overlay data; nothing here names a piece, a ticket or a row.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict

from omnimarket.models.floor_17a import (
    ModelFloorSimpleOverlay,
    ModelGoRule,
    ModelNodeEmission,
    ModelPieceEventRule,
    ModelRowMatch,
)
from omnimarket.models.model_reading import ModelLedgerEvidenceRow
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ProtocolLedgerRowSource,
)

RULING_ROW_TYPE = "RULING"
_FUTURE_SLACK = timedelta(days=1)


class ModelLedgerFacts(BaseModel):
    """What one read of the ledger gave: the go, and the piece events as emissions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    go_given_through: int
    go_rows: tuple[str, ...] = ()
    events: tuple[ModelNodeEmission, ...] = ()


class ModelLedgerFloorRead(BaseModel):
    """The facts, or the reason they could not be read. Never both, and never an empty answer for an error."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    facts: ModelLedgerFacts | None = None
    error: str | None = None


def verbatim_answer(row: ModelLedgerEvidenceRow) -> str:
    """The operator's words of a RULING row: its last cell, with the quotes taken off."""
    last = row.raw.rsplit(" | ", 1)[-1].strip()
    if len(last) >= 2 and last[0] == last[-1] == '"':
        return last[1:-1]
    return last


def _covered_pieces(match: re.Match[str]) -> range | None:
    first = int(match.group("first"))
    last_text = match.groupdict().get("last")
    last = int(last_text) if last_text else first
    if last < first:
        return None
    return range(first, last + 1)


def go_given_through(rows: list[ModelLedgerEvidenceRow], rule: ModelGoRule) -> int:
    """The highest piece the go covers, counted from piece 1 with no gap, from the rulings in ``rows``.

    The newest ruling that covers a piece decides it, so a later answer that is not affirmative takes the go back.
    """
    question = re.compile(rule.question_pattern)
    affirmative = re.compile(rule.affirmative_pattern)
    granted: dict[int, bool] = {}
    for row in sorted(rows, key=lambda r: r.stamp):
        if (
            row.row_type != RULING_ROW_TYPE
            or row.lane != rule.lane
            or row.cells.get("ticket") != rule.ticket
        ):
            continue
        matched = question.search(row.cells.get("question", ""))
        pieces = _covered_pieces(matched) if matched else None
        if pieces is None:
            continue
        said_go = affirmative.search(verbatim_answer(row)) is not None
        for piece in pieces:
            granted[piece] = said_go
    through = 0
    while granted.get(through + 1, False):
        through += 1
    return through


def _row_matches(row: ModelLedgerEvidenceRow, match: ModelRowMatch) -> bool:
    return (
        row.row_type == match.row_type
        and row.lane == match.lane
        and all(row.cells.get(k) == v for k, v in match.cells.items())
    )


def _chain(
    row: ModelLedgerEvidenceRow, rule: ModelPieceEventRule, *, failed: bool
) -> tuple[ModelNodeEmission, ...]:
    """The start and the terminal event one ledger row stands for, under a correlation id the row decides."""
    correlation = hashlib.sha256(row.raw.encode("utf-8")).hexdigest()[:32]
    start = ModelNodeEmission(
        node=rule.node,
        piece=rule.piece,
        correlation_id=correlation,
        phase="start",
        emitted_at=row.stamp,
        drill=failed and row.cells.get(rule.error_drill_cell) == "yes",
    )
    if failed:
        code = row.cells.get(rule.failure_code_cell, "")
        return (
            start,
            start.model_copy(
                update={
                    "phase": "failure",
                    "failure_code": code
                    if code not in ("", "none")
                    else rule.failure_default,
                    "failure_detail": row.raw[:300],
                }
            ),
        )
    return (start, start.model_copy(update={"phase": "success"}))


def piece_events(
    rows: list[ModelLedgerEvidenceRow], rules: tuple[ModelPieceEventRule, ...]
) -> tuple[ModelNodeEmission, ...]:
    """Golden and error emissions for the rows that match a piece's event rules, oldest first."""
    events: list[ModelNodeEmission] = []
    for rule in rules:
        for row in sorted(rows, key=lambda r: r.stamp):
            if _row_matches(row, rule.golden):
                events.extend(_chain(row, rule, failed=False))
            elif _row_matches(row, rule.error):
                events.extend(_chain(row, rule, failed=True))
    return tuple(events)


def read_ledger_facts(
    source: ProtocolLedgerRowSource, overlay: ModelFloorSimpleOverlay, now: datetime
) -> ModelLedgerFloorRead:
    """Read the go rulings and the piece event rows. Any read that fails fails the whole read, with its reason."""
    until = now + _FUTURE_SLACK
    go_rule = overlay.rebuild.go
    rulings = source.read(
        ModelLedgerQuery(
            since=now - timedelta(days=go_rule.lookback_days),
            until=until,
            row_type=RULING_ROW_TYPE,
            lane=go_rule.lane,
        )
    )
    if rulings.error is not None:
        return ModelLedgerFloorRead(error=f"go rulings unread: {rulings.error}")
    # One read per (row type, lane) of the rules; a row two rules would both read is one row.
    wanted: dict[tuple[str, str], int] = {}
    for rule in overlay.piece_events:
        for match in (rule.golden, rule.error):
            key = (match.row_type, match.lane)
            wanted[key] = max(wanted.get(key, 0), rule.lookback_days)
    event_rows: dict[str, ModelLedgerEvidenceRow] = {}
    for (row_type, lane), days in sorted(wanted.items()):
        read = source.read(
            ModelLedgerQuery(
                since=now - timedelta(days=days),
                until=until,
                row_type=row_type,
                lane=lane,
            )
        )
        if read.error is not None:
            return ModelLedgerFloorRead(
                error=f"{row_type} rows of {lane} unread: {read.error}"
            )
        for row in read.rows:
            event_rows[row.raw] = row
    rows = list(rulings.rows)
    go = go_given_through(rows, go_rule)
    return ModelLedgerFloorRead(
        facts=ModelLedgerFacts(
            go_given_through=go,
            go_rows=tuple(
                r.stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
                for r in sorted(rows, key=lambda r: r.stamp)
                if r.cells.get("ticket") == go_rule.ticket
                and re.search(go_rule.question_pattern, r.cells.get("question", ""))
            ),
            events=piece_events(list(event_rows.values()), overlay.piece_events),
        )
    )
