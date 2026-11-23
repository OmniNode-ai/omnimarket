# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fill's work, read from live state in goal order.

Replaces the stored approved-work list (deleted by a repository retirement) with three declared sources, read in
this order, each as rows the tick already takes (``id``, ``kind``, ``ticket``, ``repo``, ``goal``, ``acceptance_check``
and, for a pull request, ``pr``):

1. the integration ledger's NOT RUNNING lines no lane owns (a merged change that runs nowhere),
2. the red pull requests of the PR watcher's state in the goal repositories,
3. the open tickets of the Linear snapshot.

A source that is declared and absent, unreadable or older than its bound raises ``LiveWorkSourceError`` naming it: the
pass refuses, it never reads an empty list as green. A ticket two sources name is placed once, by the higher source. Where
a line or record names no ticket, it is counted in ``skipped`` and not placed (the tick launches lanes by ticket).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..models.model_lab_fill_work_sources import (
    ModelLabFillWatcherSource,
    ModelLabFillWorkSource,
    ModelLabFillWorkSources,
)
from .local_lab_fill_enumerate_facts import WatcherSprintPrs
from .protocol_lab_fill_effect import LabFillPortError

SOURCE_LEDGER = "integration-ledger"
SOURCE_WATCHER = "pr-watcher"
SOURCE_TICKETS = "goal-tickets"

_TICKET = re.compile(r"OMN-[0-9]+")
_LEDGER_LINE = re.compile(r"^- (?P<repo>\S+) (?P<number>[0-9]+|\(no number\))$")
_OPEN_STATUS_TYPES = ("started", "unstarted")
_TEXT_CAP = 300
_SNAPSHOT_STATE = "linear-snapshot-state.json"


class LiveWorkSourceError(LabFillPortError):
    """A declared source that cannot be read: the source's name and the reason, never an empty read.

    ``age_s`` is the source's age when it was read and found stale, ``absent`` is True when there was nothing to age (no file),
    ``bound_s`` is the bound the read held it to. The pass writes them in its MISSING line.
    """

    def __init__(
        self,
        source: str,
        reason: str,
        *,
        age_s: int | None = None,
        bound_s: int | None = None,
        absent: bool = False,
    ) -> None:
        super().__init__(f"{source}: {reason}")
        self.source = source
        self.reason = reason
        self.age_s = age_s
        self.bound_s = bound_s
        self.absent = absent


@dataclass(frozen=True)
class LiveWorkRead:
    """One source's read: its rows in order and a count of what it read but did not place, by reason."""

    source: str
    rows: tuple[dict[str, object], ...]
    skipped: dict[str, int] = field(default_factory=dict)


def _line(text: str) -> str:
    return " ".join(text.split())[:_TEXT_CAP]


def _check_fresh(source: str, path: Path, bound_s: int, now: float) -> None:
    try:
        age = now - path.stat().st_mtime
    except OSError as exc:
        raise LiveWorkSourceError(
            source, type(exc).__name__, bound_s=bound_s, absent=True
        ) from exc
    if age > bound_s:
        raise LiveWorkSourceError(
            source,
            f"{path.name} is {int(age)}s old, older than its {bound_s}s bound",
            age_s=int(age),
            bound_s=bound_s,
        )


def _read_text(source: str, path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise LiveWorkSourceError(source, type(exc).__name__) from exc


def read_integration_ledger(
    source: ModelLabFillWorkSource, *, now: float
) -> LiveWorkRead:
    """The NOT RUNNING lines whose owner is NONE and whose title names a ticket, oldest merge first (the file's order)."""
    path = Path(source.path)
    _check_fresh(SOURCE_LEDGER, path, source.max_age_seconds, now)
    lines = _read_text(SOURCE_LEDGER, path).splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.startswith("## NOT RUNNING")), None
    )
    if start is None:
        raise LiveWorkSourceError(SOURCE_LEDGER, "no NOT RUNNING section")
    rows: list[dict[str, object]] = []
    skipped: dict[str, int] = {}
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        cells = [cell.strip() for cell in line.split(" | ")]
        match = _LEDGER_LINE.match(cells[0]) if len(cells) >= 3 else None
        if match is None:
            continue
        owner = next(
            (c[len("owner:") :].strip() for c in cells if c.startswith("owner:")), ""
        )
        if not owner.startswith("NONE"):
            skipped["owned"] = skipped.get("owned", 0) + 1
            continue
        tickets = _TICKET.findall(cells[1])
        if not tickets:
            skipped["no-ticket"] = skipped.get("no-ticket", 0) + 1
            continue
        repo, number = match["repo"], match["number"]
        number_id = "nonumber" if number.startswith("(") else number
        gap = next(
            (c[len("gap:") :].strip() for c in cells if c.startswith("gap:")), ""
        )
        rows.append(
            {
                "id": f"nr-{repo}-{number_id}",
                "kind": "wiring",
                "ticket": tickets[0],
                "repo": repo,
                "goal": _line(
                    f"Make the merged change {repo} {number} run where it runs: {gap}"
                ),
                "acceptance_check": _line(
                    f"One run of {repo} {number} is read, and its integration ledger line no longer reads NOT RUNNING"
                ),
            }
        )
    return LiveWorkRead(SOURCE_LEDGER, tuple(rows), skipped)


def read_red_prs(
    source: ModelLabFillWatcherSource, repos: Sequence[str], *, now: float
) -> LiveWorkRead:
    """Each open, undrafted, unqueued pull request of the goal repositories whose CI verdict for its head is RED."""
    reader = WatcherSprintPrs(
        state_path=Path(source.path),
        max_tick_age_min=source.max_tick_age_seconds / 60,
        max_resync_age_min=source.max_resync_age_seconds / 60,
        now=lambda: now,
    )
    try:
        prs = reader(repos)
    except LabFillPortError as exc:
        raise LiveWorkSourceError(
            SOURCE_WATCHER,
            str(exc),
            bound_s=source.max_tick_age_seconds,
            absent=not Path(source.path).exists(),
        ) from exc
    rows: list[dict[str, object]] = []
    skipped: dict[str, int] = {}
    for pr in prs:
        if not (pr.head_read and pr.ci_head_matches and pr.ci_verdict.upper() == "RED"):
            continue
        tickets = _TICKET.findall(pr.title)
        if not tickets:
            skipped["no-ticket"] = skipped.get("no-ticket", 0) + 1
            continue
        rows.append(
            {
                "id": f"pr-{pr.repo}-{pr.number}",
                "kind": "pr-red",
                "ticket": tickets[0],
                "repo": pr.repo,
                "pr": f"{pr.repo}#{pr.number}",
                "goal": _line(
                    f"Make the red CI of {pr.repo}#{pr.number} green: {pr.title}"
                ),
                "acceptance_check": _line(
                    f"The required checks of {pr.repo}#{pr.number} read green on its new head"
                ),
            }
        )
    return LiveWorkRead(SOURCE_WATCHER, tuple(rows), skipped)


def _newest_snapshot(path: Path, bound_s: int) -> Path:
    """A declared directory holds dated snapshots (``linear-snapshot-<date>.json``): the newest by modification time."""
    if not path.is_dir():
        return path
    files = sorted(
        (f for f in path.glob("linear-snapshot-*.json") if f.name != _SNAPSHOT_STATE),
        key=lambda f: f.stat().st_mtime,
    )
    if not files:
        raise LiveWorkSourceError(
            SOURCE_TICKETS,
            f"no snapshot file in {path.name}",
            bound_s=bound_s,
            absent=True,
        )
    return files[-1]


def read_goal_tickets(source: ModelLabFillWorkSource, *, now: float) -> LiveWorkRead:
    """The open (started or unstarted) tickets of the Linear snapshot, in the snapshot's order.

    The declared path is the snapshot file or the directory the snapshot node writes its dated files to.
    """
    path = _newest_snapshot(Path(source.path), source.max_age_seconds)
    _check_fresh(SOURCE_TICKETS, path, source.max_age_seconds, now)
    try:
        document = json.loads(_read_text(SOURCE_TICKETS, path))
    except ValueError as exc:
        raise LiveWorkSourceError(
            SOURCE_TICKETS, f"not JSON: {type(exc).__name__}"
        ) from exc
    issues = document.get("issues") if isinstance(document, dict) else None
    if not isinstance(issues, list):
        raise LiveWorkSourceError(SOURCE_TICKETS, "no issues list")
    rows: list[dict[str, object]] = []
    for issue in issues:
        if (
            not isinstance(issue, dict)
            or issue.get("statusType") not in _OPEN_STATUS_TYPES
        ):
            continue
        ticket, title = issue.get("id"), issue.get("title")
        if not (
            isinstance(ticket, str)
            and _TICKET.fullmatch(ticket)
            and isinstance(title, str)
        ):
            continue
        rows.append(
            {
                "id": f"ticket-{ticket}",
                "kind": "ticket",
                "ticket": ticket,
                "repo": "",
                "goal": _line(f"Do the work of {ticket}: {title}"),
                "acceptance_check": _line(
                    f"Every acceptance criterion of {ticket} is met and read back from live state"
                ),
            }
        )
    return LiveWorkRead(SOURCE_TICKETS, tuple(rows), {})


def read_live_work(
    sources: ModelLabFillWorkSources, repos: Sequence[str], *, now: float
) -> tuple[LiveWorkRead, ...]:
    """Every declared source in goal order; a ticket named twice is placed by the first source that names it.

    Raises LiveWorkSourceError for the first declared source that cannot be read.
    """
    reads: list[LiveWorkRead] = []
    if sources.integration_ledger is not None:
        reads.append(read_integration_ledger(sources.integration_ledger, now=now))
    if sources.pr_watcher is not None:
        reads.append(read_red_prs(sources.pr_watcher, repos, now=now))
    if sources.goal_tickets is not None:
        reads.append(read_goal_tickets(sources.goal_tickets, now=now))
    seen: set[str] = set()
    deduped: list[LiveWorkRead] = []
    for read in reads:
        kept: list[dict[str, object]] = []
        skipped = dict(read.skipped)
        for row in read.rows:
            ticket = str(row["ticket"])
            if ticket in seen:
                skipped["named-higher"] = skipped.get("named-higher", 0) + 1
                continue
            seen.add(ticket)
            kept.append(row)
        deduped.append(LiveWorkRead(read.source, tuple(kept), skipped))
    return tuple(deduped)


class LiveWorkRows:
    """The tick's approved-work port over rows read once for this pass (the path the tick passes is a label, never opened)."""

    def __init__(self, rows: Sequence[Mapping[str, object]]) -> None:
        self._rows = tuple(dict(r) for r in rows)

    def depth(self, path: str) -> int | None:
        return len(self._rows)

    def list_rows(self, path: str) -> Sequence[Mapping[str, object]]:
        return list(self._rows)

    def row(self, path: str, row_id: str) -> Mapping[str, object]:
        matches = [r for r in self._rows if r.get("id") == row_id]
        if len(matches) != 1:
            raise LabFillPortError(f"APPROVED-ROW {row_id} missing or duplicate row")
        return matches[0]


__all__ = [
    "SOURCE_LEDGER",
    "SOURCE_TICKETS",
    "SOURCE_WATCHER",
    "LiveWorkRead",
    "LiveWorkRows",
    "LiveWorkSourceError",
    "read_goal_tickets",
    "read_integration_ledger",
    "read_live_work",
    "read_red_prs",
]
