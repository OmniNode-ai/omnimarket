# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the observer keeps between polls: cursors, seen runs, reports made."""

from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, NonNegativeInt

#: Run keys remembered per process; a re-read inside this window adds nothing.
SEEN_RUN_KEYS_KEPT = 1024


class ModelProcessCursor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Bytes of an append-only evidence file already consumed.
    offset: NonNegativeInt = 0
    #: runs as launchd last reported it (latest-only evidence).
    runs_counter: NonNegativeInt | None = None
    last_checked_at: AwareDatetime | None = None
    #: <run_id>|<phase> of every run event already journaled, oldest first.
    emitted: list[str] = Field(default_factory=list)
    #: Runs started and not yet completed: run_id -> started_at.
    open_runs: dict[str, AwareDatetime] = Field(default_factory=dict)
    #: Open runs already reported OVERRUN.
    overrun_reported: list[str] = Field(default_factory=list)
    #: The reason an unreadable read was last reported, until it reads again.
    unreadable_reported: str | None = None


class ModelObserverState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_tick_at: AwareDatetime | None = None
    polls: NonNegativeInt = 0
    next_seq: NonNegativeInt = 0
    cursors: dict[str, ModelProcessCursor] = Field(default_factory=dict)
    #: UNDECLARED process_id -> when first seen.
    undeclared_first_seen: dict[str, AwareDatetime] = Field(default_factory=dict)


def load_state(path: Path) -> ModelObserverState:
    if not path.exists():
        return ModelObserverState()
    return ModelObserverState.model_validate_json(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: ModelObserverState) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(state.model_dump_json(), encoding="utf-8")
    tmp.replace(path)
