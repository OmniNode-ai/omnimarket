"""Typed input and output of the operator-waiting tick's list and acting row (OMN-19854).

Frozen dataclasses, not pydantic: loops_check.py, which calls the handler, runs in the offline skill suites and on
installed plugin copies, where only the standard library is installed.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class ModelOperatorWaitingRequest:
    """The open asks the tick read from the ledger inbox, which of them are new, and where its row is appended."""

    asks: list[
        dict[str, str]
    ]  # loops_check.read_operator_asks: stamp, id, ticket, line, text
    new: frozenset[str]  # the ids of the asks not open at the previous run
    now: datetime
    ledger: str  # the rolling ledger the row is appended to
    resolver: Path  # the plugin's scripts/onex_ledger.py, the one row writer
    lane: str  # the session lane the row is written as
    timeout: float  # seconds the row writer may take


@dataclass(frozen=True)
class ModelOperatorWaiting:
    """The tick's output lines, and why its acting row was not written (None when it was, or no ask was open)."""

    lines: list[str]
    not_recorded: str | None
