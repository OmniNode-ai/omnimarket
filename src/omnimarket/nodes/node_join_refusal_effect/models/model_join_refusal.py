# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the join refusal node (OMN-23022): the command, what each join owed and what was written."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelJoinRefusalCommand(BaseModel):
    """Which joins to answer, and whether to write the refusals to the emission journal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    write: bool
    only_joins: tuple[str, ...] = ()


class ModelJoinRefusalReading(BaseModel):
    """What one join owed on this run.

    ``upstream_failures`` are the real (not drill) typed failures of the upstream piece in the window. ``answered`` already
    had a chain of the downstream piece under their correlation id. ``owed`` is the rest, and ``written`` the part of it
    the journal took.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    join: str
    from_piece: int
    to_piece: int
    status: Literal["answered", "owed", "unread"]
    upstream_failures: int = 0
    drills_skipped: int = 0
    answered: int = 0
    owed: int = 0
    written: int = 0
    reason: str = ""


class ModelJoinRefusalResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    wrote: bool
    readings: tuple[ModelJoinRefusalReading, ...]
