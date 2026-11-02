# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A simulated markdown ledger that rolls, and records what the file guard resolves (OMN-21947).

The file guard resolves ``<ledger>:<line>`` against the live file as it is when the citing
row is appended: ``text.splitlines()[line - 1]``. This fixture keeps the live file across
rolls (the head goes to a split, the tail stays) and records, for every citing row, the
text the guard saw. Those recordings are the "recorded days" the citation table is judged
against.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

LIVE_NAME = "ROLLING_WORK_LEDGER.md"
BASE = datetime(2026, 10, 20, 0, 0, 0, tzinfo=UTC)


def stamp(minute: int) -> str:
    return (BASE + timedelta(minutes=minute)).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class RecordedCitation:
    """One citation as the guard resolved it when the citing row was appended."""

    citing_row: str
    path: str
    line: int
    generation: int
    guard_saw: str | None


@dataclass
class SimulatedLedger:
    root: Path
    preamble: tuple[str, ...] = (
        "**Companion surfaces:** an unrelated header",
        "",
        "---",
        "## section",
    )
    live_rows: list[str] = field(default_factory=list)
    generation: int = 0
    minute: int = 0
    splits: dict[str, list[str]] = field(default_factory=dict)
    recorded: list[RecordedCitation] = field(default_factory=list)

    def guard_line(self, number: int) -> str | None:
        """What ``text.splitlines()[number - 1]`` is for the live file now, if it is a row."""
        lines = [*self.preamble, *self.live_rows]
        if not 1 <= number <= len(lines):
            return None
        text = lines[number - 1]
        return text if number > len(self.preamble) else None

    def append(self, kind: str, lane: str, *cells: str) -> str:
        self.minute += 1
        row = " | ".join([stamp(self.minute), kind, f"lane={lane}", *cells, "text"])
        for cell in cells:
            key, _, value = cell.partition("=")
            if key in {"acks", "amends", "consent", "extends"} and ":" in value:
                path, _, number = value.rpartition(":")
                # the guard resolves only the live file; a split citation is the table's own
                if number.isdigit() and (
                    path == "<ledger>" or path.endswith(LIVE_NAME)
                ):
                    self.recorded.append(
                        RecordedCitation(
                            citing_row=row,
                            path=path,
                            line=int(number),
                            generation=self.generation,
                            guard_saw=self.guard_line(int(number)),
                        )
                    )
        self.live_rows.append(row)
        return row

    def roll(self, *, keep: int, split_name: str) -> None:
        """Move all but the last ``keep`` rows to ``split_name`` (appending a block)."""
        rolled = self.live_rows[: len(self.live_rows) - keep]
        self.live_rows = self.live_rows[len(self.live_rows) - keep :]
        at = BASE + timedelta(minutes=self.minute, seconds=1)
        stamp_at = at.strftime("%Y-%m-%dT%H:%M:%SZ")
        banner = f"## Rolled from {LIVE_NAME} at {stamp_at} -- {len(rolled)} rows"
        header = (
            "<!-- ledger-roll-archive "
            + json.dumps(
                {
                    "source": f"docs/tracking/{LIVE_NAME}",
                    "section_heading": "## section",
                    "rolled_at": stamp_at,
                    "entries": len(rolled),
                    "first_rolled_heading": rolled[0],
                    "last_rolled_heading": rolled[-1],
                }
            )
            + " -->"
        )
        block = [header, banner, "", *rolled]
        existing = self.splits.get(split_name)
        self.splits[split_name] = block if existing is None else [*existing, "", *block]
        self.generation += 1
        self.minute += 2

    def write(self) -> Path:
        archive = self.root / "archive"
        archive.mkdir(parents=True, exist_ok=True)
        for name, lines in self.splits.items():
            (archive / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
        live = self.root / LIVE_NAME
        live.write_text(
            "\n".join([*self.preamble, *self.live_rows]) + "\n", encoding="utf-8"
        )
        return live


def two_day_ledger(root: Path) -> SimulatedLedger:
    """Three generations: citations into the live head, a kept tail, a split and the present."""
    sim = SimulatedLedger(root)
    for n in range(12):
        sim.append("STATUS", f"lane-{n}")
    sim.append("RULING", "orchestrator", "ticket=OMN-1")  # generation 0, live line 17
    sim.append(
        "ACK", "lane-a", f"acks=docs/tracking/{LIVE_NAME}:{len(sim.preamble) + 13}"
    )
    sim.append("STATUS", "lane-b", "amends=<ledger>:2")  # a preamble line: no row
    for n in range(10):
        sim.append("STATUS", f"lane-g0-{n}")
    sim.roll(keep=4, split_name="ROLLING_WORK_LEDGER_2026-10-20-split.md")
    # generation 1: the kept tail leads the live file again, so old line numbers moved
    sim.append("ACK", "lane-c", f"acks={LIVE_NAME}:{len(sim.preamble) + 2}")
    sim.append(
        "ACK", "lane-d", f"acks=docs/tracking/{LIVE_NAME}:{len(sim.preamble) + 5}"
    )
    sim.append(
        "ACK",
        "lane-e",
        f"extends=docs/tracking/archive/ROLLING_WORK_LEDGER_2026-10-20-split.md:{3 + 13}",
    )
    for n in range(8):
        sim.append("STATUS", f"lane-g1-{n}")
    sim.roll(keep=3, split_name="ROLLING_WORK_LEDGER_2026-10-20-split.md")
    # generation 2: the live file as it stands
    sim.append("ACK", "lane-f", f"acks={LIVE_NAME}:{len(sim.preamble) + 3}")
    sim.append(
        "ACK", "lane-g", f"acks=<ledger>:{len(sim.preamble) + 99}"
    )  # past the end: no row
    sim.append("STATUS", "lane-h")
    return sim
