# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Render the open-obligations views that markdown surfaces used to OWN.

[OMN-17019] DoD item 3. The session goal (B1), the rolling-plan governor (B2),
the open-ask reconciliation at session close (A6) and the delivery-acknowledgement
states (A14) were each, before this ticket, a hand-maintained markdown file that
*was* the record. Off-rails rev 2 rejects that outright: a client renders truth,
it does not create it. The functions here are those four surfaces as PURE
RENDERERS over the projection -- a renderer can be deleted and regenerated,
which is exactly this ticket's falsifiable done-proof.

Everything in this module is PURE. It performs no database I/O: rows arrive as
typed read models and the caller obtains them through the runtime's
contract-declared projection surface. A renderer that opened its own connection
would violate the runtime-owns-the-database boundary
(``feedback_only_runtime_touches_database``) for no benefit.

``render_open_obligations`` and ``parse_open_obligations`` are exact inverses
over the fields they carry, which is what makes "delete the rendered file,
regenerate it from the projection, diff against the pre-delete copy"
mechanically testable rather than eyeballed.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from omnimarket.nodes.node_projection_open_obligations.models.model_obligation_event import (
    EnumObligationState,
)
from omnimarket.nodes.node_projection_open_obligations.models.model_open_obligation_view import (
    ModelOpenObligationView,
)

_HEADER = "| obligation_id | owed_by | asked_by | acceptance_condition | last_event_at (UTC) |"
_DIVIDER = "|---|---|---|---|---|"
_EMPTY_LINE = "_Nothing is currently owed._"

# A literal pipe would break the table row into extra cells and make the parse
# lossy, so it is escaped on render and restored on parse. Newlines are folded
# for the same reason -- an obligation is one table LINE by construction.
_PIPE = "|"
_ESCAPED_PIPE = "&#124;"

# Rendered in place of a NULL. Chosen over an empty cell so a missing value is
# visibly missing rather than looking like a value that happens to be blank, and
# so the parse can restore None unambiguously.
_ABSENT = "-"


def _escape_cell(value: str) -> str:
    return value.replace(_PIPE, _ESCAPED_PIPE).replace("\n", " ").replace("\r", " ")


def _unescape_cell(value: str) -> str:
    return value.replace(_ESCAPED_PIPE, _PIPE)


def _cell(value: str | None) -> str:
    return _ABSENT if value is None else _escape_cell(value)


def _uncell(value: str) -> str | None:
    return None if value == _ABSENT else _unescape_cell(value)


def _format_timestamp(value: datetime) -> str:
    """Render to whole seconds, in UTC, for a value labelled ``Z``.

    ``astimezone(UTC)`` is load-bearing: converting to the RENDERING MACHINE's
    local zone while the template still appends ``Z`` produces a timestamp that
    is hours off and labelled UTC -- a real defect the sibling
    ``node_projection_work_events.ledger_view`` shipped and had to fix. A naive
    datetime is treated as already-UTC rather than silently taking the host
    zone, for the same reason.
    """
    if value.tzinfo is None:
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_timestamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def rows_from_records(
    records: Iterable[dict[str, object]],
) -> tuple[ModelOpenObligationView, ...]:
    """Validate raw projection_api records into typed read models.

    Kept separate from rendering so an unparseable record fails at the boundary
    with the offending row named, rather than silently rendering a blank line
    that reads like "nothing is owed here".
    """
    return tuple(ModelOpenObligationView.model_validate(record) for record in records)


def open_obligations(
    rows: Iterable[ModelOpenObligationView],
) -> tuple[ModelOpenObligationView, ...]:
    """Filter to what is CURRENTLY owed, newest event first.

    The single definition of "open" for every renderer below. Sorting is by
    ``last_event_at`` descending with ``obligation_id`` as the tiebreak, so the
    order is total and the render is deterministic for a fixed input -- two
    obligations whose most recent events share a timestamp must not swap places
    between runs, or the diff-the-regenerated-file done-proof would be flaky
    rather than falsifiable.
    """
    return tuple(
        sorted(
            (row for row in rows if row.state is EnumObligationState.OPEN),
            key=lambda row: (row.last_event_at, row.obligation_id),
            reverse=True,
        )
    )


def render_open_obligations(rows: Iterable[ModelOpenObligationView]) -> str:
    """Render the open set as a markdown table.

    This is the B1 session-goal body, the B2 rolling-plan open column and the A6
    session-close open-ask list -- one renderer, because they were always the
    same question ("what is owed") asked by three surfaces that each kept their
    own answer.
    """
    selected = open_obligations(rows)
    if not selected:
        return _EMPTY_LINE
    lines = [_HEADER, _DIVIDER]
    for row in selected:
        lines.append(
            f"| {_escape_cell(row.obligation_id)} "
            f"| {_cell(row.owed_by)} "
            f"| {_cell(row.asked_by)} "
            f"| {_cell(row.acceptance_condition)} "
            f"| {_format_timestamp(row.last_event_at)} |"
        )
    return "\n".join(lines)


def parse_open_obligations(rendered: str) -> tuple[ModelOpenObligationView, ...]:
    """Exact inverse of :func:`render_open_obligations` over the fields it carries.

    Every parsed row is ``state=open`` by construction: the renderer emits only
    the open set, so a row read back out of the view IS an open obligation. That
    is the property the round-trip test asserts.
    """
    lines = [line.strip() for line in rendered.splitlines() if line.strip()]
    if lines == [_EMPTY_LINE] or not lines:
        return ()
    parsed: list[ModelOpenObligationView] = []
    for line in lines:
        if line in (_HEADER, _DIVIDER):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 5:
            raise ValueError(
                f"open-obligations row has {len(cells)} cells, expected 5: {line!r}"
            )
        parsed.append(
            ModelOpenObligationView(
                obligation_id=_unescape_cell(cells[0]),
                state=EnumObligationState.OPEN,
                owed_by=_uncell(cells[1]),
                asked_by=_uncell(cells[2]),
                acceptance_condition=_uncell(cells[3]),
                last_event_at=_parse_timestamp(cells[4]),
            )
        )
    return tuple(parsed)


def render_delivery_acknowledgements(
    rows: Iterable[ModelOpenObligationView],
) -> str:
    """Render the A14 delivery-acknowledgement view: what closed, and on what.

    An obligation appears here only with the evidence that closed it. A
    ``satisfied`` row shows its artifact reference and delivery state -- never a
    ticket id, because off-rails rev 2 (A14) is explicit that a ticket moving to
    Done is not evidence that anything reached anyone. A ``superseded`` row names
    its successor; an ``abandoned`` row states its reason. There is no fourth
    closed shape, so nothing can leave the open set unaccounted for.
    """
    closed = sorted(
        (row for row in rows if row.state is not EnumObligationState.OPEN),
        key=lambda row: (row.last_event_at, row.obligation_id),
        reverse=True,
    )
    if not closed:
        return "_No obligations have closed._"
    lines = [
        "| obligation_id | state | closing evidence | last_event_at (UTC) |",
        "|---|---|---|---|",
    ]
    for row in closed:
        if row.state is EnumObligationState.SATISFIED:
            evidence = f"{_cell(row.evidence_uri)} ({_cell(row.delivery_state)})"
        elif row.state is EnumObligationState.SUPERSEDED:
            evidence = f"superseded by {_cell(row.superseded_by)}"
        else:
            evidence = f"abandoned: {_cell(row.abandon_reason)}"
        lines.append(
            f"| {_escape_cell(row.obligation_id)} "
            f"| {row.state.value} "
            f"| {evidence} "
            f"| {_format_timestamp(row.last_event_at)} |"
        )
    return "\n".join(lines)


def unmet_obligations_for_close(
    rows: Sequence[ModelOpenObligationView],
) -> tuple[ModelOpenObligationView, ...]:
    """A6: the obligations that must block a session close.

    Returns the open set. A caller that gets a non-empty tuple back and closes
    anyway is the failure this ticket was filed against, so the function returns
    the offending rows rather than a bool -- a caller cannot report "clean"
    without naming what it ignored.
    """
    return open_obligations(rows)


__all__: list[str] = [
    "open_obligations",
    "parse_open_obligations",
    "render_delivery_acknowledgements",
    "render_open_obligations",
    "rows_from_records",
    "unmet_obligations_for_close",
]
