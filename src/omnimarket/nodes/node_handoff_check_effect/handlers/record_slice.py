# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Record a scrubbed ledger slice as a test fixture: ids and tickets only, no free text."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Sequence
from pathlib import Path

from omnimarket.nodes.node_handoff_check_effect.handlers.ledger_rows import (
    load_rows,
    parse_row,
    umbrella_tickets,
)
from omnimarket.nodes.node_handoff_check_effect.handlers.rulings import (
    dropped_decisions,
)
from omnimarket.nodes.node_handoff_check_effect.models import (
    EnumHandoffCommand,
    ModelHandoffCheckRequest,
    ModelHandoffCheckResult,
    ModelHandoffPolicy,
    ModelLedgerRow,
    fmt_ts,
    parse_ts,
)

KEEP_CELLS = (
    "lane",
    "ticket",
    "mechanism-ticket",
    "id",
    "re",
    "outcome",
    "to",
    "from",
    "needs",
    "until",
    "pr",
    "handed-off",
    "closes-CLAIM",
)
PATH_RE = re.compile(r"/(?:Users|Volumes|home)/[^\s|]+")
TICKET_RE = re.compile(r"[A-Z]{2,}-\d+")
PR_REPO_RE = re.compile(r"([A-Za-z0-9_.-]+)#(?=\d)")


class Aliases:
    """Stable stand-ins for names a public fixture must not carry."""

    def __init__(self, rows: Sequence[ModelLedgerRow]) -> None:
        def values(key: str) -> list[str]:
            return sorted({r.cells[key] for r in rows if r.cells.get(key)}, key=len)

        self.tickets = {
            t: f"TKT-{n:04d}" for n, t in enumerate(sorted(self._tickets(rows)), 1)
        }
        lanes = {x for k in ("lane", "from", "to") for x in values(k)}
        self.lanes = {x: f"lane-{n:04d}" for n, x in enumerate(sorted(lanes), 1)}
        self.order = sorted(self.lanes, key=len, reverse=True)
        repos = {
            m.group(1)
            for r in rows
            for k in ("pr", "handed-off")
            for m in PR_REPO_RE.finditer(r.cells.get(k, ""))
        }
        self.repos = {x: f"repo-{n:02d}" for n, x in enumerate(sorted(repos), 1)}

    @staticmethod
    def _tickets(rows: Sequence[ModelLedgerRow]) -> set[str]:
        return {m for r in rows for v in r.cells.values() for m in TICKET_RE.findall(v)}

    def apply(self, value: str) -> str:
        value = PATH_RE.sub("<path>", value)
        value = TICKET_RE.sub(lambda m: self.tickets.get(m.group(0), "TKT-0000"), value)
        for lane in self.order:
            value = value.replace(lane, self.lanes[lane])
        return PR_REPO_RE.sub(
            lambda m: f"{self.repos.get(m.group(1), 'repo-00')}#", value
        )


def scrub_row(r: ModelLedgerRow, cited: str, alias: Aliases) -> str:
    cells = [
        f"{k}={alias.apply(r.cells[k])[:200]}" for k in KEEP_CELLS if r.cells.get(k)
    ]
    if cited:
        cells.append(f"cites={cited}")
    return " | ".join(
        [fmt_ts(r.ts), r.type, "principal=operator", *cells, "text=[scrubbed]"]
    )


def record_slice(
    request: ModelHandoffCheckRequest, policy: ModelHandoffPolicy, ledger_path: str
) -> ModelHandoffCheckResult:
    if not (request.since and request.until and request.out):
        raise ValueError("record-slice: since, until and out are required")
    gate = policy.ruling_gate
    lo, hi = parse_ts(request.since), parse_ts(request.until)
    ledger = Path(ledger_path)
    rows = [
        r
        for r in load_rows(
            ledger,
            Path(request.archive_dir)
            if request.archive_dir
            else ledger.parent / "archive",
        )
        if lo <= r.ts <= hi
    ]
    decisions = [r for r in rows if r.type in gate.row_types]
    decision_ids = {id(r) for r in decisions}
    stamps = {fmt_ts(r.ts) for r in decisions}
    umbrella = umbrella_tickets(rows, gate, hi)
    keys = {t for r in decisions for t in r.tickets} - umbrella
    chosen: list[tuple[ModelLedgerRow, str]] = []
    # Enough claims to keep each umbrella ticket an umbrella, no more.
    floor = hi - dt.timedelta(days=gate.umbrella_scan_days)
    seen_lanes: dict[str, set[str]] = {t: set() for t in umbrella}
    for r in rows:
        cited = ",".join(sorted(s for s in stamps if s in r.body and s != fmt_ts(r.ts)))
        ticket = r.cells.get("ticket", "")
        keep_umbrella = (
            r.type == "CLAIM"
            and ticket in umbrella
            and r.ts >= floor
            and len(seen_lanes[ticket]) < gate.umbrella_min_lanes
        )
        if keep_umbrella:
            seen_lanes[ticket].add(r.cells.get("lane", ""))
        wanted = (
            id(r) in decision_ids
            or r.type in ("HOLD", "RELEASE")
            or keep_umbrella
            or (r.type == "CLAIM" and (ticket in keys or cited))
            or (r.type == "TERMINAL" and (ticket in keys or cited))
        )
        if wanted:
            chosen.append((r, cited))
    alias = Aliases([r for r, _ in chosen])
    kept = [scrub_row(r, c, alias) for r, c in chosen]
    Path(request.out).write_text("\n".join(kept) + "\n", encoding="utf-8")
    parsed = [x for x in (parse_row(k, "fixture") for k in kept) if x]
    dropped = dropped_decisions(parsed, policy, hi)
    return ModelHandoffCheckResult(
        command=EnumHandoffCommand.RECORD_SLICE,
        exit_code=0,
        output=[
            f"recorded {len(kept)} rows to {request.out}; {dropped.summary_line()}"
        ],
        dropped=dropped,
        written_path=request.out,
    )
