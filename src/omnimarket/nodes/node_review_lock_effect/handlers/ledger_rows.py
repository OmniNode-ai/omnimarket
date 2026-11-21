# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reading ledger rows for the review lock: the row parser and the two row shapes."""

from __future__ import annotations

import re

from omnimarket.nodes.node_review_lock_effect.models.model_review_lock import (
    HEAD_CELL_LENGTH,
    ModelStatusRow,
)

LOCK_STATE = "review-lock"
LOCK_LANE_PREFIX = "reviewlock-"
VERDICT_LANE_PREFIX = "fable-"

_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_FIELD = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9_-]*)=(?P<value>.*)$", re.DOTALL)
_VERDICT_LANE = re.compile(r"^fable-(?P<repo>.+)-(?P<pr>\d+)-(?P<tag>[A-Za-z0-9]+)$")
_PR_CELL = re.compile(r"^(?P<repo>[A-Za-z0-9_.-]+)#(?P<pr>\d+)$")


def parse_row(line: str) -> ModelStatusRow | None:
    """Parse one ledger line into a row, or None for a line that is not a ledger row.

    The first occurrence of a repeated cell wins, as a row such as the 1310 review's carries
    ``via=`` twice. A cell that is not ``key=value`` ends the cells and starts the free text.
    """
    parts = [p.strip() for p in line.rstrip("\n").split(" | ")]
    if len(parts) < 3 or not _TS.match(parts[0]):
        return None
    cells: dict[str, str] = {}
    text: list[str] = []
    for part in parts[2:]:
        match = _FIELD.match(part)
        if match is None or text:
            text.append(part)
            continue
        cells.setdefault(match.group("key"), match.group("value").strip())
    return ModelStatusRow(
        ts=parts[0], row_type=parts[1], cells=cells, text=" | ".join(text)
    )


def row_pr(row: ModelStatusRow) -> tuple[str, int] | None:
    """The (repo, pull request number) a review or lock row is about, or None."""
    cell = _PR_CELL.match(row.cells.get("pr", ""))
    if cell is not None:
        return cell.group("repo"), int(cell.group("pr"))
    lane = _VERDICT_LANE.match(row.lane)
    if lane is not None:
        return lane.group("repo"), int(lane.group("pr"))
    return None


def row_head(row: ModelStatusRow) -> str:
    return row.cells.get("head", "").lower()[:HEAD_CELL_LENGTH]


def is_verdict_row(row: ModelStatusRow) -> bool:
    """A review's verdict: a STATUS row of lane fable-<repo>-<n>-<tag> with verdict= and head=."""
    return (
        row.row_type == "STATUS"
        and row.lane.startswith(VERDICT_LANE_PREFIX)
        and bool(row.cells.get("verdict"))
        and bool(row_head(row))
    )


def is_lock_row(row: ModelStatusRow) -> bool:
    """A review lock: a STATUS row with state=review-lock and a head= cell."""
    return (
        row.row_type == "STATUS"
        and row.cells.get("state") == LOCK_STATE
        and row.lane.startswith(LOCK_LANE_PREFIX)
        and bool(row_head(row))
    )


def verdict_tag(row: ModelStatusRow) -> str:
    lane = _VERDICT_LANE.match(row.lane)
    return lane.group("tag") if lane is not None else ""
