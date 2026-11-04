# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick command payload; host paths and executables belong to the server."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelSilentFailureAlarmRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID = Field(default_factory=uuid4)
    requested_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    operation: Literal["tick"] = "tick"
    # Replays one tick at a stated time; unset means the handler's own clock.
    now: AwareDatetime | None = None
    # False reads and decides and renders the rows, but appends and delivers nothing.
    apply: bool = True
