# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane inbox wait: poll the lane's inbox until a binding row or a stop order ends it (OMN-24459).

A running lane sitting in a long wait cannot see its inbox; the wait helper polls it on the lane's
behalf. One command is one wait: read the inbox through the injected source, decide, and if nothing
binding is there sleep the overlay's poll interval through the injected sleeper - never inline - on
the injected monotonic clock, until the budget is spent. The rows are the rolling ledger's own row
grammar, parsed the way the lane inbox guard parses a row. The first read
failing (or every read failing) is unreadable; a later single failure is recorded and the wait goes
on.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from omnimarket.models.lane_inbox_wait import (
    STAMP_RE,
    EnumInboxWaitOutcome,
    ModelInboxWaitRow,
    ModelLaneInboxWaitCommand,
    ModelLaneInboxWaitResult,
)
from omnimarket.nodes.node_lane_inbox_wait_effect.overlay import (
    ModelLaneInboxWaitOverlay,
)
from omnimarket.nodes.node_lane_inbox_wait_effect.protocols import (
    InboxUnreadableError,
    ProtocolInboxSource,
    ProtocolMonotonicClock,
    ProtocolSleeper,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"

_FAILURE_LIMIT = 300
# A leading tag some writers put before the stamp, e.g. ``MSG 3290: ``.
_LEADING_TAG = re.compile(r"^[A-Z][A-Z0-9_]* \d+: ")
# ``key=value`` with no space in the key; any other cell is the row's free text.
_KEY_VALUE = re.compile(r"^([^=\s]+)=(.*)$", re.DOTALL)

_ACK = "ACK"
_RELEASE = "RELEASE"
_HOLD = "HOLD"
_MSG = "MSG"


class WaitBudgetRefusedError(ValueError):
    """The command's budget is above the overlay's max_wait_seconds; the wait is refused."""


@dataclass(frozen=True)
class ParsedInboxRow:
    """One row line as the wait reads it: its stamp, its type, its ``key=value`` cells, its free text."""

    stamp: str
    row_type: str
    cells: Mapping[str, str]
    text: str


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


def parse_inbox_row(line: str) -> ParsedInboxRow | None:
    """One row parsed as ``inbox_parse_rows`` parses it: a leading tag stripped, split on `` | ``;
    the first cell is a stamp, the second the row type, every later cell with an ``=`` and no space
    in its key is a ``key=value`` (first occurrence wins), and the rest is the free text. A line
    that is not a row parses as None."""
    row = _LEADING_TAG.sub("", line.strip(), count=1)
    cells = [cell.strip() for cell in row.split(" | ")]
    if len(cells) < 2 or STAMP_RE.match(cells[0]) is None or not cells[1]:
        return None
    values: dict[str, str] = {}
    text: list[str] = []
    for cell in cells[2:]:
        matched = _KEY_VALUE.match(cell)
        if matched is None:
            text.append(cell)
        elif matched.group(1) not in values:
            values[matched.group(1)] = matched.group(2)
    return ParsedInboxRow(cells[0], cells[1], values, " | ".join(text))


def parse_inbox_rows(lines: tuple[str, ...]) -> tuple[ParsedInboxRow, ...]:
    """The rows of one read, in line order; lines that are not rows are dropped."""
    parsed = (parse_inbox_row(line) for line in lines)
    return tuple(row for row in parsed if row is not None)


class HandlerLaneInboxWait:
    """Poll the lane's inbox until a binding row, a stop order or the budget ends the wait."""

    def __init__(
        self,
        *,
        overlay: ModelLaneInboxWaitOverlay,
        source: ProtocolInboxSource,
        sleeper: ProtocolSleeper,
        clock: ProtocolMonotonicClock,
    ) -> None:
        self._overlay = overlay
        self._source = source
        self._sleeper = sleeper
        self._clock = clock

    def handle(self, command: ModelLaneInboxWaitCommand) -> ModelLaneInboxWaitResult:
        if command.seconds > self._overlay.max_wait_seconds:
            raise WaitBudgetRefusedError(
                f"the wait's budget {command.seconds:g}s is above the overlay's "
                f"max_wait_seconds {self._overlay.max_wait_seconds:g}s"
            )
        started = self._clock.monotonic()
        deadline = started + command.seconds
        polls = 0
        read_failures: list[str] = []
        while True:
            polls += 1
            try:
                lines = self._source.read(lane=command.lane)
            except InboxUnreadableError as exc:
                if polls == 1:  # the first read failing is the whole wait unreadable
                    reason = self._reason(exc)
                    return self._ended(
                        command,
                        EnumInboxWaitOutcome.UNREADABLE,
                        (),
                        0,
                        polls,
                        (reason,),
                        started,
                        f"the inbox of lane {command.lane} is unreadable: {reason}",
                    )
                read_failures.append(self._reason(exc))
            else:
                binding = self._binding_rows(lines, command)
                if binding:
                    stopped = any(self._is_stop(row) for row in binding)
                    outcome = (
                        EnumInboxWaitOutcome.STOP
                        if stopped
                        else EnumInboxWaitOutcome.WOKEN
                    )
                    verb = (
                        "a stop order ended the wait of"
                        if stopped
                        else "woken, binding rows for"
                    )
                    spent = self._clock.monotonic() - started
                    return self._ended(
                        command,
                        outcome,
                        binding,
                        len(binding),
                        polls,
                        tuple(read_failures),
                        started,
                        f"{verb} lane {command.lane} after {spent:.0f}s: "
                        f"{len(binding)} binding row(s) over {polls} poll(s)",
                    )
            remaining = deadline - self._clock.monotonic()
            if remaining <= 0:
                return self._ended(
                    command,
                    EnumInboxWaitOutcome.WAITED,
                    (),
                    0,
                    polls,
                    tuple(read_failures),
                    started,
                    f"lane {command.lane} waited its {command.seconds:g}s budget over "
                    f"{polls} poll(s); nothing binding arrived",
                )
            self._sleeper.sleep(min(self._overlay.poll_interval_seconds, remaining))

    def _reason(self, exc: InboxUnreadableError) -> str:
        return str(exc)[:_FAILURE_LIMIT] or type(exc).__name__

    def _binding_rows(
        self, lines: tuple[str, ...], command: ModelLaneInboxWaitCommand
    ) -> list[ParsedInboxRow]:
        rows = parse_inbox_rows(lines)
        answered = {
            row.cells["re"]
            for row in rows
            if row.cells.get("re")
            and (
                row.row_type == _RELEASE
                or (row.row_type == _ACK and row.cells.get("lane") == command.lane)
            )
        }
        return [
            row
            for row in rows
            if self._binding(row, command) and row.cells.get("id", "") not in answered
        ]

    def _binding(self, row: ParsedInboxRow, command: ModelLaneInboxWaitCommand) -> bool:
        """A row that can end the wait: a binding type, addressed to the lane or to the all
        target, not from the lane itself, strictly after ``since``, carrying an id."""
        overlay = self._overlay
        if row.row_type not in overlay.binding_types:
            return False
        targets = {
            name.strip() for name in row.cells.get("to", "").split(",") if name.strip()
        }
        named = command.lane in targets
        if not named and overlay.all_target not in targets:
            return False
        if (
            row.row_type == _HOLD
            and not named
            and any(cell in row.cells for cell in overlay.skip_hold_cells)
        ):
            return False
        if command.lane in (row.cells.get("from", ""), row.cells.get("lane", "")):
            return False
        if command.since and row.stamp <= command.since:
            return False
        return bool(row.cells.get("id"))

    def _is_stop(self, row: ParsedInboxRow) -> bool:
        overlay = self._overlay
        return (
            row.row_type == _MSG
            and row.cells.get(overlay.stop_cell) == overlay.stop_value
        )

    def _ended(
        self,
        command: ModelLaneInboxWaitCommand,
        outcome: EnumInboxWaitOutcome,
        binding: Sequence[ParsedInboxRow],
        total: int,
        polls: int,
        read_failures: tuple[str, ...],
        started: float,
        detail: str,
    ) -> ModelLaneInboxWaitResult:
        listed = sorted(binding, key=lambda row: (row.stamp, row.cells.get("id", "")))[
            : self._overlay.max_listed
        ]
        return ModelLaneInboxWaitResult(
            correlation_id=command.correlation_id,
            lane=command.lane,
            outcome=outcome,
            rows=tuple(
                ModelInboxWaitRow(
                    stamp=row.stamp,
                    row_type=row.row_type,
                    row_id=row.cells.get("id", ""),
                    sender=row.cells.get("from", ""),
                    text=row.text,
                )
                for row in listed
            ),
            total=total,
            polls=polls,
            waited_seconds=self._clock.monotonic() - started,
            read_failures=read_failures,
            detail=detail,
        )


def events_for(
    result: ModelLaneInboxWaitResult, topics: Mapping[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The chain of one wait: started, one woken event per listed binding row when the wait was
    woken or stopped, then the completed terminal - or the failed one when it was unreadable."""
    cid = result.correlation_id
    chain: list[tuple[str, dict[str, object]]] = [
        (topics["wait_started"], {"correlation_id": cid, "lane": result.lane})
    ]
    if result.outcome in (EnumInboxWaitOutcome.WOKEN, EnumInboxWaitOutcome.STOP):
        chain.extend(
            (
                topics["wait_woken"],
                {
                    "correlation_id": cid,
                    "lane": result.lane,
                    "row_id": row.row_id,
                    "row_type": row.row_type,
                    "stamp": row.stamp,
                    "sender": row.sender,
                },
            )
            for row in result.rows
        )
    failed = result.outcome is EnumInboxWaitOutcome.UNREADABLE
    chain.append(
        (
            topics["wait_failed" if failed else "wait_completed"],
            {
                "correlation_id": cid,
                "lane": result.lane,
                "outcome": result.outcome.value,
                "total": result.total,
                "polls": result.polls,
                "waited_seconds": result.waited_seconds,
                "read_failures": list(result.read_failures),
                "detail": result.detail,
            },
        )
    )
    return chain


__all__ = [
    "HandlerLaneInboxWait",
    "ParsedInboxRow",
    "WaitBudgetRefusedError",
    "events_for",
    "load_topics",
    "parse_inbox_row",
    "parse_inbox_rows",
]
