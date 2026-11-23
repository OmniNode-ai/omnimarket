# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed rows of the rolling work ledger, read through the ledger-rows port."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Sequence

from omnimarket.nodes.node_handoff_check_effect.adapters.bus_ledger_rows import (
    MAX_WINDOW_HOURS,
)
from omnimarket.nodes.node_handoff_check_effect.models import (
    ModelLedgerRow,
    ModelRulingGatePolicy,
    fmt_ts,
    parse_ts,
)
from omnimarket.nodes.node_handoff_check_effect.protocols import ProtocolLedgerRows

ROW_RE = re.compile(r"^(20\d\d-\d\d-\d\dT\d\d:\d\d:\d\dZ) \| ([A-Z][A-Z-]*) \|")
LEDGER_REF = "ledger"
KV_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)=(.*)$")


def parse_row(line: str, ref: str) -> ModelLedgerRow | None:
    match = ROW_RE.match(line)
    if not match:
        return None
    cells: dict[str, str] = {}
    for cell in line.split(" | ")[2:]:
        kv = KV_RE.match(cell.strip())
        if kv:
            cells.setdefault(kv.group(1), kv.group(2).strip())
    return ModelLedgerRow(
        ts=parse_ts(match.group(1)),
        type=match.group(2),
        cells=cells,
        raw=line,
        ref=ref,
    )


class LedgerRowsUnreadableError(Exception):
    """The ledger-rows port gave no rows (unreadable or not moved); never read as an empty ledger."""


def load_rows(
    port: ProtocolLedgerRows, window_hours: int = MAX_WINDOW_HOURS
) -> list[ModelLedgerRow]:
    """The ledger's rows of the window through the port, oldest first, duplicates dropped."""
    answer = port.read_rows(window_hours)
    if answer.status not in ("ok", "stale"):
        raise LedgerRowsUnreadableError(
            f"ledger rows {answer.status}: {answer.reason or 'no reason given'}"
        )
    seen: set[str] = set()
    rows: list[ModelLedgerRow] = []
    for number, line in enumerate(answer.lines, 1):
        row = parse_row(line, f"{LEDGER_REF}:{number}")
        if row and row.raw not in seen:
            seen.add(row.raw)
            rows.append(row)
    rows.sort(key=lambda r: r.ts)
    return rows


def open_claims(rows: Sequence[ModelLedgerRow]) -> dict[str, ModelLedgerRow]:
    """lane -> its latest CLAIM, when no TERMINAL or CLOSEOUT of the lane follows it."""
    claim: dict[str, int] = {}
    closed: dict[str, int] = {}
    for index, row in enumerate(rows):
        lane = row.cells.get("lane", "")
        if not lane:
            continue
        if row.type == "CLAIM":
            claim[lane] = index
        elif row.type in ("TERMINAL", "CLOSEOUT"):
            closed[lane] = index
    return {lane: rows[i] for lane, i in claim.items() if closed.get(lane, -1) < i}


def holds_in_force(rows: Sequence[ModelLedgerRow]) -> list[ModelLedgerRow]:
    released = {r.cells.get("re", "") for r in rows if r.type == "RELEASE"}
    return [
        r
        for r in rows
        if r.type in ("HOLD", "HELD")
        and r.cells.get("id")
        and r.cells["id"] not in released
    ]


def umbrella_tickets(
    rows: Sequence[ModelLedgerRow], gate: ModelRulingGatePolicy, now: dt.datetime
) -> set[str]:
    """Tickets so many lanes claim that lane activity on them proves nothing about one ruling."""
    lo = now - dt.timedelta(days=gate.umbrella_scan_days)
    lanes: dict[str, set[str]] = {}
    for r in rows:
        if r.type == "CLAIM" and lo <= r.ts <= now and r.cells.get("ticket"):
            lanes.setdefault(r.cells["ticket"], set()).add(r.cells.get("lane", ""))
    return {t for t, ls in lanes.items() if len(ls) >= gate.umbrella_min_lanes}


def ref_url(row: ModelLedgerRow) -> str:
    return f"{row.ref} {fmt_ts(row.ts)} {row.type}"
