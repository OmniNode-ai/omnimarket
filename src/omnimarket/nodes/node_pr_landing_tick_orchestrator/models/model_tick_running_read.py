# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the landing tick did for the running-read due node on one tick (OMN-26549)."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelTickRunningReadItem(BaseModel):
    """One append request the due node asked the tick for: a due row at a merge, or an alarm at a read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: Literal["due_row", "alarm"]
    due_id: str
    request_id: UUID
    sent: bool = Field(
        default=False,
        description="The request went to the ledger appender; only an enabled act mode sends.",
    )
    would_send: bool = Field(
        default=False,
        description="Observe and replay: the request that act mode would send; nothing was appended.",
    )
    detail: str = ""


class ModelTickRunningRead(BaseModel):
    """The tick's running-read step: its requests, the due rows still open, and the tickets held out of Done."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: tuple[ModelTickRunningReadItem, ...] = ()
    open_due_ids: tuple[str, ...] = ()
    blocked_tickets: tuple[str, ...] = ()
    refusals: tuple[str, ...] = Field(
        default=(),
        description="Why the due node refused a merge or the step raised; the tick stays compared.",
    )


__all__: list[str] = ["ModelTickRunningRead", "ModelTickRunningReadItem"]
