# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the floor reads from the ledger (OMN-22674): the go for rebuild pieces, the events of pieces that write rows and
(OMN-22478) the reading writer's reading rows.

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
    ModelLedgerReadingRule,
    ModelLedgerShadowRule,
    ModelNodeEmission,
    ModelPieceEventRule,
    ModelReadingRow,
    ModelRowMatch,
    ModelShadowReading,
)
from omnimarket.models.model_reading import ModelLedgerEvidenceRow
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    read_queries,
)
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
    # The pieces opened by name (RULING rows by subject), beyond the in-order go.
    go_pieces: tuple[int, ...] = ()
    go_rows: tuple[str, ...] = ()
    events: tuple[ModelNodeEmission, ...] = ()
    # The newest reading row of each piece the reading writer wrote, and how many matching rows could not be read as one.
    readings: tuple[ModelReadingRow, ...] = ()
    readings_rejected: int = 0
    # The newest shadow row of each shadow rule that has one, and how many matching rows could not be read as one.
    shadows: tuple[ModelShadowReading, ...] = ()
    shadows_rejected: int = 0
    # A shadow rule whose query failed, with its reason. The shadow rows are advisory: the go, the events and the readings
    # stay readable without them, and the failure stays visible here.
    shadows_unread: tuple[str, ...] = ()


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


_PIECE_NUMBER = re.compile(r"\d+|\bto\b", re.IGNORECASE)
_PARENTHETICAL = re.compile(r"\([^)]*\)")


def pieces_named(text: str) -> tuple[int, ...]:
    """The piece numbers a subject lists: ``7 (name) and piece 11`` is 7 and 11, ``2 to 6`` is 2 to 6.

    Words in brackets are labels and carry no number. A ``to`` between two numbers makes the range; a range that runs
    backwards names nothing.
    """
    out: list[int] = []
    previous: int | None = None
    ranged = False
    for token in _PIECE_NUMBER.finditer(_PARENTHETICAL.sub(" ", text)):
        if token.group(0).lower() == "to":
            ranged = previous is not None
            continue
        number = int(token.group(0))
        if ranged and previous is not None:
            if number > previous:
                out.extend(range(previous + 1, number + 1))
        else:
            out.append(number)
        previous = number
        ranged = False
    return tuple(out)


def opened_pieces(
    rows: list[ModelLedgerEvidenceRow], rule: ModelGoRule
) -> tuple[int, ...]:
    """The pieces rulings opened by name, from the rulings in ``rows``, whatever their place in the order.

    A RULING row of the rule's lane, on one of its ``open_tickets`` (the rule's own ticket is read in order by the go), whose question matches
    ``open_pattern`` names the pieces it opens in its ``list`` group. The newest ruling that names a piece decides it, so a
    later answer that is not affirmative takes the piece back. A rule that declares no open pattern opens nothing.
    """
    if rule.open_pattern is None or rule.open_affirmative_pattern is None:
        return ()
    opening = re.compile(rule.open_pattern)
    affirmative = re.compile(rule.open_affirmative_pattern)
    tickets = set(rule.open_tickets)
    opened: dict[int, bool] = {}
    for row in sorted(rows, key=lambda r: r.stamp):
        if (
            row.row_type != RULING_ROW_TYPE
            or row.lane != rule.lane
            or row.cells.get("ticket") not in tickets
        ):
            continue
        matched = opening.search(row.cells.get("question", ""))
        if matched is None:
            continue
        said_yes = affirmative.search(verbatim_answer(row)) is not None
        for piece in pieces_named(matched.group("list")):
            opened[piece] = said_yes
    return tuple(sorted(p for p, yes in opened.items() if yes))


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
    named = row.cells.get(rule.correlation_cell, "") if rule.correlation_cell else ""
    correlation = named or hashlib.sha256(row.raw.encode("utf-8")).hexdigest()[:32]
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


_READING_VERDICTS = {
    "WORKING": "WORKING",
    "NOT_WORKING": "NOT WORKING",
    "UNKNOWN": "UNKNOWN",
}


def _reading_row(
    row: ModelLedgerEvidenceRow, rule: ModelLedgerReadingRule
) -> ModelReadingRow | None:
    """One reading row of the reading writer as the floor's reading of a piece, or ``None`` when a cell is not usable."""
    verdict = _READING_VERDICTS.get(row.cells.get(rule.verdict_cell, ""))
    try:
        piece = int(row.cells[rule.piece_cell])
        probed = datetime.fromisoformat(row.cells[rule.probed_at_cell])
        published = datetime.fromisoformat(row.cells[rule.published_at_cell])
        max_age = int(row.cells[rule.max_age_cell])
    except (KeyError, ValueError):
        return None
    if verdict is None or not 1 <= piece <= 18 or max_age <= 0:
        return None
    if probed.tzinfo is None or published.tzinfo is None:
        return None
    return ModelReadingRow(
        piece=piece,
        state=verdict,
        probed_at=probed,
        published_at=published,
        max_age_s=max_age,
        reason=row.cells.get(rule.reason_cell, "") if rule.reason_cell else "",
    )


def reading_rows(
    rows: list[ModelLedgerEvidenceRow], rule: ModelLedgerReadingRule
) -> tuple[tuple[ModelReadingRow, ...], int]:
    """The newest reading row of each piece, and the count of matching rows that could not be read as a reading."""
    match = ModelRowMatch(row_type=rule.row_type, lane=rule.lane, cells=rule.cells)
    newest: dict[int, ModelReadingRow] = {}
    rejected = 0
    for row in rows:
        if not _row_matches(row, match):
            continue
        reading = _reading_row(row, rule)
        if reading is None:
            rejected += 1
        elif (
            reading.piece not in newest
            or reading.published_at > newest[reading.piece].published_at
        ):
            newest[reading.piece] = reading
    return tuple(newest[p] for p in sorted(newest)), rejected


def shadow_readings(
    rows: list[ModelLedgerEvidenceRow], rule: ModelLedgerShadowRule
) -> tuple[ModelShadowReading | None, int]:
    """The newest row of a shadow rule as a reading, and the count of matching rows whose cells were not usable."""
    match = ModelRowMatch(row_type=rule.row_type, lane=rule.lane, cells=rule.cells)
    newest: ModelShadowReading | None = None
    rejected = 0
    for row in rows:
        if not _row_matches(row, match):
            continue
        try:
            published = datetime.fromisoformat(row.cells[rule.published_at_cell])
            max_age = int(row.cells[rule.max_age_cell])
            values = tuple((f.label, row.cells[f.cell]) for f in rule.fields)
            outcome = row.cells[rule.outcome_cell] if rule.outcome_cell else None
        except (KeyError, ValueError):
            rejected += 1
            continue
        if published.tzinfo is None or max_age <= 0:
            rejected += 1
            continue
        if newest is None or published > newest.published_at:
            newest = ModelShadowReading(
                piece=rule.piece,
                label=rule.label,
                published_at=published,
                max_age_s=max_age,
                values=values,
                failed_outcome=outcome if outcome != rule.outcome_ok else None,
            )
    return newest, rejected


def read_ledger_facts(
    source: ProtocolLedgerRowSource, overlay: ModelFloorSimpleOverlay, now: datetime
) -> ModelLedgerFloorRead:
    """Read the go rulings and the piece event rows. Any read of those that fails fails the whole read, with its reason.

    The ledger is read once per cycle (OMN-23081): every query is built first and a source with a single-pass read
    answers them all from one scan of the live file and its rolls. Before, each query scanned and parsed every line
    again, which took 14 s of the publisher's 15 s step limit. The answers are used in the order the queries were asked,
    so the reason a failed read gives is the same as before.

    A shadow row is advisory telemetry: a failed shadow query is listed in ``shadows_unread`` and costs no other fact.
    """
    until = now + _FUTURE_SLACK
    go_rule = overlay.rebuild.go
    # One read per (row type, lane) of the rules; a row two rules would both read is one row.
    wanted: dict[tuple[str, str], int] = {}
    for rule in overlay.piece_events:
        for match in (rule.golden, rule.error):
            key = (match.row_type, match.lane)
            wanted[key] = max(wanted.get(key, 0), rule.lookback_days)
    event_keys = sorted(wanted.items())
    reading_rule = overlay.ledger_readings
    queries: list[ModelLedgerQuery] = [
        ModelLedgerQuery(
            since=now - timedelta(days=go_rule.lookback_days),
            until=until,
            row_type=RULING_ROW_TYPE,
            lane=go_rule.lane,
        ),
        *(
            ModelLedgerQuery(
                since=now - timedelta(days=days),
                until=until,
                row_type=row_type,
                lane=lane,
            )
            for (row_type, lane), days in event_keys
        ),
    ]
    if reading_rule is not None:
        queries.append(
            ModelLedgerQuery(
                since=now - timedelta(days=reading_rule.lookback_days),
                until=until,
                row_type=reading_rule.row_type,
                lane=reading_rule.lane,
            )
        )
    queries.extend(
        ModelLedgerQuery(
            since=now - timedelta(days=shadow_rule.lookback_days),
            until=until,
            row_type=shadow_rule.row_type,
            lane=shadow_rule.lane,
        )
        for shadow_rule in overlay.ledger_shadows
    )
    answers = iter(read_queries(source, queries))
    rulings = next(answers)
    if rulings.error is not None:
        return ModelLedgerFloorRead(error=f"go rulings unread: {rulings.error}")
    event_rows: dict[str, ModelLedgerEvidenceRow] = {}
    for (row_type, lane), _days in event_keys:
        read = next(answers)
        if read.error is not None:
            return ModelLedgerFloorRead(
                error=f"{row_type} rows of {lane} unread: {read.error}"
            )
        for row in read.rows:
            event_rows[row.raw] = row
    readings: tuple[ModelReadingRow, ...] = ()
    rejected = 0
    if reading_rule is not None:
        read = next(answers)
        if read.error is not None:
            return ModelLedgerFloorRead(
                error=f"{reading_rule.row_type} rows of {reading_rule.lane} unread: {read.error}"
            )
        readings, rejected = reading_rows(list(read.rows), reading_rule)
    shadows: list[ModelShadowReading] = []
    shadows_rejected = 0
    shadows_unread: list[str] = []
    for shadow_rule in overlay.ledger_shadows:
        shadow_read = next(answers)
        if shadow_read.error is not None:
            shadows_unread.append(
                f"{shadow_rule.row_type} rows of {shadow_rule.lane} unread: {shadow_read.error}"
            )
            continue
        reading, shadow_rejects = shadow_readings(list(shadow_read.rows), shadow_rule)
        shadows_rejected += shadow_rejects
        if reading is not None:
            shadows.append(reading)
    rows = list(rulings.rows)
    go = go_given_through(rows, go_rule)
    return ModelLedgerFloorRead(
        facts=ModelLedgerFacts(
            go_given_through=go,
            go_pieces=opened_pieces(rows, go_rule),
            go_rows=tuple(
                r.stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
                for r in sorted(rows, key=lambda r: r.stamp)
                if (
                    r.cells.get("ticket") == go_rule.ticket
                    and re.search(go_rule.question_pattern, r.cells.get("question", ""))
                )
                or (
                    go_rule.open_pattern is not None
                    and r.row_type == RULING_ROW_TYPE
                    and r.cells.get("ticket") in go_rule.open_tickets
                    and re.search(go_rule.open_pattern, r.cells.get("question", ""))
                )
            ),
            events=piece_events(list(event_rows.values()), overlay.piece_events),
            readings=readings,
            readings_rejected=rejected,
            shadows=tuple(shadows),
            shadows_rejected=shadows_rejected,
            shadows_unread=tuple(shadows_unread),
        )
    )
