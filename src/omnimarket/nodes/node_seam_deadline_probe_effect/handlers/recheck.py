# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The re-read of a red seam reading: a miss is read again soon, never held for the whole hour.

A dev runtime redeploy leaves its seams unanswered for the minutes the runtime takes to attach its
contracts. An hourly probe that lands in that window writes a NOT_WORKING row, and the floor reads
the newest row, so piece 3 stayed red for the rest of the hour after the seam answered again
(OMN-23201). The probe keeps its deadline and the reading its freshness bound; what changes is that
a red hourly run is followed by a few re-reads on a short timer until one reads WORKING.

Each real run records its verdict in a small state file beside the unit's logs. The recheck timer
runs ``read --recheck``: it re-reads only while the last run was red and fewer re-reads than the
contract allows have followed it, and it writes the same row an hourly run writes. A recheck never
hides the red row it follows: both stay on the ledger. A drill (an injected fault) neither records
state nor starts a recheck.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

STATE_FILE_NAME = "last-run.json"


class EnumRecheckDecision(StrEnum):
    RUN = "run"
    SKIP_LAST_RUN_WORKING = "skip-last-run-working"
    SKIP_NO_RUN_RECORDED = "skip-no-run-recorded"
    SKIP_ATTEMPTS_USED = "skip-attempts-used"
    SKIP_STATE_UNREADABLE = "skip-state-unreadable"


class ModelRecheckPolicy(BaseModel):
    """The contract's ``schedule.recheck`` block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unit_name: str = Field(min_length=1)
    #: How soon after the last timer run the next recheck tick fires (a systemd time span).
    interval: str = Field(min_length=1)
    #: How many re-reads may follow one hourly run; the hourly cadence resumes after them.
    attempts: int = Field(ge=1)
    description: str


class ModelLastRun(BaseModel):
    """What the last real (non-drill) run read, and how many re-reads have followed it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    working: bool
    rechecks_used: int = Field(ge=0)


def read_last_run(state_directory: Path) -> ModelLastRun | None | EnumRecheckDecision:
    """The recorded run, None when no run was recorded, or the unreadable decision."""
    path = state_directory / STATE_FILE_NAME
    if not path.exists():
        return None
    try:
        return ModelLastRun.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError):
        return EnumRecheckDecision.SKIP_STATE_UNREADABLE


def record_run(state_directory: Path, *, working: bool, recheck: bool) -> None:
    """Record one real run: an hourly run restarts the count, a recheck adds one to it."""
    previous = read_last_run(state_directory)
    used = (
        previous.rechecks_used + 1
        if recheck and isinstance(previous, ModelLastRun)
        else 0
    )
    state_directory.mkdir(parents=True, exist_ok=True)
    path = state_directory / STATE_FILE_NAME
    scratch = path.with_suffix(".tmp")
    scratch.write_text(
        ModelLastRun(working=working, rechecks_used=used).model_dump_json(),
        encoding="utf-8",
    )
    scratch.replace(path)


def decide_recheck(
    state_directory: Path, policy: ModelRecheckPolicy
) -> EnumRecheckDecision:
    """RUN only when the last real run read red and fewer than ``attempts`` re-reads followed it."""
    last = read_last_run(state_directory)
    if isinstance(last, EnumRecheckDecision):
        return last
    if last is None:
        return EnumRecheckDecision.SKIP_NO_RUN_RECORDED
    if last.working:
        return EnumRecheckDecision.SKIP_LAST_RUN_WORKING
    if last.rechecks_used >= policy.attempts:
        return EnumRecheckDecision.SKIP_ATTEMPTS_USED
    return EnumRecheckDecision.RUN


__all__ = [
    "STATE_FILE_NAME",
    "EnumRecheckDecision",
    "ModelLastRun",
    "ModelRecheckPolicy",
    "decide_recheck",
    "read_last_run",
    "record_run",
]
