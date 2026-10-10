# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ledger claim reading shared by worktree reconcile and worktree lease."""

import re
from datetime import datetime
from pathlib import Path

_LANE = re.compile(r"(?:^|[\s|])lane=([^\s|]+)")


def claim_lane(row: str) -> str | None:
    """The lane a ledger row names, or None."""
    match = _LANE.search(row)
    return match[1] if match else None


def claim_stamp(row: str) -> str:
    """The first cell of a ledger row, the stamp a RELEASE row cites in `re=`."""
    return row.strip().strip("|").split("|")[0].strip()


def live_claims(text: str, now: datetime, quiet_hours: float) -> tuple[str, ...]:
    """Keep each CLAIM until a later terminal, using all lane rows as heartbeat."""
    rows: list[tuple[datetime, int, str, str, str]] = []
    for index, line in enumerate(text.splitlines()):
        cells = [part.strip() for part in line.strip().strip("|").split("|")]
        if len(cells) < 3 or not re.match(r"\d{4}-\d{2}-\d{2}T", cells[0]):
            continue
        # A shared ledger carries legacy and malformed rows. One bad row must not
        # make the whole ledger unreadable (that would keep every tree forever);
        # a row that cannot be dated or attributed simply opens no claim.
        try:
            stamp = datetime.fromisoformat(cells[0].replace("Z", "+00:00"))
        except ValueError:
            continue
        if stamp.tzinfo is None:
            continue
        lane = claim_lane(line)
        if lane is None:
            continue
        rows.append((stamp, index, lane, cells[1], line))
    claims: dict[str, list[str]] = {}
    latest: dict[str, datetime] = {}
    for stamp, _, lane_name, kind, text_row in sorted(rows):
        latest[lane_name] = stamp
        if kind == "CLAIM":
            claims.setdefault(lane_name, []).append(text_row)
        elif kind in ("TERMINAL", "RELEASE"):
            claims[lane_name] = []
    return tuple(
        row
        for lane, entries in claims.items()
        if (now - latest[lane]).total_seconds() < quiet_hours * 3600
        for row in entries
    )


def claim_names_tree(path: Path, root: Path, row: str) -> bool:
    """One claim row names the tree's path or, as a whole word, its ticket directory."""
    ticket = path.name if path.parent == root else path.parent.name
    word = re.compile(rf"(?<![\w-]){re.escape(ticket)}(?![\w-])")
    return str(path) in row or bool(word.search(row))


def claimed(path: Path, root: Path, claims: tuple[str, ...]) -> bool:
    """A live claim names the tree's path or, as a whole word, its ticket directory."""
    return any(claim_names_tree(path, root, row) for row in claims)
