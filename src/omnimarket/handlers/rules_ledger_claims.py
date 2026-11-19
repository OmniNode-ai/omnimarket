# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ledger claim reading shared by worktree reconcile and worktree lease."""

import re
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict

_LANE = re.compile(r"(?:^|[\s|])lane=([^\s|]+)")
_TICKET_CELL = re.compile(r"(?:^|[\s|])(?:ticket|parent_ticket)=(OMN-\d+)")
_TICKET_ID = re.compile(r"OMN-\d+")
_CLOSING = ("TERMINAL", "RELEASE")
_SKEW_SECONDS = 3600
_ENDING_OUTCOMES = ("done", "merged", "handed-off")
_WORKTREE_CELL = re.compile(r"(?:^|[\s|])worktree=([^\s|]*)")
_OUTCOME_CELL = re.compile(r"(?:^|[\s|])outcome=([^\s|]*)")


class ModelLedgerRead(BaseModel):
    """One bounded read of the ledger: whether it can vouch for "no lane holds this tree"."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fresh: bool
    reason: str = ""
    live_claims: tuple[str, ...] = ()
    open_claims: tuple[str, ...] = ()
    closed_claims: tuple[str, ...] = ()
    finished_claims: tuple[str, ...] = ()


def claim_lane(row: str) -> str | None:
    """The lane a ledger row names, or None."""
    match = _LANE.search(row)
    return match[1] if match else None


def claim_stamp(row: str) -> str:
    """The first cell of a ledger row, the stamp a RELEASE row cites in `re=`."""
    return row.strip().strip("|").split("|")[0].strip()


def _row(line: str, index: int) -> tuple[datetime, int, str, str, str] | None:
    """One ledger line as (stamp, index, lane, kind, line), or None when it opens nothing."""
    cells = [part.strip() for part in line.strip().strip("|").split("|")]
    if len(cells) < 3 or not re.match(r"\d{4}-\d{2}-\d{2}T", cells[0]):
        return None
    # A shared ledger carries legacy and malformed rows. One bad row must not
    # make the whole ledger unreadable (that would keep every tree forever);
    # a row that cannot be dated or attributed simply opens no claim.
    try:
        stamp = datetime.fromisoformat(cells[0].replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return None
    lane = claim_lane(line)
    if lane is None:
        return None
    return stamp, index, lane, cells[1], line.strip()


def ends_work(row: str) -> bool:
    """Whether a ledger row ends the work on the tree it names.

    True only for a TERMINAL row whose worktree cell is removed, pinned-removed or
    released, or whose outcome is done, merged or handed-off with no kept worktree.
    A kept worktree, a partial/blocked/failed outcome, a RELEASE row, or any other
    row type is False.
    """
    cells = [part.strip() for part in row.strip().strip("|").split("|")]
    if len(cells) < 3 or cells[1] != "TERMINAL":
        return False
    worktree_match = _WORKTREE_CELL.search(row)
    worktree = worktree_match[1] if worktree_match else ""
    if (
        worktree.startswith("removed:")
        or worktree.startswith("pinned-removed:")
        or worktree == "released"
        or worktree.startswith("released:")
    ):
        return True
    if worktree.startswith("kept"):
        return False
    outcome_match = _OUTCOME_CELL.search(row)
    outcome = outcome_match[1] if outcome_match else ""
    return outcome in _ENDING_OUTCOMES


def _fold(
    rows: Iterable[tuple[datetime, int, str, str, str]],
) -> tuple[dict[str, list[str]], list[str], list[str], dict[str, datetime]]:
    """Fold ledger rows in order into (open claims per lane, closed rows, finished rows, last stamp per lane).

    Every row refreshes the lane's heartbeat stamp; a CLAIM opens its line on the lane;
    a TERMINAL or RELEASE closes the lane's open claims. A closing row extends finished
    with the closed claims only when ends_work is True for that row.
    """
    claims: dict[str, list[str]] = {}
    closed: list[str] = []
    finished: list[str] = []
    latest: dict[str, datetime] = {}
    for stamp, _, lane, kind, line in sorted(rows):
        latest[lane] = stamp
        if kind == "CLAIM":
            claims.setdefault(lane, []).append(line)
        elif kind in _CLOSING:
            popped = claims.pop(lane, [])
            closed.extend(popped)
            if ends_work(line):
                finished.extend(popped)
    return claims, closed, finished, latest


def _live(
    claims: dict[str, list[str]],
    heartbeat: dict[str, datetime],
    now: datetime,
    quiet_hours: float,
) -> tuple[str, ...]:
    """The open claim rows of every lane whose last row is still within the quiet window."""
    return tuple(
        row
        for lane, entries in claims.items()
        if (now - heartbeat[lane]).total_seconds() < quiet_hours * 3600
        for row in entries
    )


def live_claims(text: str, now: datetime, quiet_hours: float) -> tuple[str, ...]:
    """Keep each CLAIM until a later terminal, using all lane rows as heartbeat."""
    rows = [
        row
        for index, line in enumerate(text.splitlines())
        if (row := _row(line, index)) is not None
    ]
    claims, _, _, latest = _fold(rows)
    return _live(claims, latest, now, quiet_hours)


def claim_names_tree(path: Path, root: Path, row: str) -> bool:
    """One claim row names the tree's path or, as a whole word, its ticket directory."""
    ticket = path.name if path.parent == root else path.parent.name
    word = re.compile(rf"(?<![\w-]){re.escape(ticket)}(?![\w-])")
    return str(path) in row or bool(word.search(row))


def claim_maps_to_tree(path: Path, root: Path, row: str) -> bool:
    """A claim row names the tree, or its ticket cell is a ticket id the tree's directory carries.

    A successor lane's CLAIM names the ticket, not the predecessor's directory
    (OMN-1234-readings): the id in the directory name maps the tree to it.
    """
    if claim_names_tree(path, root, row):
        return True
    directory = path.name if path.parent == root else path.parent.name
    return bool(set(_TICKET_ID.findall(directory)) & set(_TICKET_CELL.findall(row)))


def claimed(path: Path, root: Path, claims: tuple[str, ...]) -> bool:
    """A live claim maps to the tree: it names its path or ticket directory, or its ticket."""
    return any(claim_maps_to_tree(path, root, row) for row in claims)


def _unread(reason: str) -> ModelLedgerRead:
    return ModelLedgerRead(fresh=False, reason=reason)


def read_ledger(
    path: Path,
    now: datetime,
    *,
    quiet_hours: float,
    max_age_hours: float,
    lookback_days: int,
    max_bytes: int,
) -> ModelLedgerRead:
    """Read the ledger fresh and bounded; anything short of that is unread, never an empty ledger.

    Unread (fresh False, with the reason) when the file is missing, empty, bigger than the
    byte bound with its rolled files, unreadable, frozen (its newest row older than
    max_age_hours: a rolled or stopped writer) or stamped in the future. A tree is never
    called unclaimed from an unread ledger. Rolled files of the last lookback_days days
    (archive/*.md beside the live file) are read first, so a lane that finished before the
    last roll still shows its TERMINAL.
    """
    try:
        if not path.exists():
            return _unread("missing")
        if not path.is_file():
            return _unread("unreadable")
        files = [path]
        archive = path.parent / "archive"
        if lookback_days and archive.is_dir():
            cutoff = (now - timedelta(days=lookback_days)).timestamp()
            files = [
                *(
                    rolled
                    for rolled in sorted(archive.glob("*.md"))
                    if rolled.is_file() and rolled.stat().st_mtime >= cutoff
                ),
                path,
            ]
        if sum(file.stat().st_size for file in files) > max_bytes:
            return _unread("too_large")
        if path.stat().st_size == 0:
            return _unread("empty")
        rows: list[tuple[datetime, int, str, str, str]] = []
        heartbeat: dict[str, datetime] = {}
        newest: datetime | None = None
        position = 0
        for file in files:
            with file.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    position += 1
                    row = _row(line, position)
                    if row is None:
                        continue
                    stamp, _, lane, kind, _ = row
                    newest = stamp if newest is None else max(newest, stamp)
                    heartbeat[lane] = max(heartbeat.get(lane, stamp), stamp)
                    if kind == "CLAIM" or kind in _CLOSING:
                        rows.append(row)
    except OSError:
        return _unread("unreadable")
    if newest is None:
        return _unread("empty")
    if (newest - now).total_seconds() > _SKEW_SECONDS:
        return _unread("clock_skew")
    if (now - newest).total_seconds() > max_age_hours * 3600:
        return _unread("frozen")
    claims, closed, finished, _ = _fold(rows)
    return ModelLedgerRead(
        fresh=True,
        live_claims=_live(claims, heartbeat, now, quiet_hours),
        open_claims=tuple(row for entries in claims.values() for row in entries),
        closed_claims=tuple(closed),
        finished_claims=tuple(finished),
    )


def fold_ledger_lines(
    lines: Iterable[str],
    now: datetime,
    *,
    quiet_hours: float,
) -> ModelLedgerRead:
    """Parse and fold ledger lines into a ModelLedgerRead without age, skew, size or empty checks.

    The caller owns those checks. Every line is parsed with _row (indexing from 1);
    the heartbeat per lane is the newest stamp among all parsed rows; only CLAIM and
    closing rows are kept for the fold; the result is a fresh ModelLedgerRead.
    """
    rows: list[tuple[datetime, int, str, str, str]] = []
    heartbeat: dict[str, datetime] = {}
    for index, line in enumerate(lines, start=1):
        row = _row(line, index)
        if row is None:
            continue
        stamp, _, lane, kind, _ = row
        heartbeat[lane] = max(heartbeat.get(lane, stamp), stamp)
        if kind == "CLAIM" or kind in _CLOSING:
            rows.append(row)
    claims, closed, finished, _ = _fold(rows)
    return ModelLedgerRead(
        fresh=True,
        live_claims=_live(claims, heartbeat, now, quiet_hours),
        open_claims=tuple(row for entries in claims.values() for row in entries),
        closed_claims=tuple(closed),
        finished_claims=tuple(finished),
    )


def owner_finished(path: Path, root: Path, read: ModelLedgerRead | None) -> bool:
    """A fresh ledger read shows the tree's owning lane finished.

    A CLAIM that names the tree (its path or ticket directory) was closed by a later TERMINAL,
    and no open CLAIM names it: a lane that claimed the tree again and never finished holds it.
    A successor lane that claimed only the ticket holds it too, but that is read from live_claims
    (a quiet claim on an umbrella ticket must not keep every tree under it for ever). An unread
    ledger shows nothing.
    """
    return bool(
        read is not None
        and read.fresh
        and any(claim_names_tree(path, root, row) for row in read.finished_claims)
        and not any(claim_names_tree(path, root, row) for row in read.open_claims)
    )
