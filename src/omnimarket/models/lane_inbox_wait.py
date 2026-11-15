# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of node_lane_inbox_wait_effect (why-process-6803).

One command is one wait: a lane that sits in a long wait cannot see its inbox, so the
wait helper polls it on the lane's behalf until a binding MSG or HOLD is addressed to
the lane, a stop order arrives, or the wait's budget is spent. The rows are the rolling
work ledger's own row grammar (``<stamp> | <TYPE> | key=value | ... | free text``), read
through the injected inbox source; ``since`` bounds what counts, because a row the lane
already saw before it started waiting is not news.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

STAMP_PATTERN = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
STAMP_RE = re.compile(rf"^{STAMP_PATTERN}$")


class EnumInboxWaitOutcome(StrEnum):
    """How one wait ended."""

    #: The budget elapsed and nothing binding reached the lane.
    WAITED = "waited"
    #: One or more binding rows reached the lane; they are listed on the result.
    WOKEN = "woken"
    #: A stop order reached the lane; the rows (stop orders among them) are listed.
    STOP = "stop"
    #: The inbox could not be read: the source failed on the first read, or on every
    #: read of the loop. A later single failure is recorded and waited out instead.
    UNREADABLE = "unreadable"


class ModelInboxWaitRow(BaseModel):
    """One binding ledger row, as the wait lists it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The row's own UTC stamp, ``%Y-%m-%dT%H:%M:%SZ``.
    stamp: str = Field(pattern=STAMP_RE.pattern)
    row_type: str = Field(min_length=1)
    #: The row's ``id=`` cell; a binding row always carries one.
    row_id: str = Field(min_length=1)
    #: The row's ``from=`` cell, empty when the row carries none.
    sender: str = ""
    #: The row's free text (the cells that are not ``key=value``).
    text: str = ""


class ModelLaneInboxWaitCommand(BaseModel):
    """One wait: the lane, its budget and the stamp only rows after which count."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    lane: str = Field(min_length=1)
    #: The wait's budget in seconds; the overlay's ``max_wait_seconds`` bounds it.
    seconds: float = Field(gt=0)
    #: A UTC stamp ``%Y-%m-%dT%H:%M:%SZ``; only rows stamped strictly after it count.
    #: Empty means no lower bound. The CLI fills it with the stamp at start.
    since: str = ""

    @field_validator("since")
    @classmethod
    def _stamp_or_empty(cls, value: str) -> str:
        if value and STAMP_RE.match(value) is None:
            raise ValueError(
                "since must be a UTC stamp %Y-%m-%dT%H:%M:%SZ (or empty for no bound)"
            )
        return value


class ModelLaneInboxWaitResult(BaseModel):
    """How the wait ended, and the rows that ended it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    lane: str
    outcome: EnumInboxWaitOutcome
    #: The binding rows, sorted by (stamp, id), at most the overlay's ``max_listed``.
    rows: tuple[ModelInboxWaitRow, ...]
    #: All binding rows found, also the ones past ``max_listed``.
    total: int
    #: Reads of the inbox the wait made, including the one that ended it.
    polls: int
    #: How much of the budget the wait spent, on the injected monotonic clock.
    waited_seconds: float
    #: Why single later reads failed, in order; the wait went on past each.
    read_failures: tuple[str, ...]
    #: One line a caller can print: what ended the wait, and when.
    detail: str


__all__ = [
    "STAMP_PATTERN",
    "STAMP_RE",
    "EnumInboxWaitOutcome",
    "ModelInboxWaitRow",
    "ModelLaneInboxWaitCommand",
    "ModelLaneInboxWaitResult",
]
