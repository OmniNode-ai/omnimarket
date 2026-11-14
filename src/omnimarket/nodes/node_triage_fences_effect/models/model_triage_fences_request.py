# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fence command payload; the ledger path belongs to the server (OMN-20682)."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelTriageFencesRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID = Field(default_factory=uuid4)
    requested_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    operation: Literal["triage_fences"] = "triage_fences"
    # Also fence the tickets with an open pull request in ``repos``, read from the PR watcher's
    # state with no GitHub call.
    include_open_prs: bool = False
    repos: tuple[str, ...] | None = None
    pr_window_hours: float | None = Field(default=None, gt=0)
    stale_after_hours: float | None = Field(default=None, gt=0)
    # Replays one derivation at a stated time; unset means the handler's own clock.
    now: AwareDatetime | None = None
