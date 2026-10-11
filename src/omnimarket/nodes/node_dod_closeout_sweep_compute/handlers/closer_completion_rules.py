# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The closer's completion receipt and morning health row (OMN-20675).

Ported rule for rule from the standalone closeout-completion module the closer ran in its
Report phase. The caller passes the ledger rows (archive rolls first, the live ledger
last) and the run summary; these functions read nothing. Archive rolls and the live ledger
are one history, so a run key counts once, even after a replay.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from omnimarket.nodes.node_dod_closeout_sweep_compute.models.model_dod_closeout_sweep import (
    EnumCloserHealth,
    ModelRunSummary,
)

LANE = "dod-closeout-sweep"
THRESHOLD = 3

# The ledger row grammar the closer's history is written in.
_ROW = re.compile(
    r"^(?P<stamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) \| (?P<type>[^|]*?)\s*(?:\|(?P<rest>.*))?$"
)
_FIELD = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9_-]*)=(?P<value>.*)$", re.DOTALL)
_REFUSED_BINDING = re.compile(r"binding refused \(([^)]+)\)")


@dataclass(frozen=True)
class Receipt:
    stamp: str
    run: str
    flipped: int
    reason: str


@dataclass(frozen=True)
class _Row:
    stamp: str
    row_type: str
    cells: tuple[str, ...]

    def value(self, key: str) -> str | None:
        """The first non-empty value of `key`, or None."""
        for cell in self.cells:
            match = _FIELD.match(cell)
            if match and match.group("key").lower() == key:
                value = match.group("value").strip()
                if value:
                    return value
        return None

    def token(self, key: str) -> str | None:
        """The value only when its cell is exactly `key=<one token>`."""
        for cell in self.cells:
            match = _FIELD.match(cell)
            if match and match.group("key").lower() == key:
                value = match.group("value").strip()
                if value and not re.search(r"\s", value):
                    return value
        return None


def _parse_row(line: str) -> _Row | None:
    match = _ROW.match(line.rstrip("\n"))
    if match is None:
        return None
    rest = match.group("rest")
    cells = tuple(c.strip() for c in rest.split("|")) if rest is not None else ()
    return _Row(match.group("stamp"), match.group("type").strip(), cells)


def read_receipts(ledger_rows: Sequence[str]) -> list[Receipt]:
    """Completed sweep runs ordered by completion stamp, one per run key.

    Claims, other lanes and unkeyed rows do not count. An unparseable sweep count is an
    error, never evidence that the closer is healthy.
    """
    runs: dict[str, Receipt] = {}
    for line in ledger_rows:
        row = _parse_row(line)
        if row is None or row.row_type != "TERMINAL" or row.token("lane") != LANE:
            continue
        run, count = row.token("run"), row.token("flipped")
        if not run:  # Older daily adjudications are not keyed launchd run receipts.
            continue
        if count is None or not count.isdecimal():
            raise ValueError(f"malformed closer completion at {row.stamp}")
        receipt = Receipt(
            row.stamp, run, int(count), row.value("top_deferral_reason") or "unknown"
        )
        if run not in runs or receipt.stamp >= runs[run].stamp:
            runs[run] = receipt
    return sorted(runs.values(), key=lambda r: (r.stamp, r.run))


def streak(receipts: Sequence[Receipt]) -> int:
    count = 0
    for receipt in reversed(receipts):
        if receipt.flipped:
            break
        count += 1
    return count


def cell(value: str) -> str:
    """Free text must never create extra ledger cells or rows."""
    return " ".join(value.replace("|", "/").split())


def top_reason(summary: ModelRunSummary) -> str:
    """Count unmet criteria; use refusal classes rather than variable binder prose.

    Budget-deferred candidates were never adjudicated, so they are a fallback, not
    votes that hide a binder or acceptor failure on the selected tickets.
    """
    counts: Counter[str] = Counter()
    for ticket in summary.open:
        for unmet in ticket.unmet:
            match = _REFUSED_BINDING.match(unmet.reason)
            counts[f"binding-refused:{match[1]}" if match else cell(unmet.reason)] += 1
    if not counts:
        counts.update(f"binding-refused:{r.refused}" for r in summary.refused_bindings)
    if counts:
        return sorted(counts, key=lambda r: (-counts[r], r))[0]
    if summary.flip_refused:
        return "done-flip-refused"
    if summary.held_for_amendment:
        return "held-for-amendment"
    if summary.deferred_count:
        return "candidate-budget"
    return "no-candidates" if not summary.selected else "no-applied-flip"


def completion_rows(
    receipts: Sequence[Receipt], summary: ModelRunSummary, stamp: str, report: str
) -> tuple[list[str], int, str]:
    """This run's rows, streak and reason: the third (and every later) zero adds an alarm
    FRICTION row before the TERMINAL that names it."""
    run = summary.run_key
    flipped = len(summary.flipped)
    # A force replay replaces this run's receipt; it cannot extend its own streak.
    length = 0 if flipped else streak([r for r in receipts if r.run != run]) + 1
    reason = cell(top_reason(summary))
    common = f"lane={LANE} | run={run} | actor=launchd:{LANE}"
    health = f"zero_flip_streak={length} | top_deferral_reason={reason}"
    rows: list[str] = []
    friction = "none"
    if length >= THRESHOLD:
        friction = f"{stamp} lane={LANE}"
        rows.append(
            f"{stamp} | FRICTION | {common} | existing=OMN-20655 | "
            f"class=zero-flip-streak | {health} | "
            "cost=est ~1.5 lane-hours (this run's declared scheduled closer budget)"
        )
    rows.append(
        f"{stamp} | TERMINAL | {common} | ticket=OMN-17427 | worktree=none | "
        f"selected={len(summary.selected)} | flipped={flipped} | "
        f"open={len(summary.open)} | deferred={summary.deferred_count} | "
        f"held={len(summary.held_for_amendment)} | {health} | friction={friction} | "
        f"{cell(report) if report else 'no report: nothing changed'}"
    )
    return rows, length, reason


def ground_state(
    receipts: Sequence[Receipt],
) -> tuple[EnumCloserHealth, str, int | None, str | None]:
    """The morning report's row for the closer: RED from the third zero-flip run on."""
    if not receipts:
        return (
            EnumCloserHealth.UNKNOWN,
            f"| {LANE} | UNKNOWN | no completed receipts |",
            None,
            None,
        )
    length = streak(receipts)
    latest = receipts[-1]
    verdict = EnumCloserHealth.RED if length >= THRESHOLD else EnumCloserHealth.GREEN
    reason = cell(latest.reason)
    return (
        verdict,
        f"| {LANE} | {verdict.value} | zero-flip streak={length}; "
        f"top deferral reason={reason}; last run={latest.run} |",
        length,
        reason,
    )
