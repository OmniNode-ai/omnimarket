# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the two day windows of the day-over-day report out of ledger text (OMN-26212). Pure: text in, typed readings out.

Running and board states are cumulative: a change or check keeps its newest reading up to the end of the day, because the
ledger only records a reading when someone takes one. Cost is the day's own rows. A day with no ledger row at all, and a source with no reading
in it, read as not read (None), never as an empty day.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from omnimarket.handlers.work_ledger_text import ledger_row_stamp, split_ledger_rows
from omnimarket.models.model_day_over_day_report import (
    ModelBoardReading,
    ModelCostReading,
    ModelDayReadings,
    ModelRunningReading,
)

_PR = re.compile(r"\bpr=([A-Za-z0-9_.-]+#\d+)")
_INSTALLED = re.compile(r"\binstalled=([A-Za-z0-9_.-]+):(\S*)")
_CRIT = re.compile(r"\bcrit=(C\d+)\b")
_VERDICT = re.compile(r"\bverdict=(red|green)\b")
_MODEL = re.compile(r"\| model=([^\s|]+)")
_DELEGATED = re.compile(r"\bdelegated=(\d+)")
LEDGER_SOURCE = "ledger STATUS/TERMINAL rows"


def read_day(ledger_text: str | None, day: str) -> ModelDayReadings:
    """The readings for UTC date ``day`` (YYYY-MM-DD) from ledger text; every source None when the text is missing."""
    if ledger_text is None:
        return ModelDayReadings(label=day)
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    end = start + timedelta(days=1)
    rows = []
    for row in split_ledger_rows(ledger_text):
        try:
            rows.append((ledger_row_stamp(row), row))
        except ValueError:
            continue
    if not any(start <= at < end for at, _ in rows):
        return ModelDayReadings(label=day)
    running: dict[str, ModelRunningReading] = {}
    board: dict[str, ModelBoardReading] = {}
    calls: dict[str, tuple[int, datetime]] = {}
    for at, row in sorted(rows, key=lambda r: r[0]):
        if at >= end:
            break
        cells = row.split(" | ")
        kind = cells[1] if len(cells) > 1 else ""
        src = f"ledger:{at:%Y-%m-%dT%H:%M:%SZ} {kind}"
        if kind == "STATUS":
            pr, inst = _PR.search(row), _INSTALLED.search(row)
            if pr and inst:
                host = inst.group(1)
                running[pr.group(1)] = ModelRunningReading(
                    change=pr.group(1),
                    host=host,
                    state="not_running" if host == "none" else "running",
                    source=src,
                    read_at=at,
                )
            crit, verdict = _CRIT.search(row), _VERDICT.search(row)
            if crit and verdict:
                board[crit.group(1)] = ModelBoardReading(
                    check_id=crit.group(1),
                    verdict=verdict.group(1),
                    source=src,
                    read_at=at,
                )
        elif kind == "TERMINAL" and at >= start:
            model, n = _MODEL.search(row), _DELEGATED.search(row)
            if model and n and model.group(1) != "none":
                prior = calls.get(model.group(1), (0, at))[0]
                calls[model.group(1)] = (prior + int(n.group(1)), at)
    return ModelDayReadings(
        label=day,
        running=tuple(running.values()) or None,
        board=tuple(board.values()) or None,
        cost=tuple(
            ModelCostReading(
                engine=engine,
                calls=n,
                source="ledger TERMINAL delegated= cells",
                read_at=at,
            )
            for engine, (n, at) in sorted(calls.items())
        ),
    )
