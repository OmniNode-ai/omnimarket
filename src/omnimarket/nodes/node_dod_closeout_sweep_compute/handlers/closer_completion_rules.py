# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The closer's completion receipt and morning health row (OMN-20675).

Ported rule for rule from the standalone closeout-completion module the closer ran in its
Report phase. The caller passes the ledger rows (archive rolls first, the live ledger
last) and the run summary; these functions read nothing. Archive rolls and the live ledger
are one history, so a run key counts once, even after a replay.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from omnimarket.models.dod_closeout_sweep import (
    EnumCloserHealth,
    ModelDodCloseoutZeroFlipAlert,
    ModelDodCloseoutZeroFlipReading,
    ModelRunSummary,
)

LANE = "dod-closeout-sweep"

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


def held_reason_counts(summary: ModelRunSummary) -> dict[str, int]:
    """Why the run's tickets did not flip, counted by class (OMN-20718).

    Unmet criteria and refused bindings by refusal class (the same classes top_reason votes
    over), then each refused done-flip, each ticket held for an amendment and each ticket
    behind the candidate budget. Reads the run summary only.
    """
    counts: Counter[str] = Counter()
    for ticket in summary.open:
        for unmet in ticket.unmet:
            match = _REFUSED_BINDING.match(unmet.reason)
            counts[f"binding-refused:{match[1]}" if match else cell(unmet.reason)] += 1
    if not counts:
        counts.update(f"binding-refused:{r.refused}" for r in summary.refused_bindings)
    if summary.flip_refused:
        counts["done-flip-refused"] += len(summary.flip_refused)
    if summary.held_for_amendment:
        counts["held-for-amendment"] += len(summary.held_for_amendment)
    if summary.deferred_count:
        counts["candidate-budget"] += summary.deferred_count
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def zero_flip_reading(
    summary: ModelRunSummary, stamp: str
) -> ModelDodCloseoutZeroFlipReading | None:
    """The typed reading of a run that closed zero tickets; None for a run that flipped one.

    A dry run (apply false) flips nothing by design and reads as no zero-flip. The reading
    never decides whether a ticket may close: that stays with the acceptor and dod_verify.
    """
    if summary.flipped or not summary.apply:
        return None
    candidates = sum(
        1 for sources in summary.candidate_sources.values() if "merged" in sources
    )
    return ModelDodCloseoutZeroFlipReading(
        lane=LANE,
        run_key=summary.run_key,
        observed_at=stamp,
        candidate_count=candidates,
        selected_count=len(summary.selected),
        open_count=len(summary.open),
        held_count=len(summary.held_for_amendment),
        deferred_count=summary.deferred_count,
        reason_counts=held_reason_counts(summary),
        top_reason=cell(top_reason(summary)),
    )


def zero_flip_row(reading: ModelDodCloseoutZeroFlipReading) -> str:
    """The alarm row of a zero-flip reading: a STATUS row, the run exits 0 (OMN-20718)."""
    counts = cell(json.dumps(reading.reason_counts, sort_keys=True))
    return (
        f"{reading.observed_at} | STATUS | lane={reading.lane} | run={reading.run_key} | "
        f"actor=launchd:{reading.lane} | ticket=OMN-20655 | state=zero-flip-reading | "
        f"exit={reading.exit_code} | candidates={reading.candidate_count} | "
        f"selected={reading.selected_count} | open={reading.open_count} | "
        f"held={reading.held_count} | deferred={reading.deferred_count} | "
        f"top_reason={reading.top_reason} | held_reasons={counts} | "
        "this run closed no ticket: a working closer, not a failed run"
    )


def completion_rows(
    receipts: Sequence[Receipt],
    summary: ModelRunSummary,
    stamp: str,
    report: str,
    *,
    threshold: int,
) -> tuple[list[str], int, str]:
    """This run's rows, streak and reason: at the contract's threshold and every later
    zero, add an alarm FRICTION row before the TERMINAL that names it."""
    run = summary.run_key
    flipped = len(summary.flipped)
    # A force replay replaces this run's receipt; it cannot extend its own streak.
    length = 0 if flipped else streak([r for r in receipts if r.run != run]) + 1
    reason = cell(top_reason(summary))
    common = f"lane={LANE} | run={run} | actor=launchd:{LANE}"
    health = f"zero_flip_streak={length} | top_deferral_reason={reason}"
    rows: list[str] = []
    reading = zero_flip_reading(summary, stamp)
    if reading is not None:
        rows.append(zero_flip_row(reading))
    friction = "none"
    if length >= threshold:
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
    receipts: Sequence[Receipt], *, threshold: int
) -> tuple[EnumCloserHealth, str, int | None, str | None]:
    """The morning report's row: RED from the contract's threshold of zero-flip runs on."""
    if not receipts:
        return (
            EnumCloserHealth.UNKNOWN,
            f"| {LANE} | UNKNOWN | no completed receipts |",
            None,
            None,
        )
    length = streak(receipts)
    latest = receipts[-1]
    verdict = EnumCloserHealth.RED if length >= threshold else EnumCloserHealth.GREEN
    reason = cell(latest.reason)
    return (
        verdict,
        f"| {LANE} | {verdict.value} | zero-flip streak={length}; "
        f"top deferral reason={reason}; last run={latest.run} |",
        length,
        reason,
    )


def zero_flip_alert(
    summary: ModelRunSummary,
    length: int,
    reason: str,
    stamp: str,
    *,
    threshold: int,
) -> ModelDodCloseoutZeroFlipAlert | None:
    """OMN-21146: alert at the contract's threshold while tickets remain deferred."""
    if length < threshold or summary.deferred_count == 0:
        return None
    return ModelDodCloseoutZeroFlipAlert(
        lane=LANE,
        run_key=summary.run_key,
        zero_flip_streak=length,
        threshold_runs=threshold,
        deferred_count=summary.deferred_count,
        top_deferral_reason=reason,
        observed_at=stamp,
    )
