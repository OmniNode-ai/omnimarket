# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the pinned-rulings step of the host mirror is asked to do and what it did (OMN-26412)."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class EnumPinnedRulingsStatus(StrEnum):
    #: Every pinned ruling is on the mirror's archive roll.
    MIRRORED = "mirrored"
    #: At least one pinned ruling was in none of the host's archive rolls; the roll holds the ones found.
    MISSING = "missing"


class ModelPinnedRuling(BaseModel):
    """One ruling a brief template cites by stamp and lane, whatever its age."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ts: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    lane: str = Field(min_length=1)

    @property
    def prefix(self) -> str:
        """The leading cells of the ruling's row, the same three cells a brief's landing check matches."""
        return f"{self.ts} | RULING | lane={self.lane} |"


class ModelPinnedRulingsRequest(BaseModel):
    """Where the host's archive rolls are, where the mirror's own roll goes, and the rulings to carry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rolls_directory: Path
    target_path: Path
    pinned: tuple[ModelPinnedRuling, ...]


class ModelPinnedRulingsResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumPinnedRulingsStatus
    rows_written: int = Field(ge=0)
    target_path: str
    missing: tuple[str, ...] = ()
    detail: str = ""
