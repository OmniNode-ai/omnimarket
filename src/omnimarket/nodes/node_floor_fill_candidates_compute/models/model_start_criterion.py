# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the start criterion: what a task's ``Start when:`` line parses to, and the served pieces list it is read against."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

M0Verdict = Literal["PASS", "FAIL", "UNREAD"]
PrerequisiteKind = Literal["piece-working", "merged", "m0-pass"]
PiecesUnreadCause = Literal["", "absent", "malformed", "old"]
WORKING = "WORKING"


class ModelFloorStartCriterion(BaseModel):
    """The ``Start when:`` line of one task's ticket, as the orchestrator read it. The node parses the line itself."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    line_id: str
    task_id: str
    start_when: str


class ModelStartPrerequisite(BaseModel):
    """One prerequisite of a start criterion: a piece reading WORKING, a merged reference, or the M0 PASS verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: PrerequisiteKind
    # The piece number of a ``piece-working`` prerequisite.
    piece: int | None = None
    # The pull request (``repo#n``) or ticket id of a ``merged`` prerequisite.
    ref: str = ""

    def label(self) -> str:
        """The words the prerequisite is named by in a not-ready cause."""
        if self.kind == "piece-working":
            return f"piece {self.piece} WORKING"
        if self.kind == "merged":
            return f"{self.ref} merged"
        return "M0 PASS"


class ModelStartCriterionParse(BaseModel):
    """The parse of one ``Start when:`` line: its prerequisites (none for a line that says ``none``), or why it cannot be read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prerequisites: tuple[ModelStartPrerequisite, ...] = ()
    # Set when the line cannot be read; the task then reads not ready with this cause.
    unparseable_cause: str = ""


# ------------------------------------------------------------------ the served pieces list (floor-simple record)


class ModelPieceReading(BaseModel):
    """One piece of the served floor pieces list, as far as the criterion reads it."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    piece: int
    reading_state: str = ""
    reading_age_s: int | None = Field(default=None, ge=0)
    reading_max_age_s: int | None = Field(default=None, gt=0)


class ModelPiecesRecordRead(BaseModel):
    """The served floor record (``floor-simple.json``), as far as the criterion reads its pieces list."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    generated_at: str
    freshness_max_age_s: int = Field(gt=0)
    pieces: tuple[ModelPieceReading, ...]
