# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build one subject per live lane from its CLAIM, STATUS and TERMINAL rows.

Phases follow the stall alerts plan, section 2.1. Rows are ordered by ``row_ts``
then ``row_id``, so equal stamps select the same row on every read. The current
phase is the latest of ``placed``, ``claimed`` and ``pr_open`` whose entering row
exists, entered at that row's stamp; a later row that is not an entering row never
resets the clock.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta
from fnmatch import fnmatchcase

from omnimarket.models.model_work_progress import (
    ModelWorkProgressSubject,
)
from omnimarket.nodes.node_work_progress_alert_effect.models import (
    ModelIgnoreList,
    ModelLedgerRow,
)

PLACED = "placed"
CLAIMED = "claimed"
PR_OPEN = "pr_open"
_PHASE_ORDER = (PLACED, CLAIMED, PR_OPEN)
_CELL_SEPARATOR = " | "
_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
_CLAIM, _STATUS, _TERMINAL = "CLAIM", "STATUS", "TERMINAL"


def parse_cells(raw_row: str) -> dict[str, str]:
    """Return the ``key=value`` cells of a ledger row; the first cell of a key wins."""
    cells: dict[str, str] = {}
    for cell in raw_row.split(_CELL_SEPARATOR):
        key, separator, value = cell.strip().partition("=")
        if separator and _KEY.fullmatch(key) and key not in cells:
            cells[key] = value.strip()
    return cells


def _sorted(rows: Iterable[ModelLedgerRow]) -> list[ModelLedgerRow]:
    return sorted(rows, key=lambda row: (row.row_ts, row.row_id))


def _ignored(lane: str, rows: list[ModelLedgerRow], ignore: ModelIgnoreList) -> bool:
    if any(fnmatchcase(lane, glob) for glob in ignore.lanes):
        return True
    actors = {parse_cells(row.raw_row).get("actor", "") for row in rows}
    return any(
        fnmatchcase(actor, glob) for actor in actors if actor for glob in ignore.actors
    )


def _owner(lane: str, earliest_claim: ModelLedgerRow) -> str:
    return parse_cells(earliest_claim.raw_row).get("parent") or lane


def _lane_subject(
    lane: str,
    rows: list[ModelLedgerRow],
    *,
    window_start: datetime,
    ignore: ModelIgnoreList,
) -> ModelWorkProgressSubject | None:
    ordered = _sorted(rows)
    claims = [row for row in ordered if row.row_type == _CLAIM]
    if not claims or claims[0].row_ts < window_start:
        return None
    if any(row.row_type == _TERMINAL for row in ordered):
        return None
    if _ignored(lane, ordered, ignore):
        return None
    working = [row for row in ordered if row.row_type in (_CLAIM, _STATUS)]
    placed = next((r for r in claims if parse_cells(r.raw_row).get("host")), None)
    entering: dict[str, ModelLedgerRow] = {}
    if placed is not None:
        entering[PLACED] = placed
    claimed = next((r for r in working if r is not placed), None)
    if claimed is not None:
        entering[CLAIMED] = claimed
    pr_row = next((r for r in working if parse_cells(r.raw_row).get("pr")), None)
    if pr_row is not None:
        entering[PR_OPEN] = pr_row
    phase = next((p for p in reversed(_PHASE_ORDER) if p in entering), None)
    if phase is None:
        return None
    row = entering[phase]
    return ModelWorkProgressSubject(
        lane=lane,
        phase=phase,
        entered_at=row.row_ts,
        entering_row_id=row.row_id,
        owner=_owner(lane, claims[0]),
    )


def gather_subjects(
    rows: Iterable[ModelLedgerRow],
    *,
    now: datetime,
    lookback_seconds: int,
    ignore: ModelIgnoreList,
) -> tuple[ModelWorkProgressSubject, ...]:
    """One subject per lane whose earliest CLAIM lies inside the lookback.

    ``rows`` carries every CLAIM, STATUS and TERMINAL row of each such lane with
    no lower bound on stamps, so a lane's entering rows do not move with the
    window: the lane keeps the same rows or leaves when its earliest CLAIM ages
    out. Lanes are returned in lane-id order.
    """
    window_start = now - timedelta(seconds=lookback_seconds)
    by_lane: dict[str, list[ModelLedgerRow]] = defaultdict(list)
    for row in rows:
        lane = row.row_lane
        if lane and "|" not in lane and row.row_type in (_CLAIM, _STATUS, _TERMINAL):
            by_lane[lane].append(row)
    subjects = (
        _lane_subject(lane, lane_rows, window_start=window_start, ignore=ignore)
        for lane, lane_rows in sorted(by_lane.items())
    )
    return tuple(subject for subject in subjects if subject is not None)


__all__ = ["CLAIMED", "PLACED", "PR_OPEN", "gather_subjects", "parse_cells"]
