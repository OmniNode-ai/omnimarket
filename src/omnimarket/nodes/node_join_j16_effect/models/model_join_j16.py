# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the J16 join node (OMN-23122): the command and what one run read and wrote."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ModelJoinJ16Command(BaseModel):
    """Whether the run writes its two chains to the emission journal (``run``) or only reads (``probe``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    write: bool


class ModelJoinJ16Result(BaseModel):
    """One J16 run.

    ``outcome`` is ``golden`` when piece 16 produced an eval result and the floor record drew it with an age, ``error``
    when either side ended in a typed failure (``failure_code`` names it, ``failed_side`` says which piece), and
    ``unread`` when the ledger could not be read (nothing is written: an unreadable ledger is never a result).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal["golden", "error", "unread"]
    wrote: bool
    correlation_id: str
    failed_side: Literal["", "from", "to"] = ""
    failure_code: str = ""
    detail: str = ""
    reading_age_s: int | None = None
