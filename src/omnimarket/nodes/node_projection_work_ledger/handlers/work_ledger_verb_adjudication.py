# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Adjudicate why the file and the database answer ``holds`` and ``open-claims`` differently (OMN-21947, L3).

Row parity is exact and the verb answers still differed (49 holds from the database against 2
from the file; 715 claims only in the file, 112 only in the database). Every item that one
source lists and the other does not falls in exactly one class:

``file_window_truncation``
    Only the database lists it, it opened before the first row the file read covered, and the
    file read did not include the archive splits. The file answer is the incomplete one; the
    fix is to read the splits (``--archives``), which is the reader's, not the database's.
``before_first_db_row``
    Only the file lists it and it opened before the database's first row (``min(row_ts)``).
    No fold can produce it from rows the database does not hold; the history import (this
    ticket) supplies the rows.
``database_fold_difference``
    Either source lists it only, it opened inside both sources' extents, and the rows are in
    the database: the two folds disagree. The database's replay is aligned to the reader's
    rule for the cause found (the per-cause fixes and their tests live with the replay).
``unclassified``
    The item carries no opening stamp, so no class can be decided. Reported, never hidden;
    the acceptance bar is zero.

The settled stale-hold rule runs first (RULING 2026-10-29T23:39:54Z, "A hold order, then 14
days counts as stale."): an ``until=open`` hold with no release older than 14 days is not in
force, so it is a difference of neither source; it is listed under its own heading with its age.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta

from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_history import (
    EnumVerbDiffClass,
    EnumVerbSide,
    ModelClassifiedDiff,
    ModelDiffWindow,
    ModelStaleHoldEntry,
    ModelVerbAdjudication,
    ModelVerbItem,
)

STALE_HOLD_AGE = timedelta(days=14)
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_HOLD_LINE = re.compile(r"^HOLD (?P<id>\S+) ")
_UNTIL = re.compile(r"\buntil=(?P<until>\S+)")
_CLAIM_LINE = re.compile(r"^OPEN-CLAIM lane=(?P<lane>\S+) claim=(?P<stamp>\S+) ")

_FIX = {
    EnumVerbDiffClass.FILE_WINDOW_TRUNCATION: (
        EnumVerbSide.FILE,
        "read the archive splits with the file read (--archives); the reader owns it",
    ),
    EnumVerbDiffClass.BEFORE_FIRST_DB_ROW: (
        EnumVerbSide.DB,
        "import the archive splits and the live file once (work_ledger_history_import)",
    ),
    EnumVerbDiffClass.DATABASE_FOLD_DIFFERENCE: (
        EnumVerbSide.DB,
        "align the database replay with the reader's rule for the cause found",
    ),
    EnumVerbDiffClass.UNCLASSIFIED: (
        EnumVerbSide.DB,
        "none: the item has no opening stamp; inspect the row",
    ),
}


def _moment(value: str) -> datetime | None:
    match = _STAMP.match(value)
    if match is None:
        return None
    return datetime.strptime(match.group(0), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def parse_holds_answer(text: str) -> list[ModelVerbItem]:
    """The ``HOLD <id> ...`` lines of a ``holds`` answer. ``HOLD none`` lines hold nothing."""
    items = []
    for line in text.splitlines():
        match = _HOLD_LINE.match(line)
        if match is None or match.group("id") == "none":
            continue
        until = _UNTIL.search(line)
        items.append(
            ModelVerbItem(
                verb="holds",
                key=match.group("id"),
                opened_at=_moment(match.group("id")),
                until_open=until is None or until.group("until") == "open",
            )
        )
    return items


def parse_open_claims_answer(text: str) -> list[ModelVerbItem]:
    """The ``OPEN-CLAIM lane=.. claim=<stamp>`` lines of an ``open-claims`` answer."""
    items = []
    for line in text.splitlines():
        match = _CLAIM_LINE.match(line)
        if match is None:
            continue
        items.append(
            ModelVerbItem(
                verb="open-claims",
                key=f"{match.group('lane')}@{match.group('stamp')}",
                opened_at=_moment(match.group("stamp")),
            )
        )
    return items


def is_stale(item: ModelVerbItem, as_of: datetime) -> bool:
    """An ``until=open`` hold older than 14 days. Only a hold can be stale."""
    return (
        item.verb == "holds"
        and item.until_open
        and item.opened_at is not None
        and as_of - item.opened_at > STALE_HOLD_AGE
    )


def classify_difference(
    item: ModelVerbItem, present_in: EnumVerbSide, window: ModelDiffWindow
) -> EnumVerbDiffClass:
    """The class of one item that only ``present_in`` lists. Pure."""
    if item.opened_at is None:
        return EnumVerbDiffClass.UNCLASSIFIED
    if present_in is EnumVerbSide.FILE:
        if item.opened_at < window.db_first_row_ts:
            return EnumVerbDiffClass.BEFORE_FIRST_DB_ROW
        return EnumVerbDiffClass.DATABASE_FOLD_DIFFERENCE
    if not window.file_reads_archives and item.opened_at < window.file_first_stamp:
        return EnumVerbDiffClass.FILE_WINDOW_TRUNCATION
    return EnumVerbDiffClass.DATABASE_FOLD_DIFFERENCE


def adjudicate_verb(
    verb: str,
    file_items: Iterable[ModelVerbItem],
    db_items: Iterable[ModelVerbItem],
    window: ModelDiffWindow,
) -> ModelVerbAdjudication:
    """Set the stale holds aside, then classify every item only one source lists."""
    file_all = {item.key: item for item in file_items}
    db_all = {item.key: item for item in db_items}
    stale: dict[str, ModelStaleHoldEntry] = {}
    for item in [*file_all.values(), *db_all.values()]:
        if is_stale(item, window.as_of) and item.opened_at is not None:
            stale[item.key] = ModelStaleHoldEntry(
                key=item.key,
                opened_at=item.opened_at,
                age_days=(window.as_of - item.opened_at).days,
            )
    file_live = {k: v for k, v in file_all.items() if k not in stale}
    db_live = {k: v for k, v in db_all.items() if k not in stale}
    diffs: list[ModelClassifiedDiff] = []
    for side, mine, other in (
        (EnumVerbSide.FILE, file_live, db_live),
        (EnumVerbSide.DB, db_live, file_live),
    ):
        for key in sorted(mine.keys() - other.keys()):
            item = mine[key]
            diff_class = classify_difference(item, side, window)
            wrong_side, fix = _FIX[diff_class]
            diffs.append(
                ModelClassifiedDiff(
                    item=item,
                    present_in=side,
                    diff_class=diff_class,
                    wrong_side=wrong_side,
                    fix=fix,
                )
            )
    return ModelVerbAdjudication(
        verb=verb,
        shared=len(file_live.keys() & db_live.keys()),
        diffs=tuple(diffs),
        stale=tuple(sorted(stale.values(), key=lambda e: (e.opened_at, e.key))),
    )


def render_adjudication(adjudication: ModelVerbAdjudication) -> list[str]:
    """Plain lines: the counts per class, then the stale holds under their own heading."""
    counts = adjudication.counts()
    lines = [
        f"{adjudication.verb} shared={adjudication.shared} differing={len(adjudication.diffs)} "
        + " ".join(f"{name}={count}" for name, count in counts.items())
    ]
    if adjudication.stale:
        lines.append(
            f"STALE HOLDS (not in force, older than {STALE_HOLD_AGE.days} days):"
        )
        lines.extend(
            f"  {entry.key} opened={entry.opened_at:%Y-%m-%dT%H:%M:%SZ} age={entry.age_days}d"
            for entry in adjudication.stale
        )
    return lines


def adjudicate_answers(
    holds: tuple[str, str],
    open_claims: tuple[str, str],
    window: ModelDiffWindow,
) -> Sequence[ModelVerbAdjudication]:
    """Adjudicate both verbs from their ``(file answer, database answer)`` texts.

    ``window.file_reads_archives`` is the ``holds`` read's; ``open-claims`` always reads
    the splits, so its window says so.
    """
    claims_window = window.model_copy(update={"file_reads_archives": True})
    return (
        adjudicate_verb(
            "holds", parse_holds_answer(holds[0]), parse_holds_answer(holds[1]), window
        ),
        adjudicate_verb(
            "open-claims",
            parse_open_claims_answer(open_claims[0]),
            parse_open_claims_answer(open_claims[1]),
            claims_window,
        ),
    )


__all__ = [
    "STALE_HOLD_AGE",
    "adjudicate_answers",
    "adjudicate_verb",
    "classify_difference",
    "is_stale",
    "parse_holds_answer",
    "parse_open_claims_answer",
    "render_adjudication",
]
