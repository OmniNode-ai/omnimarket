# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reading counts and instants out of receipts and log lines."""

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.models.liveness.model_automation_liveness import (
    DEMAND_NONE,
    EnumAutomationRunOutcome,
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_instant(text: str) -> datetime | None:
    """An ISO-8601 instant with an offset; None for anything else.

    A timestamp without an offset is refused rather than assumed UTC: a wrong
    guess shifts a run across the OVERRUN bound without a trace.
    """
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def work_field_names(real_work: str) -> tuple[str, ...]:
    """Field names in an entry's real_work such as merges+arms."""
    names = tuple(part.strip() for part in real_work.split("+"))
    return tuple(name for name in names if _IDENTIFIER.match(name))


def count_work(real_work: str, fields: Mapping[str, int]) -> int:
    """Sum of the named work fields present; 0 when the run counted none."""
    return sum(fields.get(name, 0) for name in work_field_names(real_work))


def demand_field_name(demand: str) -> str | None:
    """The field a demand names when it is a bare field name, else None."""
    if demand == DEMAND_NONE:
        return None
    return demand if _IDENTIFIER.match(demand) else None


def outcome_for_exit(exit_code: int) -> EnumAutomationRunOutcome:
    return (
        EnumAutomationRunOutcome.OK
        if exit_code == 0
        else EnumAutomationRunOutcome.FAILED
    )


def read_complete_lines(path: Path, offset: int) -> tuple[list[tuple[int, bytes]], int]:
    """New complete lines of an append-only file and the offset after them.

    A file shorter than ``offset`` was rotated and is read from the start. A
    trailing line without its newline is left for the next poll, so a record
    being written is never parsed half-way. Raises ``OSError`` when unreadable.
    """
    raw = path.read_bytes()
    start = offset if offset <= len(raw) else 0
    end = raw.rfind(b"\n") + 1
    if end <= start:
        return [], start
    line_no = raw.count(b"\n", 0, start) + 1
    lines = []
    for line in raw[start:end].split(b"\n")[:-1]:
        lines.append((line_no, line))
        line_no += 1
    return lines, end
