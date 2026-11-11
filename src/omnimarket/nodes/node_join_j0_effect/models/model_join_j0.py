# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the J0 join node (OMN-23022): the command, what one run read and wrote, and the floor record's reading."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelJoinJ0Command(BaseModel):
    """Whether the run writes its chains to the emission journal (``run``) or only reads (``probe``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    write: bool


class ModelFloorRecordReading(BaseModel):
    """The floor record read: how many rows its ``pieces`` list holds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    floor_rows: int = Field(ge=0)


class ModelFloorRecordFailure(BaseModel):
    """The typed failure of reading the floor record: the code and the detail that ended the piece-17 chain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1)
    detail: str = ""


class ModelJoinJ0Result(BaseModel):
    """One J0 run.

    ``outcome`` is ``golden`` when the floor record read and drew a reading age for the piece the code graph draws,
    and ``error`` when the piece-17 side ended in a typed failure (``failure_code`` names it, ``detail`` says why).
    ``control`` reports the deliberately corrupt record the overlay feeds the reader: ``broke`` when the reader
    rejected it and the control's chains were written, ``did_not_break`` when the reader accepted it (nothing is
    written for the control and the run fails), and ``not_run`` when the real run failed and the control was
    skipped. The golden/error outcome is about the real run only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal["golden", "error"]
    wrote: bool
    correlation_id: str
    failure_code: str = ""
    detail: str = ""
    floor_rows: int | None = None
    control: Literal["not_run", "broke", "did_not_break"] = "not_run"
    control_correlation_id: str = ""
