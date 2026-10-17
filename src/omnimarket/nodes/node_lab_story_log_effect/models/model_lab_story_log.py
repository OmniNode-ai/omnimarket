# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the weekly lab story log."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

STORY_LOG_RELATIVE_DIR = "articles/lab-journey/story-log"


class ModelLabStoryLogRequest(BaseModel):
    """What week to write, where the ledger and the hygiene vocabulary are, where the log goes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    week: str = Field(
        default="previous",
        description="An ISO week such as 2026-W42, or 'previous' for the last whole ISO week before `now`.",
    )
    now: datetime | None = Field(
        default=None,
        description="The instant 'previous' is resolved against; defaults to the current UTC time.",
    )
    ledger_paths: tuple[Path, ...] = Field(
        description="The live ledger first, then any archive files; a file with no row in the week is read and skipped."
    )
    hygiene_vocabulary_path: Path = Field(
        description="The public-repo hygiene gate's private denylist vocabulary (repository names, host patterns, person names)."
    )
    person_names_path: Path | None = Field(
        default=None,
        description="The shared scrub wordlist of person names, one token per line.",
    )
    lab_vocabulary_path: Path | None = Field(
        default=None, description="The gate's lab vocabulary (lane ids)."
    )
    output_root: Path = Field(
        description="Root of the target checkout; the log lands at <root>/articles/lab-journey/story-log/<year>-W<week>.md."
    )
    dry_run: bool = Field(default=False, description="Render and check, write nothing.")


class ModelLabStoryLogResult(BaseModel):
    """The written log and the numbers about how it was made."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    week: str
    range_start: datetime
    range_end: datetime
    output_path: Path
    written: bool
    rows_in_week: int
    last_row_stamp: datetime | None
    rulings: int
    friction_rows: int
    merged_prs: int
    replacements: dict[str, int] = Field(
        description="Count of tokens replaced by a role, per class of private name."
    )
    markdown: str


__all__ = [
    "STORY_LOG_RELATIVE_DIR",
    "ModelLabStoryLogRequest",
    "ModelLabStoryLogResult",
]
