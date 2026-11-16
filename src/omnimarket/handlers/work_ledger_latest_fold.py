# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The pure work-ledger latest fold (OMN-23606): one row in, its latest entries out.

No clock, no broker, no database. ``apply_latest`` is the in-memory twin of the
writer's guarded SQL, so the fold's order-independence is falsified by a unit
test and the parity check can fold the markdown file with the very same rules
the lab projection applies.

``work_ledger_latest`` keeps, per subject (a lane or a ticket) and per tracked
row type, the newest row and the few cells a reader decides on. It is state,
not history: a board or a lane placement that needs "when was this ticket last
claimed, closed, asked about" reads one small page of this table instead of
paging every row of a window.
"""

from __future__ import annotations

from collections.abc import Iterable

from omnimarket.handlers.ledger_row_grammar import parse_row
from omnimarket.models.model_work_ledger_fold_result import (
    ModelWorkLedgerRowRecord,
)
from omnimarket.models.work_ledger_query.model_work_ledger_latest import (
    EnumWorkLedgerLatestSubject,
    ModelWorkLedgerLatestEntry,
    ModelWorkLedgerLatestPage,
    ModelWorkLedgerLatestQuery,
)


def _first(fields: dict[str, list[str]], key: str) -> str | None:
    values = fields.get(key)
    return values[0] if values else None


def latest_entries_for_row(
    row: ModelWorkLedgerRowRecord, tracked_row_types: frozenset[str]
) -> tuple[ModelWorkLedgerLatestEntry, ...]:
    """The latest entries one row contributes, or none when its type is untracked."""
    if row.row_type not in tracked_row_types:
        return ()

    first_line = row.raw_row.split("\n", 1)[0]
    parsed = parse_row(first_line)
    fields = parsed.fields if parsed is not None else {}

    pr = _first(fields, "pr")
    outcome = _first(fields, "outcome")
    to = _first(fields, "to")
    ticket = row.tickets[0] if row.tickets else None

    def entry(
        subject_kind: EnumWorkLedgerLatestSubject, subject: str
    ) -> ModelWorkLedgerLatestEntry:
        return ModelWorkLedgerLatestEntry(
            subject_kind=subject_kind,
            subject=subject,
            row_type=row.row_type,
            row_id=row.row_id,
            at=row.row_ts,
            lane=row.row_lane,
            ticket=ticket,
            pr=pr,
            outcome=outcome,
            to=to,
        )

    entries: list[ModelWorkLedgerLatestEntry] = []
    if row.row_lane:
        entries.append(entry(EnumWorkLedgerLatestSubject.LANE, row.row_lane))
    for t in row.tickets:
        entries.append(entry(EnumWorkLedgerLatestSubject.TICKET, t))
    return tuple(entries)


def entry_key(entry: ModelWorkLedgerLatestEntry) -> tuple[str, str, str]:
    """The keyset key of one entry: subject kind, subject, row type."""
    return (entry.subject_kind.value, entry.subject, entry.row_type)


def apply_latest(
    state: dict[tuple[str, str, str], ModelWorkLedgerLatestEntry],
    entries: Iterable[ModelWorkLedgerLatestEntry],
) -> None:
    """Replace a held entry only when the new one is not older than the held one."""
    for entry in entries:
        key = entry_key(entry)
        held = state.get(key)
        if held is None or (held.at, held.row_id) <= (entry.at, entry.row_id):
            state[key] = entry


def page_latest(
    state: dict[tuple[str, str, str], ModelWorkLedgerLatestEntry],
    query: ModelWorkLedgerLatestQuery,
) -> ModelWorkLedgerLatestPage:
    """One keyset page in key order, filtered by the query's narrowing fields."""
    after = query.after
    after_key = (
        (after.subject_kind.value, after.subject, after.row_type) if after else None
    )

    def matches(entry: ModelWorkLedgerLatestEntry) -> bool:
        if (
            query.subject_kind is not None
            and entry.subject_kind is not query.subject_kind
        ):
            return False
        if query.subjects and entry.subject not in query.subjects:
            return False
        if query.row_types and entry.row_type not in query.row_types:
            return False
        if query.since is not None and entry.at < query.since:
            return False
        return True

    ordered = sorted(state.items(), key=lambda item: item[0])
    entries: list[ModelWorkLedgerLatestEntry] = []
    has_more = False
    for key, entry in ordered:
        if after_key is not None and key <= after_key:
            continue
        if not matches(entry):
            continue
        if len(entries) == query.limit:
            has_more = True
            break
        entries.append(entry)

    return ModelWorkLedgerLatestPage(
        entries=tuple(entries),
        watermark=None,
        has_more=has_more,
    )


__all__: list[str] = [
    "apply_latest",
    "entry_key",
    "latest_entries_for_row",
    "page_latest",
]
